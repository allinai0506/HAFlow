"""First-node dispatch transactions in the existing workflow recovery table."""
import json
import time

from . import node_dispatch as core, recovery_store as rs


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


def reconcile_workflow(db_path, wid, *, legacy_notified=(), discover=True, now=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        workflow, config, tasks = rs._snapshot(conn, wid)
        current_tasks = [t for t in tasks if not t.get('execution_id')
                         or t.get('execution_id') == workflow.get('execution_id')]
        if discover and core.active(workflow) and not current_tasks:
            for node in config.get('nodes') or []:
                if (not node.get('id') or node.get('depends_on') or node.get('node_type', 'agent') != 'agent'):
                    continue
                facts = core.payload(workflow, config, node)
                legacy = node['id'] in legacy_notified
                detail = ({'reason': 'legacy_dispatch_unknown', 'origin': 'legacy_notified',
                           'deadline_at': now + core.WAIT_SECONDS} if legacy else {})
                cur = conn.execute('''INSERT INTO workflow_recovery_operations
                    (identity_key,workflow_id,payload_json,status,next_due_at,started,detail_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO NOTHING''',
                    (facts['identity_key'], wid, json.dumps(facts), 'awaiting_result' if legacy else 'pending',
                     now if not legacy else now + core.CHECK_SECONDS, int(legacy), json.dumps(detail), now, now))
                if cur.rowcount:
                    row = conn.execute('SELECT id FROM workflow_recovery_operations WHERE identity_key=?', (facts['identity_key'],)).fetchone()
                    rs._event(conn, rs._get(conn, row['id']), 'dispatch_registered', now, detail)
        for op in _operations(conn, wid):
            if op['status'] in core.TERMINAL:
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
        workflow, config = rs._workflow_snapshot(conn, wid)
        node = next((n for n in config.get('nodes') or [] if n.get('id') == node_id), None)
        if not node:
            return None
        identity = core.payload(workflow, config, node)['identity_key']
        row = conn.execute('SELECT * FROM workflow_recovery_operations WHERE identity_key=?', (identity,)).fetchone()
        return rs._decode(row) if row else None
    finally:
        conn.close()


def claim(db_path, operation_id, owner, now=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._get(conn, operation_id)
        workflow, config, _ = rs._snapshot(conn, op['workflow_id'])
        if (not owner or not core.active(workflow) or not core.current(op, workflow, config)
                or op['status'] != 'pending' or op['started'] or op['next_due_at'] > now or op['attempts'] >= 3):
            return None
        return _write(conn, op, 'running', {}, now, owner=owner, lease=now + 660, next_due=now + 660,
                      attempts=op['attempts'] + 1)


def start(db_path, operation_id, owner, now=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._owned(conn, operation_id, owner, now)
        workflow, config, _ = rs._snapshot(conn, op['workflow_id'])
        if not core.active(workflow) or not core.current(op, workflow, config) or op['started']:
            raise ValueError('dispatch no longer owns current unsent generation')
        return _write(conn, op, 'running', {'sent_at': now, 'deadline_at': now + core.WAIT_SECONDS},
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
        workflow, config, tasks = rs._snapshot(conn, op['workflow_id'])
        outcome = core.result(op, workflow, config, tasks, now)
        if outcome:
            return _write(conn, op, *outcome, now)
        return op


def defer(db_path, operation_id, reason, now=None, owner=None):
    now = time.time() if now is None else now
    with rs._transaction(db_path) as conn:
        op = rs._get(conn, operation_id)
        workflow, config, _ = rs._snapshot(conn, op['workflow_id'])
        held = op['status'] == 'running' and owner and op['owner'] == owner and op['lease_until'] > now
        if (op['started'] or (not held and (op['status'] != 'pending' or op['next_due_at'] > now))
                or not core.active(workflow) or not core.current(op, workflow, config)):
            return op
        attempts = op['attempts'] if held else op['attempts'] + 1
        return _write(conn, op, 'waiting_human' if attempts >= 3 else 'pending',
                      {'reason': reason, 'decision_needed': '检查总指挥工位与需求，处理前置问题后重试'},
                      now, next_due=now + core.CHECK_SECONDS, attempts=attempts)


def validate_launch(conn, operation_id, workflow_id, node_id, now):
    op = rs._get(conn, operation_id)
    workflow, config, _ = rs._snapshot(conn, workflow_id)
    if (op['payload'].get('kind') != 'node_dispatch' or op['workflow_id'] != workflow_id
            or op['payload']['node_id'] != node_id or not workflow.get('execution_id')
            or not core.active(workflow) or not core.current(op, workflow, config)
            or not op['started'] or op['status'] not in {'running', 'awaiting_result', 'resolved', 'waiting_human'}
            or op['detail'].get('deadline_at', 0) <= now or op['detail'].get('origin') == 'legacy_notified'):
        raise ValueError('dispatch operation does not authorize this launch')
    return op, workflow


def record_intent(db_path, intent, now):
    from . import state_db
    with rs._transaction(db_path) as conn:
        _, workflow = validate_launch(conn, intent['dispatch_operation_id'], intent['workflow_id'], intent['node_id'], now)
        if intent.get('execution_id') != workflow['execution_id'] or not intent['resources'].get('run_id'):
            raise ValueError('dispatch launch intent requires current execution and run identity')
        state_db.record_event({'event_type': 'launch_intent', 'payload': intent,
            'workflow_id': intent['workflow_id'], 'node_id': intent['node_id'], 'task_id': intent['key'],
            'source': 'launch', 'timestamp': now}, conn=conn)


def validate_task_registration(conn, task, now):
    operation_id = task.get('dispatch_operation_id')
    if operation_id is None:
        return
    _, workflow = validate_launch(conn, operation_id, task['workflow_id'], task.get('node') or task.get('stage'), now)
    if not task.get('run_id') or task.get('execution_id') != workflow.get('execution_id'):
        raise ValueError('dispatch task requires current run and execution identity')
    row = conn.execute("SELECT payload_json FROM events WHERE workflow_id=? AND event_type='launch_intent' AND source='launch' AND json_extract(payload_json,'$.intent_id')=? ORDER BY id DESC LIMIT 1",
                       (task['workflow_id'], task.get('launch_intent_id'))).fetchone()
    intent = json.loads(row['payload_json']) if row else {}
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
        workflow, config = rs._workflow_snapshot(conn, wid)
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
            'run_id': intent['resources']['run_id']}
