"""Node dispatch transactions in the existing workflow recovery table."""
import json
import time

from . import node_dispatch as core, recovery_store as rs
from .direct_dispatch import lineage_redispatch_candidates


def _write(conn, op, status, detail, now, *, next_due=None, owner=None, lease=None, started=None, attempts=None):
    receipt = {**op['detail'], **detail}
    conn.execute('''UPDATE workflow_recovery_operations SET status=?,detail_json=?,
        next_due_at=?,owner=?,lease_until=?,started=?,attempts=?,version=version+1,updated_at=? WHERE id=?''',
        (status, json.dumps(receipt, ensure_ascii=False), next_due, owner, lease,
         op['started'] if started is None else started,
         op['attempts'] if attempts is None else attempts, now, op['id']))
    result = rs._get(conn, op['id'])
    rs._event(conn, result, 'dispatch_' + status, now, receipt)
    return result


def _operations(conn, wid):
    return [rs._decode(r) for r in conn.execute(
        "SELECT * FROM workflow_recovery_operations WHERE workflow_id=? AND identity_key LIKE 'node_dispatch:%' AND status NOT IN ('resolved','superseded') ORDER BY id DESC LIMIT 1000", (wid,))]


def _node_operation(conn, workflow, config, node):
    key = core.payload(workflow, config, node)['identity_key']
    row = conn.execute("""SELECT * FROM workflow_recovery_operations
        WHERE identity_key=? OR (workflow_id=? AND identity_key LIKE 'node_dispatch:%'
            AND json_extract(payload_json,'$.generation_key')=?) ORDER BY id DESC LIMIT 1""",
        (key, workflow['workflow_id'], key)).fetchone()
    return rs._decode(row) if row else None


def _prior_unresolved(conn, workflow_id, node_id, *, excluding_key):
    return conn.execute(
        "SELECT id FROM workflow_recovery_operations WHERE workflow_id=? "
        "AND identity_key LIKE 'node_dispatch:%' AND json_extract(payload_json,'$.node_id')=? "
        "AND identity_key!=? AND status NOT IN ('resolved','superseded') AND started=1 LIMIT 1",
        (workflow_id, node_id, excluding_key)).fetchone()


def _prior_delivery_unknown(conn, op):
    return _prior_unresolved(conn, op['workflow_id'], op['payload']['node_id'],
                             excluding_key=op['identity_key']) is not None


def _snapshot(conn, wid):
    """Upstream identity and reuse are read in the same transaction as the lease."""
    from . import scheduler, scheduler_facts, reverification
    workflow, config, tasks = rs._snapshot(conn, wid)
    facts = []
    for node_id in scheduler.verifier_branch_node_ids(config):
        fact = scheduler_facts.find_reuse_fact(wid, node_id, workflow.get('candidate_sha'),
            policy_identity=reverification.policy_identity(reverification.policy_from_workflow(config)),
            episode_id=workflow.get('candidate_episode_id'), conn=conn)
        if fact:
            facts.append(fact)
    workflow['_dispatch_reuse_facts'] = facts
    workflow['_dispatch_dependency_runs'] = {
        node['id']: sorted(({'task_id': t['task_id'], 'run_id': t.get('run_id')}
            for t in tasks if (t.get('node') or t.get('stage')) == node['id']
            and t.get('status') != 'superseded' and not t.get('superseded_by')
            and (not t.get('execution_id') or t['execution_id'] == workflow.get('execution_id'))),
            key=lambda t: t['task_id']) or [
                {'reuse_event_id': f['event_id']} for f in facts if f['verifier'] == node['id']]
        for node in config.get('nodes') or []}
    return workflow, config, tasks


