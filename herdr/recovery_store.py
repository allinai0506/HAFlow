"""Transactional recovery obligations in the workflow's authoritative SQLite DB."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS workflow_recovery_operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT NOT NULL UNIQUE,
        workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
        owner TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0,
        next_due_at REAL, started INTEGER NOT NULL DEFAULT 0, detail_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS recovery_workflow_due ON workflow_recovery_operations(workflow_id,status,next_due_at)')


def _decode(row):
    result = dict(row)
    result['payload'] = json.loads(result.pop('payload_json'))
    result['detail'] = json.loads(result.pop('detail_json'))
    return result


def _get(conn, operation_id):
    row = conn.execute('SELECT * FROM workflow_recovery_operations WHERE id=?', (operation_id,)).fetchone()
    if row is None:
        raise ValueError('recovery operation not found')
    return _decode(row)


def _event(conn, operation, action, now, detail=None):
    from herdr.state_db import record_event
    record_event({'workflow_id': operation['workflow_id'], 'event_type': 'recovery.' + action,
                  'timestamp': now, 'source': 'recovery_store',
                  'payload': {'operation_id': operation['id'], 'version': operation['version'],
                              'identity_key': operation['identity_key'], 'detail': detail or {}}}, conn=conn)


def _workflow_snapshot(conn, workflow_id):
    row = conn.execute('SELECT * FROM workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
    if row is None:
        raise ValueError('workflow not found')
    workflow = json.loads(row['metadata_json'] or '{}')
    workflow.update({k: row[k] for k in ('workflow_id', 'title', 'template_name', 'current_stage', 'status', 'created_at', 'updated_at')})
    if len((row['config_json'] or '').encode()) > 262144:
        raise ValueError('pinned configuration exceeds snapshot budget')
    config = json.loads(row['config_json'] or '{}')
    epoch = max(float(workflow.get('created_at') or 0), float(workflow.get('reopened_at') or 0))
    frozen = conn.execute("SELECT id,payload_json FROM events WHERE workflow_id=? AND event_type='candidate_frozen' AND source='critical-path-scheduler' AND timestamp>=? ORDER BY id DESC LIMIT 1", (workflow_id, epoch)).fetchone()
    workflow['candidate_sha'] = json.loads(frozen['payload_json']).get('candidate_sha') if frozen else None
    workflow['candidate_episode_id'] = frozen['id'] if frozen else 0
    workflow['config'] = config
    return workflow, config


def _snapshot(conn, workflow_id):
    from herdr.state_db import _decode_task_row
    workflow, config = _workflow_snapshot(conn, workflow_id)
    tasks = [_decode_task_row(r) for r in conn.execute('SELECT * FROM tasks WHERE workflow_id=? LIMIT 10001', (workflow_id,))]
    if len(tasks) > 10000:
        raise ValueError('workflow tasks exceed snapshot budget')
    return workflow, config, tasks


def read_snapshot(db_path, workflow_id):
    """Read workflow, pinned configuration and tasks from one SQLite snapshot.

    Candidate publication events override the metadata projection. This entry
    never imports mutable files or initializes a missing database.
    """
    from herdr.state_db import get_readonly_db_connection
    conn = get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        return _snapshot(conn, workflow_id)
    finally:
        conn.close()


def ensure_obligations(conn, workflow, config, tasks, now=None):
    from herdr.workflow_progress import assess_workflow, recovery_identity
    now = time.time() if now is None else now
    assessment = assess_workflow(workflow, config, tasks)
    identities = set()
    result = []
    for facts in assessment['obligations']:
        identity = facts.get('identity_key') or recovery_identity(workflow, facts)
        identities.add(identity)
        payload = json.dumps(facts, sort_keys=True, ensure_ascii=False)
        status = facts.get('status', 'pending')
        cur = conn.execute('''INSERT OR IGNORE INTO workflow_recovery_operations
            (identity_key,workflow_id,payload_json,status,next_due_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)''', (identity, workflow['workflow_id'], payload, status, now, now, now))
        row = conn.execute('SELECT * FROM workflow_recovery_operations WHERE identity_key=?', (identity,)).fetchone()
        if not cur.rowcount and row['payload_json'] != payload:
            if row['status'] == 'running' and row['started']:
                continue
            changed_status = status if (row['status'] == 'running' or (row['status'] == 'waiting_human' and not row['started'] and row['detail_json'] == '{}')) else row['status']
            conn.execute('UPDATE workflow_recovery_operations SET payload_json=?,status=?,owner=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=?', (payload, changed_status, now, row['id']))
            _event(conn, _get(conn, row['id']), 'facts_changed', now)
        op = _get(conn, row['id'])
        if cur.rowcount:
            _event(conn, op, 'registered', now)
        result.append(op)
    for row in conn.execute("SELECT * FROM workflow_recovery_operations WHERE workflow_id=? AND status NOT IN ('superseded','resolved')", (workflow['workflow_id'],)).fetchall():
        if json.loads(row['payload_json']).get('kind') == 'node_dispatch':
            continue  # Its independent dispatch lifecycle owns reconciliation.
        if row['identity_key'] not in identities:
            if (row['status'] == 'awaiting_result' or (row['started'] and row['status'] == 'running')
                    or (row['started'] and row['status'] == 'pending'
                        and json.loads(row['detail_json'] or '{}').get('action') == 'verify')):
                continue
            if row['started']:
                if row['status'] != 'waiting_human':
                    conn.execute("UPDATE workflow_recovery_operations SET status='waiting_human',version=version+1,owner=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now, row['id']))
                continue
            conn.execute("UPDATE workflow_recovery_operations SET status='superseded',version=version+1,owner=NULL,lease_until=NULL,updated_at=? WHERE id=?", (now, row['id']))
            _event(conn, _get(conn, row['id']), 'superseded', now)
    return result


def ensure_for_workflow(conn, workflow_id, now=None):
    return ensure_obligations(conn, *_snapshot(conn, workflow_id), now=now)


def list_operations(db_path, workflow_id, limit=100):
    path = Path(db_path).resolve()
    if not path.exists():
        return []
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_recovery_operations'").fetchone():
            return []
        return [_decode(r) for r in conn.execute("SELECT * FROM workflow_recovery_operations WHERE workflow_id=? ORDER BY CASE WHEN status IN ('resolved','superseded') THEN 1 ELSE 0 END,next_due_at,id LIMIT ?", (workflow_id, max(0, min(int(limit), 1000))))]
    finally:
        conn.close()


@contextmanager
def _transaction(db_path):
    from herdr.state_db import get_db_connection
    conn = get_db_connection(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _expire(conn, workflow_id, now):
    for row in conn.execute("SELECT * FROM workflow_recovery_operations WHERE workflow_id=? AND ((status='running' AND lease_until<=?) OR (status='waiting' AND next_due_at<=?))", (workflow_id, now, now)).fetchall():
        if json.loads(row['payload_json']).get('kind') == 'node_dispatch' and row['status'] == 'running':
            continue  # Unknown transport is checked against its fixed dispatch deadline.
        unknown = row['status'] == 'running' and row['started']
        status = 'waiting_human' if unknown or row['status'] == 'waiting' or row['attempts'] >= 3 else 'pending'
        detail = {**json.loads(row['detail_json']), 'reason': 'delivery_unknown' if unknown else 'lease_expired' if row['status'] == 'running' else 'hold_expired'}
        conn.execute('UPDATE workflow_recovery_operations SET status=?,detail_json=?,owner=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=?', (status, json.dumps(detail), now, row['id']))
        _event(conn, _get(conn, row['id']), 'expired', now, detail)


def reconcile(db_path, workflow_id, now=None, active_only=False, limit=32):
    now = time.time() if now is None else now
    with _transaction(db_path) as conn:
        ensure_for_workflow(conn, workflow_id, now)
        _expire(conn, workflow_id, now)
        where = " AND status IN ('pending','awaiting_result') AND next_due_at<=?" if active_only else ''
        order = 'next_due_at,id' if active_only else 'id'
        params = (workflow_id, now, max(0, min(int(limit), 1000))) if active_only else (workflow_id, max(0, min(int(limit), 1000)))
        return [_decode(r) for r in conn.execute('SELECT * FROM workflow_recovery_operations WHERE workflow_id=?' + where + ' ORDER BY ' + order + ' LIMIT ?', params)]


def claim_operation(db_path, operation_id, owner, now, lease_seconds=60):
    if not owner or lease_seconds <= 0:
        raise ValueError('owner and positive lease required')
    with _transaction(db_path) as conn:
        op = _get(conn, operation_id)
        ensure_for_workflow(conn, op['workflow_id'], now)
        _expire(conn, op['workflow_id'], now)
        workflow, _, _ = _snapshot(conn, op['workflow_id'])
        if workflow['status'] != 'running' or workflow.get('startup_not_ready') or workflow.get('startup_ready') is False:
            return None
        cur = conn.execute("UPDATE workflow_recovery_operations SET status='running',owner=?,lease_until=?,attempts=attempts+1,version=version+1,updated_at=? WHERE id=? AND status='pending' AND next_due_at<=? AND attempts<3", (owner, now + lease_seconds, now, operation_id, now))
        if not cur.rowcount:
            return None
        result = _get(conn, operation_id)
        _event(conn, result, 'claimed', now)
        return result


def _owned(conn, operation_id, owner, now):
    op = _get(conn, operation_id)
    if op['status'] != 'running' or op['owner'] != owner or op['lease_until'] <= now:
        raise ValueError('stale recovery owner or lease')
    return op


def mark_started(db_path, operation_id, owner, step, now):
    if not step:
        raise ValueError('step required')
    with _transaction(db_path) as conn:
        current = _get(conn, operation_id)
        ensure_for_workflow(conn, current['workflow_id'], now)
        _owned(conn, operation_id, owner, now)
        conn.execute('UPDATE workflow_recovery_operations SET started=1,detail_json=?,version=version+1,updated_at=? WHERE id=?', (json.dumps({'step': step}), now, operation_id))
        op = _get(conn, operation_id)
        _event(conn, op, 'started', now, {'step': step})
        return op


def _validate_step(conn, operation, now, step=None, detail=None):
    from herdr.workflow_progress import assess_workflow, recovery_identity
    workflow, config, tasks = _snapshot(conn, operation['workflow_id'])
    payload = operation['payload']
    receipt = {**operation['detail'], **(detail or {})}
    if workflow.get('status') != 'running' or workflow.get('startup_ready') is False or workflow.get('startup_not_ready'):
        raise ValueError('recovery workflow became inactive')
    original_workflow = dict(workflow, candidate_sha=payload.get('candidate_sha'))
    if payload.get('identity') and recovery_identity(original_workflow, payload.get('facts') or []) != payload['identity']:
        raise ValueError('recovery generation changed before effect')
    if payload.get('candidate_sha') != workflow.get('candidate_sha'):
        if receipt.get('action') == 'verify' and step in ('renew_owner', 'verify_receipts'):
            return operation
        raise ValueError('recovery candidate changed before effect')
    if step == 'gates_invalidating':
        from herdr.workflow_recovery import repair_coverage
        if repair_coverage(dict(operation, detail=receipt), tasks):
            raise ValueError('incomplete repair coverage before gate invalidation')
        from herdr.recovery_successor import confirmed_rework
        for original_id, mapping in (receipt.get('repair_map') or {}).items():
            if mapping.get('kind') == 'rework':
                task = next((t for t in tasks if t['task_id'] == original_id), {})
                if not confirmed_rework(conn, task, mapping.get('request_id')):
                    raise ValueError('unconfirmed rework request before gate invalidation')
    by_id = {task['task_id']: task for task in tasks}
    original_affected = set(payload.get('affected_task_ids') or [])
    confirmed_successors = set()
    for successor_id in receipt.get('successor_ids') or []:
        successor = by_id.get(successor_id) or {}
        predecessor_id = successor.get('supersedes')
        predecessor = by_id.get(predecessor_id) or {}
        if ((predecessor_id in original_affected or successor_id in original_affected)
                and predecessor.get('superseded_by') == successor_id
                and successor.get('workflow_id') == operation['workflow_id']
                and successor.get('execution_id') == workflow.get('execution_id')):
            confirmed_successors.add(successor_id)
    from herdr.recovery_successor import confirmed_rework
    delivered_reworks = {task_id for task_id in receipt.get('rework_ids') or []
                         if task_id in by_id and confirmed_rework(conn, by_id[task_id],
                             (receipt.get('rework_requests') or {}).get(task_id))}
    assessed = assess_workflow(workflow, config, tasks)['obligations']
    current = next((facts for facts in assessed if facts.get('identity_key') == operation['identity_key']), None)
    expected_gates = set(payload.get('task_ids') or [])
    invalidating = (operation['detail'].get('gate_invalidation_started')
                    or operation['detail'].get('step') == 'gates_invalidating')
    removed_gates = {task_id for task_id in expected_gates
                     if by_id.get(task_id, {}).get('status') == 'superseded'
                     or by_id.get(task_id, {}).get('superseded_by')}
    current_gates = set((current or {}).get('task_ids') or [])
    if (current is None and not (invalidating and removed_gates == expected_gates)) or (
            current_gates - expected_gates - delivered_reworks):
        raise ValueError('new recovery facts require replanning')
    missing_gates = expected_gates - current_gates
    if missing_gates and (not invalidating or missing_gates - removed_gates):
        raise ValueError('recovery gate disappeared before owned invalidation')
    if set((current or {}).get('affected_task_ids') or []) - original_affected - confirmed_successors:
        raise ValueError('new affected recovery lineage requires replanning')
    for original in payload.get('facts') or []:
        task = by_id.get(original.get('task_id'))
        if task is None:
            raise ValueError('recovery source task disappeared')
        for key in ('run_id', 'execution_id', 'candidate_sha', 'commit', 'stage_verdict', 'stage_verdict_note'):
            if original.get(key) == task.get(key):
                continue
            delivered_clear = (task['task_id'] in delivered_reworks
                               and task.get('run_id') == original.get('run_id')
                               and ((key in ('stage_verdict', 'stage_verdict_note') and task.get(key) in (None, ''))
                                    or (key == 'commit' and receipt.get('action') == 'verify'
                                        and isinstance(task.get('commit'), str)
                                        and len(task['commit']) == 40)))
            if not delivered_clear:
                raise ValueError('recovery source facts changed before effect')
    return operation


def validate_step(db_path, operation_id, owner, step, detail, now):
    with _transaction(db_path) as conn:
        operation = _owned(conn, operation_id, owner, now)
        return _validate_step(conn, operation, now, step, detail)


def renew_owner(db_path, operation_id, owner, now, lease_seconds=60):
    if lease_seconds <= 0:
        raise ValueError('positive recovery lease required')
    with _transaction(db_path) as conn:
        operation = _owned(conn, operation_id, owner, now)
        _validate_step(conn, operation, now, 'renew_owner')
        conn.execute('UPDATE workflow_recovery_operations SET lease_until=?,version=version+1,updated_at=? WHERE id=? AND owner=? AND lease_until>?', (now + lease_seconds, now, operation_id, owner, now))
        renewed = _get(conn, operation_id)
        _event(conn, renewed, 'renewed', now)
        return renewed


def record_step(db_path, operation_id, owner, step, detail, now, validate=True):
    if not step or not isinstance(detail, dict):
        raise ValueError('step and detail required')
    with _transaction(db_path) as conn:
        current = _owned(conn, operation_id, owner, now)
        if validate:
            _validate_step(conn, current, now, step, detail)
        receipt = {**current['detail'], **detail, 'step': step}
        if step == 'gates_invalidating':
            receipt['gate_invalidation_started'] = True
        conn.execute('UPDATE workflow_recovery_operations SET started=1,detail_json=?,version=version+1,updated_at=? WHERE id=?', (json.dumps(receipt), now, operation_id))
        op = _get(conn, operation_id)
        _event(conn, op, 'step', now, receipt)
        return op


def finish_operation(db_path, operation_id, owner, status, detail, now):
    if status not in ('waiting', 'pending', 'waiting_human', 'awaiting_result', 'resolved', 'superseded'):
        raise ValueError('invalid recovery status')
    with _transaction(db_path) as conn:
        current = _owned(conn, operation_id, owner, now)
        detail = {**current['detail'], **detail}
        conn.execute('UPDATE workflow_recovery_operations SET status=?,detail_json=?,next_due_at=?,owner=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=?', (status, json.dumps(detail), detail.get('next_due_at', now), now, operation_id))
        op = _get(conn, operation_id)
        _event(conn, op, 'finished', now, detail)
        return op


def decide_operation(db_path, operation_id, expected_version, operator, action, reason, now, until=None):
    if not isinstance(operator, str) or not operator.strip() or not isinstance(reason, str) or not reason.strip() or action not in ('retry', 'hold', 'verify'):
        raise ValueError('operator, reason and retry/hold/verify action required')
    if action == 'hold' and (until is None or until <= now):
        raise ValueError('future hold deadline required')
    with _transaction(db_path) as conn:
        original = _get(conn, operation_id)
        if original['version'] != expected_version:
            raise ValueError('stale recovery version')
        ensure_for_workflow(conn, original['workflow_id'], now)
        op = _get(conn, operation_id)
        workflow, _, _ = _snapshot(conn, op['workflow_id'])
        if op['version'] != expected_version or op['status'] in ('superseded', 'resolved', 'running') or workflow['status'] in ('closed', 'paused'):
            raise ValueError('stale recovery facts or inactive workflow')
        if action == 'retry' and ((op['payload'].get('status') == 'waiting_human' and op['payload'].get('reason') != 'finalize_escalated') or op['started']):
            raise ValueError('retry prerequisites unknown; delivery must be verified')
        if op['payload'].get('kind') == 'node_dispatch':
            from .node_dispatch import current, scope_cancelled
            from .node_dispatch_store import _snapshot as dispatch_snapshot, _prior_delivery_unknown
            workflow, config, dispatch_tasks = dispatch_snapshot(conn, op['workflow_id'])
            if action == 'retry' and (scope_cancelled(op, workflow, dispatch_tasks)
                                      or _prior_delivery_unknown(conn, op)):
                raise ValueError('dispatch scope cancelled or prior delivery unknown; verify responsibility first')
            if not current(op, workflow, config):
                raise ValueError('stale dispatch generation or configuration')
        if action == 'verify':
            from herdr.workflow_progress import recovery_identity
            if op['status'] not in ('waiting_human', 'awaiting_result') or not op['started']:
                raise ValueError('verify requires an unresolved started receipt')
            facts = op['payload']
            if facts.get('identity') and recovery_identity(dict(workflow, candidate_sha=facts.get('candidate_sha')), facts.get('facts') or []) != facts['identity']:
                raise ValueError('stale recovery generation or candidate')
        detail = {**op['detail'], 'operator': operator, 'action': action, 'reason': reason}
        conn.execute('UPDATE workflow_recovery_operations SET status=?,next_due_at=?,detail_json=?,attempts=0,version=version+1,updated_at=? WHERE id=? AND version=?', ('pending' if action in ('retry', 'verify') else 'waiting', now if action in ('retry', 'verify') else until, json.dumps(detail), now, operation_id, expected_version))
        result = _get(conn, operation_id)
        _event(conn, result, 'decided', now, detail)
        return result


def settle_result(db_path, operation_id, expected_version, now):
    """Settle only from fresh same-transaction task facts and the core proof."""
    from types import SimpleNamespace

    from herdr.workflow_recovery import result_status

    from .observation import ObservationStore
    from .task_checkpoint import business_acceptance_receipt, has_business_acceptance
    observations = ObservationStore(db_path)
    evidence_store = SimpleNamespace(db_path=db_path)
    from .state_db import get_readonly_db_connection
    reader = get_readonly_db_connection(db_path)
    try:
        reader.execute('BEGIN')
        prepared_op = _get(reader, operation_id)
        prepared_workflow, _, prepared_tasks = _snapshot(reader, prepared_op['workflow_id'])
        prepared_scope = {t['task_id']: t.get('version') for t in prepared_tasks}
        acceptance = {}
        evidence_ids = {}
        for task in prepared_tasks:
            if task.get('completion_protocol') == 'receipt-v1' and (task.get('node') or task.get('stage')) in (prepared_op['detail'].get('gate_nodes') or []):
                acceptance[task['task_id']] = has_business_acceptance(evidence_store, task,
                    prepared_workflow.get('candidate_sha'), conn=reader, observations=observations)
                receipt = business_acceptance_receipt(reader, task, prepared_workflow.get('candidate_sha'))
                evidence_ids[task['task_id']] = receipt['id'] if receipt else None
    finally:
        reader.close()
    # No Git/content hashing occurs under BEGIN IMMEDIATE. Recheck the exact
    # episode, Task versions and latest business-receipt IDs before settling.
    with _transaction(db_path) as conn:
        op = _get(conn, operation_id)
        if op['version'] != expected_version or op['status'] != 'awaiting_result':
            raise ValueError('stale recovery result version or status')
        workflow, _, tasks = _snapshot(conn, op['workflow_id'])
        if workflow['status'] != 'running' or workflow.get('startup_ready') is False:
            return op
        if (prepared_op['version'] != op['version']
                or prepared_workflow.get('candidate_episode_id') != workflow.get('candidate_episode_id')
                or prepared_workflow.get('execution_id') != workflow.get('execution_id')
                or {t['task_id']: t.get('version') for t in tasks} != prepared_scope):
            raise ValueError('recovery acceptance snapshot changed')
        for task in tasks:
            if task['task_id'] in evidence_ids:
                receipt = business_acceptance_receipt(conn, task, workflow.get('candidate_sha'))
                if (receipt['id'] if receipt else None) != evidence_ids[task['task_id']]:
                    raise ValueError('recovery acceptance receipt changed')
        outcome = result_status(op, workflow, tasks, acceptance=acceptance)
        if outcome and outcome[0] == 'resolved':
            from herdr.recovery_successor import confirmed_rework
            by_id = {task['task_id']:task for task in tasks}
            for original_id, mapping in (op['detail'].get('repair_map') or {}).items():
                if mapping.get('kind') == 'rework' and not confirmed_rework(conn, by_id.get(original_id, {}), mapping.get('request_id')):
                    outcome = ('waiting_human', {'reason':'rework_delivery_unconfirmed'})
                    break
        if outcome is None and op['detail'].get('result_deadline_at', op['updated_at'] + 3600) <= now:
            outcome = ('waiting_human', {'reason': 'recovery_result_timeout'})
        if outcome is None:
            conn.execute('UPDATE workflow_recovery_operations SET next_due_at=? WHERE id=?', (now + 30, operation_id))
            return op
        status, detail = outcome if isinstance(outcome, tuple) else (outcome, {})
        if status not in ('resolved', 'waiting_human'):
            raise ValueError('invalid recovery result status')
        receipt = {**op['detail'], **detail}
        conn.execute('UPDATE workflow_recovery_operations SET status=?,detail_json=?,version=version+1,updated_at=? WHERE id=? AND version=?', (status, json.dumps(receipt), now, operation_id, expected_version))
        result = _get(conn, operation_id)
        _event(conn, result, 'result', now, receipt)
        return result
