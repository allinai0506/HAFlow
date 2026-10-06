"""Evidence-backed, CAS guarded identity/config repair; never guesses a Run.

The reviewable plan imports legacy definition bytes once. Apply and rollback
use the existing state transaction and retain every historical verdict/event.
"""
import hashlib
import json
from pathlib import Path

from . import recovery_store, state_db
from .workflow_progress import _generation, _hash, recovery_identity


def _rows(conn, workflow_id):
    workflow = conn.execute('SELECT * FROM workflows WHERE workflow_id=?', (workflow_id,)).fetchone()
    if workflow is None:
        raise ValueError('workflow not found')
    tasks = [dict(r) for r in conn.execute('SELECT * FROM tasks WHERE workflow_id=? ORDER BY task_id LIMIT 10001', (workflow_id,))]
    operations = [dict(r) for r in conn.execute('SELECT * FROM workflow_recovery_operations WHERE workflow_id=? ORDER BY id LIMIT 1001', (workflow_id,))]
    if len(tasks) > 10000 or len(operations) > 1000:
        raise ValueError('migration row budget exhausted')
    result = {'workflow': dict(workflow), 'tasks': tasks, 'operations': operations,
              'event_watermark': conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE workflow_id=?', (workflow_id,)).fetchone()[0]}
    if len(json.dumps(result).encode()) > 8 * 1024 * 1024:
        raise ValueError('migration byte budget exhausted')
    return result


def _definition(workflow):
    path = Path(workflow.get('workflow_file') or '').expanduser().resolve()
    with path.open('rb') as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError('configuration exceeds migration budget')
    from .workflow import normalize_workflow, yaml
    data = yaml.safe_load(raw) if path.suffix.lower() in ('.yaml', '.yml') and yaml else json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError('configuration must be an object')
    config = normalize_workflow(data)
    if not config.get('nodes'):
        raise ValueError('configuration nodes unknown')
    return config, {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def _plan(conn, workflow_id):
    before = _rows(conn, workflow_id)
    workflow, config, tasks = recovery_store._snapshot(conn, workflow_id)
    epoch = max(float(workflow.get('created_at') or 0), float(workflow.get('reopened_at') or 0))
    generations = {t['execution_id'] for t in tasks if t.get('execution_id') and float(t.get('created_at') or 0) >= epoch}
    if workflow.get('execution_id'):
        generations.add(workflow['execution_id'])
    if len(generations) != 1:
        raise ValueError('unique generation evidence unavailable')
    execution_id = next(iter(generations))
    evidence, changed = {}, []
    for task in tasks:
        if task.get('execution_id'):
            if task['execution_id'] != execution_id and task.get('status') != 'superseded' and not task.get('superseded_by'):
                raise ValueError('active task generation conflict')
            continue
        if float(task.get('created_at') or 0) < epoch or not task.get('run_id'):
            raise ValueError('task outside generation or Run unknown')
        receipts = conn.execute("SELECT * FROM events WHERE workflow_id=? AND task_id=? AND run_id=? AND node_id=? AND source='herdr-task' AND event_type IN ('initial_dispatched','rework_dispatched') AND timestamp>=? ORDER BY id DESC LIMIT 100",
            (workflow_id, task['task_id'], task['run_id'], task.get('node') or task.get('stage'), epoch)).fetchall()
        initial = conn.execute("SELECT id,payload_json FROM events WHERE workflow_id=? AND task_id=? AND run_id=? AND node_id=? AND source='herdr-task' AND event_type='initial_dispatched' AND timestamp>=? ORDER BY id DESC LIMIT 1",
            (workflow_id, task['task_id'], task['run_id'], task.get('node') or task.get('stage'), epoch)).fetchone()
        initial_payload = json.loads(initial['payload_json']) if initial else {}
        if not initial or initial_payload.get('delivery_phase') != 'dispatched' or not initial_payload.get('intervention_id'):
            raise ValueError('initial Run delivery receipt missing: ' + task['task_id'])
        accepted = []
        for row in receipts:
            payload = json.loads(row['payload_json'] or '{}')
            if (payload.get('delivery_phase') == 'dispatched' and payload.get('intervention_id')
                    and task.get('completion_epoch') and payload.get('completion_epoch') == task['completion_epoch']
                    and task.get('completion_identity_path') and payload.get('identity_path') == task['completion_identity_path']
                    and (row['event_type'] == 'initial_dispatched' or
                         (task.get('rework_delivery') == 'delivered' and payload['intervention_id'] == task.get('rework_request_id')))):
                accepted.append(row['id'])
        if not accepted:
            raise ValueError('current epoch delivery receipt missing: ' + task['task_id'])
        changed.append(task['task_id'])
        evidence[task['task_id']] = {'run_id': task['run_id'], 'initial_event_id': initial['id'], 'event_ids': accepted,
                                   'completion_epoch': task['completion_epoch'],
                                   'identity_path': task['completion_identity_path']}
    finalize_clears = {}
    from .scheduler import resolve_candidate_sha_for_branch
    for task in tasks:
        if task.get('finalize_escalated') and task.get('status') in {'integrated', 'cleanup_ready', 'cleaned'}:
            ref = task.get('integration_ref')
            sha = task.get('integrated_commit')
            if (ref == f"refs/herdr/tasks/{task['task_id']}" and sha and task.get('source_repo')
                    and sha == task.get('commit')
                    and resolve_candidate_sha_for_branch(task['source_repo'], ref) == sha):
                finalize_clears[task['task_id']] = {'source_repo': task['source_repo'], 'ref': ref, 'sha': sha,
                                                  'run_id': task.get('run_id')}
    file_proof = None
    if not config.get('nodes'):
        if config:
            raise ValueError('legacy configuration is not empty; explicit configuration repair required')
        config, file_proof = _definition(workflow)
    return {'workflow_id': workflow_id, 'fingerprint': _hash(before), 'execution_id': execution_id,
            'task_ids': sorted(changed), 'evidence': evidence, 'config': config, 'file': file_proof,
            'finalize_clears': finalize_clears}


def _public_plan(prepared):
    result = {k: v for k, v in prepared.items() if k != 'config'}
    result['configuration_sha256'] = _hash(prepared['config'])
    result['configuration_nodes'] = [n.get('id') for n in prepared['config'].get('nodes', [])]
    return result


def _prepare(db_path, workflow_id):
    conn = state_db.get_readonly_db_connection(db_path)
    try:
        conn.execute('BEGIN')
        return _plan(conn, workflow_id)
    finally:
        conn.close()


def plan(db_path, workflow_id):
    return _public_plan(_prepare(db_path, workflow_id))


def _field_before(value, key):
    return {'present': key in value, 'value': value.get(key)}


def _restore_field(value, key, before):
    if before['present']:
        value[key] = before['value']
    else:
        value.pop(key, None)


def apply(db_path, reviewed_plan):
    wid = reviewed_plan['workflow_id']
    # Git/file verification runs without a SQLite write lock. Only the frozen
    # bytes in this preparation are imported after the database CAS below.
    conn = state_db.get_readonly_db_connection(db_path)
    try:
        if _hash(_rows(conn, wid)) != reviewed_plan.get('fingerprint'):
            raise ValueError('migration snapshot changed')
    finally:
        conn.close()
    current = _prepare(db_path, wid)
    if _public_plan(current) != reviewed_plan:
        raise ValueError('migration evidence/config/snapshot changed')
    with recovery_store._transaction(db_path) as conn:
        before = _rows(conn, wid)
        if _hash(before) != reviewed_plan.get('fingerprint'):
            raise ValueError('migration snapshot changed')
        workflow, _, tasks = recovery_store._snapshot(conn, wid)
        workflow['execution_id'] = current['execution_id']
        meta = json.loads(before['workflow']['metadata_json'] or '{}')
        undo = {'workflow': {'execution_id': _field_before(meta, 'execution_id'),
                             'config_json': before['workflow']['config_json'] if current['file'] else None},
                'tasks': {}, 'operations': {}}
        meta['execution_id'] = current['execution_id']
        conn.execute('UPDATE workflows SET metadata_json=?,config_json=? WHERE workflow_id=?',
                     (json.dumps(meta), json.dumps(current['config']), wid))
        for row in before['tasks']:
            tid = row['task_id']
            if tid in current['task_ids'] or tid in current['finalize_clears']:
                task_meta = json.loads(row['payload_json'] or '{}')
                change = {'version': row['version']}
                if tid in current['task_ids']:
                    change['execution_id'] = _field_before(task_meta, 'execution_id')
                    task_meta['execution_id'] = current['execution_id']
                if tid in current['finalize_clears']:
                    change['finalize_escalated'] = _field_before(task_meta, 'finalize_escalated')
                    task_meta['finalize_escalated'] = False
                    # Retain the historical reason; do not duplicate it in audit.
                undo['tasks'][tid] = change
                conn.execute('UPDATE tasks SET payload_json=?,version=version+1 WHERE task_id=?', (json.dumps(task_meta), tid))
        by_id = {t['task_id']: t for t in tasks}
        occupied, replacements = set(), []
        for row in before['operations']:
            payload, detail = json.loads(row['payload_json']), json.loads(row['detail_json'])
            change = {'identity_key': row['identity_key'], 'version': row['version'],
                      'identity': _field_before(payload, 'identity'),
                      'payload_identity_key': _field_before(payload, 'identity_key'),
                      'detail_execution_id': _field_before(detail, 'execution_id'),
                      'fact_execution_ids': [], 'added_identity_migration': 'identity_migration' not in detail}
            if detail.get('execution_id') not in (None, '', current['execution_id']):
                raise ValueError('operation generation conflict')
            old_identity = payload.get('identity')
            for index, fact in enumerate(payload.get('facts') or []):
                task = by_id.get(fact.get('task_id'))
                if not task or task.get('run_id') != fact.get('run_id'):
                    raise ValueError('operation fact Run changed')
                if (task.get('execution_id') not in (None, '', current['execution_id'])
                        or fact.get('execution_id') not in (None, '', current['execution_id'])):
                    raise ValueError('operation generation conflict')
                change['fact_execution_ids'].append({'index': index, 'execution_id': _field_before(fact, 'execution_id')})
                fact['execution_id'] = current['execution_id']
            payload['identity'] = recovery_identity(dict(workflow, candidate_sha=payload.get('candidate_sha')), payload.get('facts') or [])
            key = _hash({'workflow_id': wid, 'generation': _generation(workflow),
                         'candidate_sha': payload.get('candidate_sha'), 'retry_node': payload.get('retry_node'), 'kind': payload.get('kind')})
            if key in occupied:
                raise ValueError('migration operation identity collision')
            occupied.add(key)
            payload['identity_key'] = key
            detail['execution_id'] = current['execution_id']
            detail.setdefault('identity_migration', {'old_identity': old_identity, 'old_identity_key': row['identity_key']})
            undo['operations'][str(row['id'])] = change
            replacements.append((row['id'], key, payload, detail))
        for op_id, _, _, _ in replacements:
            conn.execute('UPDATE workflow_recovery_operations SET identity_key=? WHERE id=?', ('migration:' + str(op_id), op_id))
        for op_id, key, payload, detail in replacements:
            conn.execute('UPDATE workflow_recovery_operations SET identity_key=?,payload_json=?,detail_json=?,version=version+1 WHERE id=?',
                         (key, json.dumps(payload), json.dumps(detail), op_id))
        # Whitelisted inverse fields contain no notes/prompts/secret-bearing
        # historical payloads. Unchanged fields stay solely in their authority.
        event = state_db.record_event({'event_type': 'workflow_identity_migrated', 'workflow_id': wid,
            'source': 'workflow-repair-migration', 'payload': {'plan': reviewed_plan, 'undo': undo}}, conn=conn)
        after_fingerprint = _hash(_rows(conn, wid))
        conn.execute('UPDATE events SET payload_json=? WHERE id=?',
                     (json.dumps({'plan': reviewed_plan, 'undo': undo, 'after_fingerprint': after_fingerprint}), event['id']))
        return {'workflow_id': wid, 'migration_event_id': event['id'], 'after_fingerprint': after_fingerprint}


def rollback(db_path, receipt):
    wid = receipt['workflow_id']
    with recovery_store._transaction(db_path) as conn:
        if _hash(_rows(conn, wid)) != receipt['after_fingerprint']:
            raise ValueError('migration rollback snapshot changed')
        event = conn.execute("SELECT payload_json FROM events WHERE id=? AND workflow_id=? AND event_type='workflow_identity_migrated' AND source='workflow-repair-migration'",
                             (receipt['migration_event_id'], wid)).fetchone()
        if event is None:
            raise ValueError('migration audit receipt unavailable')
        audit = json.loads(event['payload_json'])
        if audit.get('after_fingerprint') != receipt['after_fingerprint']:
            raise ValueError('migration rollback receipt changed')
        undo = audit['undo']
        row = conn.execute('SELECT metadata_json FROM workflows WHERE workflow_id=?', (wid,)).fetchone()
        meta = json.loads(row['metadata_json'])
        _restore_field(meta, 'execution_id', undo['workflow']['execution_id'])
        conn.execute('UPDATE workflows SET metadata_json=? WHERE workflow_id=?', (json.dumps(meta), wid))
        if undo['workflow']['config_json'] is not None:
            conn.execute('UPDATE workflows SET config_json=? WHERE workflow_id=?', (undo['workflow']['config_json'], wid))
        for tid, change in undo['tasks'].items():
            row = conn.execute('SELECT payload_json FROM tasks WHERE task_id=?', (tid,)).fetchone()
            meta = json.loads(row['payload_json'])
            for field in ('execution_id', 'finalize_escalated'):
                if field in change:
                    _restore_field(meta, field, change[field])
            conn.execute('UPDATE tasks SET payload_json=?,version=? WHERE task_id=?', (json.dumps(meta), change['version'], tid))
        for op_id in undo['operations']:
            conn.execute('UPDATE workflow_recovery_operations SET identity_key=? WHERE id=?', ('rollback:' + op_id, int(op_id)))
        for op_id, change in undo['operations'].items():
            row = conn.execute('SELECT payload_json,detail_json FROM workflow_recovery_operations WHERE id=?', (int(op_id),)).fetchone()
            payload, detail = json.loads(row['payload_json']), json.loads(row['detail_json'])
            _restore_field(payload, 'identity', change['identity'])
            _restore_field(payload, 'identity_key', change['payload_identity_key'])
            _restore_field(detail, 'execution_id', change['detail_execution_id'])
            if change['added_identity_migration']:
                detail.pop('identity_migration', None)
            for fact in change['fact_execution_ids']:
                _restore_field(payload['facts'][fact['index']], 'execution_id', fact['execution_id'])
            conn.execute('UPDATE workflow_recovery_operations SET identity_key=?,payload_json=?,detail_json=?,version=? WHERE id=?',
                         (change['identity_key'], json.dumps(payload), json.dumps(detail), change['version'], int(op_id)))
        state_db.record_event({'event_type': 'workflow_identity_migration_rolled_back', 'workflow_id': wid,
            'source': 'workflow-repair-migration', 'payload': {'migration_fingerprint': receipt['after_fingerprint']}}, conn=conn)