def reconcile_workflow(db_path, wid, *, legacy_notified=(), discover=True, intake_enabled=True, now=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        workflow, config, tasks = _snapshot(conn, wid)
        current_tasks = [t for t in tasks if not t.get('execution_id')
                         or t.get('execution_id') == workflow.get('execution_id')]
        if discover and core.active(workflow):
            for node in config.get('nodes') or []:
                if (not node.get('id') or node.get('node_type', 'agent') != 'agent'
                        or (not node.get('depends_on') and not intake_enabled)
                        or not core.dependencies_ready(workflow, config, node, current_tasks)
                        or core.verification_reused(workflow, config, node['id'], current_tasks)):
                    continue
                previous = _node_operation(conn, workflow, config, node)
                node_tasks = [t for t in current_tasks if (t.get('node') or t.get('stage')) == node['id']]
                candidates = lineage_redispatch_candidates(node_tasks)
                predecessors = None
                if candidates and (not previous or previous['status'] in core.TERMINAL):
                    predecessors = sorted(({'task_id': t['task_id'], 'run_id': t.get('run_id')}
                                           for t in candidates), key=lambda t: t['task_id'])
                elif node_tasks and (not node.get('depends_on') or not core.cancelled_inventory(node_tasks)):
                    continue
                prior_id = previous['id'] if previous and previous['status'] in core.TERMINAL else None
                facts = core.payload(workflow, config, node, predecessors, prior_id)
                legacy = not predecessors and node['id'] in legacy_notified
                detail = ({'reason': 'legacy_dispatch_unknown', 'origin': 'legacy_notified',
                           'deadline_at': now + core.WAIT_SECONDS} if legacy else {})
                status, started = ('awaiting_result', 1) if legacy else ('pending', 0)
                if core.cancelled_inventory(node_tasks):
                    status, started = 'waiting_human', 0
                    detail = {'reason': 'dispatch_scope_cancelled',
                              'decision_needed': '该节点任务已明确取消补派；确认剩余验收范围'}
                prior_unknown = _prior_unresolved(conn, wid, node['id'], excluding_key=facts['identity_key'])
                if prior_unknown:
                    status, started = 'waiting_human', 0
                    detail = {'reason': 'dispatch_prior_delivery_unknown',
                              'prior_operation_id': prior_unknown['id'],
                              'decision_needed': '旧派发已开始且交付未明确，先核验，禁止重叠派发'}
                cur = conn.execute('''INSERT INTO workflow_recovery_operations
                    (identity_key,workflow_id,payload_json,status,next_due_at,started,detail_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO NOTHING''',
                    (facts['identity_key'], wid, json.dumps(facts), status,
                     now if not legacy else now + core.CHECK_SECONDS, started, json.dumps(detail), now, now))
                if cur.rowcount:
                    row = conn.execute('SELECT id FROM workflow_recovery_operations WHERE identity_key=?', (facts['identity_key'],)).fetchone()
                    rs._event(conn, rs._get(conn, row['id']), 'dispatch_registered', now, detail)
        for op in _operations(conn, wid):
            if op['status'] in core.TERMINAL:
                continue
            if (not intake_enabled and not op['payload'].get('dependency_runs') and not op['started'] and op['status'] in {'pending', 'running'}
                    and core.current(op, workflow, config)):
                _write(conn, op, 'superseded', {'reason': 'dispatch_direct_mode',
                    'responsibility_transferred_to': 'direct_scheduler'}, now)
                continue
            if op['status'] == 'waiting' and (op['next_due_at'] or 0) > now and core.current(op, workflow, config):
                continue
            outcome = core.result(op, workflow, config, tasks, now)
            if outcome:
                status, detail = outcome
                if op['status'] != status or any(op['detail'].get(k) != v for k, v in detail.items()):
                    _write(conn, op, status, detail, now)
                continue
            if not core.active(workflow):
                continue
            if op['status'] == 'running' and op['lease_until'] <= now:
                if op['started']:
                    _write(conn, op, 'awaiting_result', {'reason': 'dispatch_delivery_unknown'}, now,
                           next_due=now + core.CHECK_SECONDS)
                else:
                    _write(conn, op, 'waiting_human' if op['attempts'] >= 3 else 'pending',
                           {'reason': 'dispatch_lease_expired'}, now, next_due=now)
            elif op['status'] == 'pending' and op['started']:
                # Human verify is read-only; never put a started operation back on transport.
                _write(conn, op, 'awaiting_result', {'reason': 'dispatch_verifying'}, now, next_due=now)
            elif op['status'] == 'awaiting_result' and (op['next_due_at'] or 0) <= now:
                conn.execute('UPDATE workflow_recovery_operations SET next_due_at=? WHERE id=?', (now + core.CHECK_SECONDS, op['id']))
        return _operations(conn, wid)


def operation_for_node(db_path, wid, node_id):
    from . import state_db
    conn = state_db.get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        if not conn.execute('SELECT 1 FROM workflows WHERE workflow_id=?', (wid,)).fetchone():
            return None
        workflow, config, _ = _snapshot(conn, wid)
        node = next((n for n in config.get('nodes') or [] if n.get('id') == node_id), None)
        if not node:
            return None
        return _node_operation(conn, workflow, config, node)
    finally:
        conn.close()


def claim(db_path, operation_id, owner, now=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._get(conn, operation_id)
        workflow, config, tasks = _snapshot(conn, op['workflow_id'])
        outcome = core.result(op, workflow, config, tasks, now)
        if outcome:
            _write(conn, op, *outcome, now)
            return None
        if (not owner or not core.active(workflow) or not core.current(op, workflow, config)
                or not core.predecessors_current(op, workflow, tasks) or not core.ready(op, workflow, config, tasks)
                or _prior_delivery_unknown(conn, op)
                or op['status'] != 'pending' or op['started'] or op['next_due_at'] > now or op['attempts'] >= 3):
            return None
        return _write(conn, op, 'running', {}, now, owner=owner, lease=now + 660, next_due=now + 660,
                      attempts=op['attempts'] + 1)


def start(db_path, operation_id, owner, now=None, *, expected_task_ids=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._owned(conn, operation_id, owner, now)
        workflow, config, tasks = _snapshot(conn, op['workflow_id'])
        outcome = core.result(op, workflow, config, tasks, now)
        if outcome and outcome[1].get('reason') in {'dispatch_existing_tasks', 'dispatch_verification_reused'}:
            _write(conn, op, *outcome, now)
            return None
        if (not core.active(workflow) or not core.current(op, workflow, config) or op['started']
                or not core.predecessors_current(op, workflow, tasks) or not core.ready(op, workflow, config, tasks)
                or _prior_delivery_unknown(conn, op)):
            raise ValueError('dispatch no longer owns current unsent generation')
        detail = {'sent_at': now, 'deadline_at': now + core.WAIT_SECONDS}
        if expected_task_ids is not None:
            if not core._valid_required(expected_task_ids) or len(set(expected_task_ids)) != len(expected_task_ids):
                raise ValueError('direct dispatch requires a bounded unique task inventory')
            detail['expected_task_ids'] = expected_task_ids
        return _write(conn, op, 'running', detail,
                      now, owner=owner, lease=now + 660, next_due=now + core.CHECK_SECONDS, started=1)


def transport_finished(db_path, operation_id, owner, *, reason, now=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._get(conn, operation_id)
        # A simultaneous scanner may have resolved this operation from real registration.
        if op['status'] in core.TERMINAL or op['status'] == 'waiting_human':
            return op
        if op['owner'] != owner or op['status'] != 'running' or not op['started']:
            return op
        op = _write(conn, op, 'awaiting_result', {'reason': reason}, now, next_due=now)
        workflow, config, tasks = _snapshot(conn, op['workflow_id'])
        outcome = core.result(op, workflow, config, tasks, now)
        if outcome:
            return _write(conn, op, *outcome, now)
        return op


def direct_finished(db_path, operation_id, owner, attempted_task_ids, now=None):
    """Only durable resources_absent receipts can authorize another direct attempt."""
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._get(conn, operation_id)
        if op['status'] in core.TERMINAL or op['status'] == 'waiting_human':
            return op
        if op['owner'] != owner or op['status'] != 'running' or not op['started']:
            return op
        workflow, config, tasks = _snapshot(conn, op['workflow_id'])
        absent = bool(attempted_task_ids) and not core.registered_tasks(op, workflow, tasks)
        for task_id in attempted_task_ids:
            row = conn.execute("SELECT payload_json FROM events WHERE workflow_id=? "
                "AND event_type='launch_intent' AND source='launch' "
                "AND json_extract(payload_json,'$.dispatch_operation_id')=? "
                "AND json_extract(payload_json,'$.task_id')=? ORDER BY id DESC LIMIT 1",
                (op['workflow_id'], op['id'], task_id)).fetchone()
            intent = json.loads(row['payload_json']) if row else {}
            absent = absent and intent.get('phase') == 'resources_absent'
        if absent and core.current(op, workflow, config) and core.ready(op, workflow, config, tasks):
            return _write(conn, op, 'waiting_human' if op['attempts'] >= 3 else 'pending',
                {'reason': 'dispatch_resources_absent', 'deadline_at': None,
                 'decision_needed': '资源已确认未创建；修正派发前置条件后可重试'},
                now, next_due=now + core.CHECK_SECONDS, started=0)
        outcome = core.result(op, workflow, config, tasks, now)
        if outcome:
            return _write(conn, op, *outcome, now)
        return _write(conn, op, 'awaiting_result', {'reason': 'dispatch_delivery_unknown'},
                      now, next_due=now + core.CHECK_SECONDS)


def defer(db_path, operation_id, reason, now=None, owner=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._get(conn, operation_id)
        workflow, config, _ = _snapshot(conn, op['workflow_id'])
        held = op['status'] == 'running' and owner and op['owner'] == owner and op['lease_until'] > now
        if (op['started'] or (not held and (op['status'] != 'pending' or op['next_due_at'] > now))
                or not core.active(workflow) or not core.current(op, workflow, config)):
            return op
        attempts = op['attempts'] if held else op['attempts'] + 1
        return _write(conn, op, 'waiting_human' if attempts >= 3 else 'pending',
                      {'reason': reason, 'decision_needed': '检查总指挥工位与需求，处理前置问题后重试'},
                      now, next_due=now + core.CHECK_SECONDS, attempts=attempts)


def can_launch_with_replacement(op, intent, tasks):
    """Pure admission: an explicit --supersedes replacement may reuse its operation."""
    replacement = (intent or {}).get('supersedes')
    if not replacement:
        return False
    if op.get('status') in core.TERMINAL:
        return False
    old = next((t for t in tasks or [] if t.get('task_id') == replacement), None)
    if not old or old.get('status') != 'superseded':
        return False
    if (old.get('node') or old.get('stage')) != op['payload'].get('node_id'):
        return False
    return True


def _launch_refusals(conn, op, workflow, config, tasks, node_id, now, *, allow_unstarted):
    """Enumerate precise refusal reasons so recovery does not have to guess."""
    node = next((n for n in config.get('nodes') or [] if n.get('id') == node_id), None)
    latest = _node_operation(conn, workflow, config, node) if node else None
    reasons = []
    if op['payload'].get('kind') != 'node_dispatch':
        reasons.append('operation kind is not node_dispatch')
    if op['workflow_id'] != workflow.get('workflow_id'):
        reasons.append('operation belongs to a different workflow')
    if op['payload'].get('node_id') != node_id:
        reasons.append('operation targets a different node')
    if not workflow.get('execution_id'):
        reasons.append('workflow execution identity missing')
    if not core.active(workflow):
        reasons.append('workflow is not active')
    if not core.current(op, workflow, config):
        reasons.append('operation generation is stale')
    if not latest or latest['id'] != op['id']:
        suffix = f" (current={latest['id']})" if latest else ''
        reasons.append('operation is not the current node operation' + suffix)
    if not core.predecessors_current(op, workflow, tasks):
        reasons.append('operation predecessors changed')
    if not core.ready(op, workflow, config, tasks):
        reasons.append('node is not ready for dispatch')
    if _prior_delivery_unknown(conn, op):
        reasons.append('a prior dispatch delivery is unresolved')
    if op['started'] and op['detail'].get('deadline_at', 0) <= now:
        reasons.append('operation deadline expired')
    if op['detail'].get('origin') == 'legacy_notified':
        reasons.append('legacy operation must be reconciled, not launched')
    if not allow_unstarted:
        if not op['started']:
            reasons.append('operation was never started')
        if op['status'] not in {'running', 'awaiting_result', 'resolved', 'waiting_human'}:
            reasons.append(f"operation status {op['status']} does not authorize a launch")
    return reasons


def validate_launch(conn, operation_id, workflow_id, node_id, now, *, intent=None, tasks_view=None):
    op = rs._get(conn, operation_id)
    workflow, config, tasks = _snapshot(conn, workflow_id)
    if tasks_view is not None:
        tasks = tasks_view
    allow_unstarted = can_launch_with_replacement(op, intent, tasks)
    reasons = _launch_refusals(conn, op, workflow, config, tasks, node_id, now,
                               allow_unstarted=allow_unstarted)
    if reasons:
        raise ValueError('dispatch operation does not authorize this launch: ' + '; '.join(reasons))
    return op, workflow


def record_intent(db_path, intent, now):
    from . import state_db
    with rs._transaction(db_path) as conn:
        op, workflow = validate_launch(conn, intent['dispatch_operation_id'], intent['workflow_id'], intent['node_id'], now, intent=intent)
        if intent.get('execution_id') != workflow['execution_id'] or not intent['resources'].get('run_id'):
            raise ValueError('dispatch launch intent requires current execution and run identity')
        if op['payload'].get('candidate_sha') and intent.get('candidate_sha') != op['payload']['candidate_sha']:
            raise ValueError('dispatch intent candidate does not match current episode')
        predecessors = op['payload'].get('predecessors') or []
        if predecessors and intent.get('supersedes') not in {p['task_id'] for p in predecessors}:
            raise ValueError('replacement dispatch requires its predecessor')
        state_db.record_event({'event_type': 'launch_intent', 'payload': intent,
            'workflow_id': intent['workflow_id'], 'node_id': intent['node_id'], 'task_id': intent['key'],
            'source': 'launch', 'timestamp': now}, conn=conn)


def validate_task_registration(conn, task, now):
    operation_id = task.get('dispatch_operation_id')
    if operation_id is None:
        return
    op, workflow = validate_launch(conn, operation_id, task['workflow_id'],
                                   task.get('node') or task.get('stage'), now,
                                   intent={'supersedes': task.get('supersedes')})
    if op['payload'].get('candidate_sha') and task.get('candidate_sha') != op['payload']['candidate_sha']:
        raise ValueError('dispatch task candidate does not match current episode')
    if not task.get('run_id') or task.get('execution_id') != workflow.get('execution_id'):
        raise ValueError('dispatch task requires current run and execution identity')
    row = conn.execute("SELECT payload_json FROM events WHERE workflow_id=? AND event_type='launch_intent' AND source='launch' AND json_extract(payload_json,'$.intent_id')=? ORDER BY id DESC LIMIT 1",
                       (task['workflow_id'], task.get('launch_intent_id'))).fetchone()
    intent = json.loads(row['payload_json']) if row else {}
    predecessors = op['payload'].get('predecessors') or []
    if predecessors and (task.get('supersedes') != intent.get('supersedes')
                         or task.get('supersedes') not in {p['task_id'] for p in predecessors}):
        raise ValueError('replacement task requires matching predecessor intent')
    if (intent.get('dispatch_operation_id') != operation_id or intent.get('task_id') != task['task_id']
            or intent.get('node_id') != (task.get('node') or task.get('stage'))
            or (intent.get('resources') or {}).get('run_id') != task['run_id']
            or intent.get('execution_id') != workflow['execution_id'] or intent.get('phase') == 'resources_absent'):
        raise ValueError('dispatch task is not bound to a matching durable launch intent')


def read_wait_projection(db_path, wid, now):
    """Read both identity and obligation from one snapshot; never initialize a DB."""
    from . import state_db
    if not db_path.is_file():
        return None
    conn = state_db.get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_recovery_operations'").fetchone():
            return None
        workflow, config, _ = _snapshot(conn, wid)
        if not core.active(workflow):
            return None
        operations = [op for op in _operations(conn, wid)
                      if op['status'] not in core.TERMINAL and core.current(op, workflow, config)]
        operations.sort(key=lambda op: (op['status'] != 'waiting_human', op['id']))
        return core.wait_projection(operations[0], now) if operations else None
    finally:
        conn.close()


def recover_registration_identity(conn, workflow_id, task_id):
    """Preserve dispatch identity when repairing a damaged legacy Task payload."""
    row = conn.execute("""SELECT payload_json FROM events WHERE workflow_id=?
        AND event_type='launch_intent' AND source='launch' AND json_valid(payload_json)
        AND json_extract(payload_json,'$.task_id')=?
        AND json_extract(payload_json,'$.dispatch_operation_id') IS NOT NULL
        ORDER BY id DESC LIMIT 1""", (workflow_id, task_id)).fetchone()
    if not row:
        return {}
    intent = json.loads(row['payload_json'])
    return {'dispatch_operation_id': intent['dispatch_operation_id'],
            'launch_intent_id': intent['intent_id'], 'execution_id': intent['execution_id'],
            'run_id': intent['resources']['run_id'], 'supersedes': intent.get('supersedes')}
