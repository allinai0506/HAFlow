"""Cockpit I/O and Controller execution boundary. No Console or browser imports.

Claims and receipts live in the authoritative StateStore database. A committed
claim is never replayed: an interrupted external effect remains explicitly
unknown until an operator reconciles it.
"""
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from herdr import recovery_store, state_db
from herdr.controller_actions import collect_workflow_actions, controller_action_type
from herdr.controller_cockpit import build_cockpit_projection
from herdr.controller_receipts import _receipt, classify_receipt
from herdr.controller_workflow_actions import workflow_control_actions
from herdr.workflow_graph import workflow_graph_projection
from herdr.workflow_control_store import WorkflowIdentityChanged as _IdentityChanged, fingerprint
DEPENDENCIES = {}
MAX_PER_TICK = 4
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cockpit')
_inflight = set()
_lock = threading.Lock()
_scan_cursor = 0

def _has_table(conn, name):
    return bool(conn.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', name)).fetchone())


def read_cockpit(store, workflow_id, *, controller=None, errors=None, action_id=None, target_task_id=None):
    """Read graph/action/recovery/event facts from one bounded SQLite snapshot."""
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        conn.execute('BEGIN')
        workflow, config, tasks = recovery_store._snapshot(conn, workflow_id)
        control_fingerprint = fingerprint(conn, workflow_id)
        epoch = conn.execute("SELECT id FROM events WHERE workflow_id=? AND event_type='workflow_transition' AND json_valid(payload_json) "
                             "AND json_extract(payload_json,'$.to_status')=? ORDER BY timestamp DESC,id DESC LIMIT 1",
                             (workflow_id, workflow['status'])).fetchone()
        operations = ([recovery_store._decode(row) for row in conn.execute(
            'SELECT * FROM workflow_recovery_operations WHERE workflow_id=? '
            'ORDER BY CASE WHEN status IN (\'resolved\',\'superseded\') THEN 1 ELSE 0 END,id DESC LIMIT 101',
            (workflow_id,))] if _has_table(conn, 'workflow_recovery_operations') else [])
        events = []
        for row in conn.execute('SELECT id,workflow_id,node_id,task_id,event_type,timestamp,source,'
                                'substr(payload_json,1,16384) AS payload_json FROM events '
                                'WHERE workflow_id=? ORDER BY timestamp DESC,id DESC LIMIT 101', (workflow_id,)):
            event = dict(row)
            raw = event.pop('payload_json') or '{}'
            try:
                event['payload'] = json.loads(raw)
            except ValueError:
                event['payload'] = {'truncated': True}
            events.append(event)
        receipts = ([json.loads(row[0]) for row in conn.execute(
            'SELECT receipt_json FROM cockpit_action_receipts WHERE workflow_id=? ORDER BY updated_at DESC,rowid DESC LIMIT 100',
            (workflow_id,))] if _has_table(conn, 'cockpit_action_receipts') else [])
        checkpoints = [dict(row) for row in conn.execute(
            'SELECT checkpoint_id,tag FROM checkpoints WHERE workflow_id=? ORDER BY created_at DESC LIMIT 101',
            (workflow_id,))]
        # Exact execution lookup must also escape recovery/checkpoint display caps.
        if action_id and action_id.startswith(workflow_id + ':confirm_dispatch:'):
            suffix = action_id.removeprefix(workflow_id + ':confirm_dispatch:')
            if suffix.isdigit():
                row = conn.execute('SELECT * FROM workflow_recovery_operations WHERE workflow_id=? AND id=?',
                    (workflow_id, int(suffix))).fetchone()
                if row and not any(op['id'] == row['id'] for op in operations):
                    operations.append(recovery_store._decode(row))
        if action_id and action_id.startswith(workflow_id + ':restore_checkpoint:'):
            checkpoints = [dict(row) for row in conn.execute(
                'SELECT checkpoint_id,tag FROM checkpoints WHERE workflow_id=? AND checkpoint_id=?',
                (workflow_id, action_id.removeprefix(workflow_id + ':restore_checkpoint:')))]
    finally:
        conn.close()
    errors = dict(errors or {})
    if callable(controller):
        try:
            controller = controller()
        except Exception:
            controller = {'status': 'unknown'}
            errors['controller'] = 'runtime_unavailable'
    actions = collect_workflow_actions(tasks, workflow, project_root=workflow.get('project_root') or '',
                                       workflow_paused=workflow.get('status') == 'paused')
    actions.extend(workflow_control_actions(workflow, config, tasks, operations, checkpoints,
                                            fingerprint=control_fingerprint))
    for action in actions:
        if not action.target.get('task_id'):
            action.expected_identity['workflow_fingerprint'] = control_fingerprint
    graph = workflow_graph_projection({**config, **workflow}, tasks)
    result = build_cockpit_projection(workflow=workflow, config=config, tasks=tasks, graph=graph,
                                      actions=actions, recovery=operations, controller=controller,
                                      events=events, errors=errors, action_id=action_id,
                                      target_task_id=target_task_id)
    for action in result['actions']:
        action['effect'] = action.get('effect') or action.get('description') or action.get('title') or action['action_id']
        automatic = (action['automation']['mode'] == 'automatic'
                     and controller_action_type(action) in default_handlers(store))
        action['execution'] = {'mode': 'automatic' if automatic else 'manual',
            'reason': '' if automatic or action['automation']['mode'] != 'automatic' else 'automatic_handler_unavailable'}
    from herdr.controller_actions import resolve_workflow_blockers
    result['blockers'] = resolve_workflow_blockers(tasks, workflow)
    result['workflow_id'] = workflow_id
    result['receipts'] = receipts
    result['workflow_status_epoch'] = epoch['id'] if epoch else 0
    return result


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS cockpit_action_receipts ('
                 'identity_key TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,'
                 'receipt_json TEXT NOT NULL,updated_at REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS cockpit_receipts_workflow ON '
                 'cockpit_action_receipts(workflow_id,updated_at)')


def persist_receipt(store, receipt, *, key=None, claim=False):
    """Serialize competing claims across independent processes before effects."""
    key = key or hashlib.sha256(json.dumps(receipt, sort_keys=True, default=str).encode()).hexdigest()
    conn = state_db.get_db_connection(store.db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _schema(conn)
        row = conn.execute('SELECT receipt_json FROM cockpit_action_receipts WHERE identity_key=?', (key,)).fetchone()
        if claim and row:
            conn.rollback()
            return json.loads(row[0]), False
        if row:
            authoritative = json.loads(row[0])
            if ('reconciliation' not in authoritative
                    and authoritative.get('status') in {'applied', 'partial', 'rejected'}):
                conn.rollback()
                return authoritative, False
        conn.execute('INSERT INTO cockpit_action_receipts VALUES (?,?,?,?) '
                     'ON CONFLICT(identity_key) DO UPDATE SET receipt_json=excluded.receipt_json,updated_at=excluded.updated_at',
                     (key, receipt['workflow_id'], json.dumps(receipt, ensure_ascii=False), time.time()))
        conn.commit()
        return receipt, True
    finally:
        conn.close()


def _project(deps, workflow_id):
    project = getattr(deps, 'project', None)
    return project(workflow_id) if project else read_cockpit(deps.store, workflow_id, controller={'status': 'running'})


def _lookup(deps, workflow_id, action_id, projection):
    lookup = getattr(deps, 'lookup', None)
    if lookup:
        projection = lookup(workflow_id, action_id)
    elif not getattr(deps, 'project', None):
        projection = read_cockpit(deps.store, workflow_id, controller={'status': 'running'}, action_id=action_id)
    return next((a for a in projection['actions'] if a.get('action_id') == action_id), None)


def _intent(action, payload):
    """Server-derived semantic intent; a browser request id grants no authority."""
    values = {**(action.get('api_payload') or {}), **payload}
    kind = controller_action_type(action)
    if kind == 'steer':
        from herdr.completion import sanitize_completion_marker
        instruction, _ = sanitize_completion_marker(str(values.get('instruction') or '').strip(),
                                                     action['target'].get('task_id'))
        fields = {'instruction': instruction, 'operator': str(values.get('operator') or 'human').strip(), 'urgent': False}
    elif kind == 'halt':
        fields = {'reason': str(values.get('reason') or '人工在控制台紧急叫停').strip(),
                  'operator': str(values.get('operator') or 'human').strip()}
    else:
        fields = {key: values[key] for key in ('reason', 'operator', 'confirmed') if key in values}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _claim_key(action, before, intent_digest):
    expected = dict(action['expected_identity'])
    if controller_action_type(action) in {'pause', 'resume'}:
        # A metadata edit must not replay an uncertain transition in the same epoch.
        expected.pop('workflow_fingerprint', None)
    return hashlib.sha256(json.dumps({'workflow_id': before['workflow']['workflow_id'],
        'action_id': action['action_id'], 'target': action['target'], 'expected': expected,
        'intent_digest': intent_digest, 'workflow_status_epoch': before.get('workflow_status_epoch', 0)
        if not action['target'].get('task_id') else None}, sort_keys=True).encode()).hexdigest()


def read_action_state(store, action, result=None, *, since=0, intent_digest=None, event_watermark=0):
    """Exact execution facts, independent of every Cockpit display window."""
    result = result or {}
    target = action.get('target') or {}
    wid = target['workflow_id']
    tid = result.get('task_id') or target.get('task_id')
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        conn.execute('BEGIN')
        workflow, _ = recovery_store._workflow_snapshot(conn, wid)
        row = conn.execute('SELECT * FROM tasks WHERE workflow_id=? AND task_id=?', (wid, tid)).fetchone() if tid else None
        task = state_db._decode_task_row(row) if row else {}
        watermark = conn.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE workflow_id=?', (wid,)).fetchone()[0]
        facts = {'workflow': workflow, 'task': task, 'event_watermark': watermark}
        kind = controller_action_type(action)
        if kind == 'confirm_dispatch':
            op = recovery_store._get(conn, target['operation_id'])
            approval = op['detail'].get('dispatch_confirmation') or {}
            if (op['workflow_id'] == wid and approval.get('identity_key') == op['identity_key']
                    and approval.get('confirmed_at', 0) >= since):
                facts['dispatch_confirmation'] = {'operation_id': op['id']}
        if kind in {'checkpoint', 'restore_checkpoint', 'step'}:
            event_type = {'checkpoint': 'checkpoint_created', 'restore_checkpoint': 'checkpoint_restored',
                          'step': 'workflow_transition'}[kind]
            identity_field = 'reason' if kind == 'step' else 'checkpoint_id'
            identity = 'step_workflow' if kind == 'step' else result.get('checkpoint_id') or target.get('checkpoint_id')
            source = 'kernel_step' if kind == 'step' else 'checkpoint_store'
            event = conn.execute('SELECT id FROM events WHERE workflow_id=? AND event_type=? AND source=? '
                    "AND json_valid(payload_json) "
                    f"AND json_extract(payload_json,'$.{identity_field}')=? AND id>? "
                    'AND timestamp>=? ORDER BY id DESC LIMIT 1',
                    (wid, event_type, source, identity, event_watermark, since)).fetchone()
            if event:
                facts['control_event'] = {'id': event['id'], 'event_type': event_type}
        if kind == 'rollback':
            history = [h for h in workflow.get('history', []) if h.get('action') == 'rollback'
                and h.get('target_node_id') == target.get('node_id') and h.get('timestamp', 0) >= since]
            if len(history) == 1:
                facts['rollback'] = history[0]
        if kind in {'resume', 'pause'} and result.get('event_id'):
            event = conn.execute("SELECT * FROM events WHERE id=? AND workflow_id=? AND id>? "
                "AND event_type='workflow_transition' AND source='controller-cockpit' AND timestamp>=?",
                (result['event_id'], wid, event_watermark, since)).fetchone()
            payload = json.loads(event['payload_json']) if event else {}
            if (payload.get('from_status') == ('paused' if kind == 'resume' else 'running')
                    and payload.get('to_status') == ('running' if kind == 'resume' else 'paused')
                    and payload.get('reason') == kind + '_workflow'):
                facts['workflow_transition'] = {'id': event['id']}
        if kind == 'steer' and tid:
            if result.get('steer_id'):
                rows = conn.execute('SELECT * FROM steering_items WHERE task_id=? AND steer_id=?',
                                    (tid, result['steer_id'])).fetchall()
            else:
                rows = conn.execute('SELECT * FROM steering_items WHERE task_id=? AND created_at>=? '
                                    'ORDER BY created_at DESC LIMIT 100', (tid, since)).fetchall()
            matches = []
            for item in rows:
                item = {**json.loads(item['payload_json'] or '{}'), **dict(item)}
                if item.get('run_id') != target.get('run_id'):
                    continue
                digest = _intent(action, {'instruction': item['instruction'], 'operator': item['operator']})
                if intent_digest and digest != intent_digest:
                    continue
                matches.append({key: item.get(key) for key in ('steer_id', 'task_id', 'run_id', 'status')})
            if len(matches) == 1:
                facts['steer'] = matches[0]
        if kind == 'halt' and tid:
            rows = conn.execute("SELECT * FROM events WHERE workflow_id=? AND task_id=? AND id>? "
                "AND event_type IN ('steering.task_halted','steering.task_halt_failed') "
                "AND source='steering' AND timestamp>=? ORDER BY timestamp DESC,id DESC LIMIT 100",
                (wid, tid, event_watermark, since)).fetchall()
            for event in rows:
                payload = json.loads(event['payload_json'] or '{}')
                if result.get('timestamp') is not None and event['timestamp'] != result['timestamp']:
                    continue
                if intent_digest and _intent(action, payload) != intent_digest:
                    continue
                if event['run_id'] and event['run_id'] != target.get('run_id'):
                    continue
                facts['halt_event'] = {'id': event['id'], 'event_type': event['event_type'], 'timestamp': event['timestamp']}
                break
        return {'workflow': workflow, 'tasks': [task] if task else [], 'recovery': [], 'action_readback': facts}
    finally:
        conn.close()


def _unknown_cockpit(wid, receipt):
    result = build_cockpit_projection(workflow={'workflow_id': wid}, config=None, tasks=None, graph=None,
        actions=None, recovery=None, controller=None, events=None, errors={'readback': 'unavailable'})
    result['receipts'] = [receipt]
    return result


def pending_action_receipt(action, before, payload, initial):
    context = {'action': {key: action.get(key) or {} for key in ('action_id', 'target', 'expected_identity')},
               'confirmation': {'confirmed': payload.get('confirmed') is True, 'operator': payload.get('operator')},
               'before_workflow': {key: before['workflow'].get(key) for key in ('workflow_id', 'status', 'execution_id', 'candidate_sha')},
               'started_at': time.time(), 'event_watermark': initial['action_readback']['event_watermark'],
               'intent_digest': _intent(action, payload)}
    context['action']['api_payload'] = {key: action['api_payload'][key] for key in ('type', 'step', 'target_node_id', 'checkpoint_id') if key in action['api_payload']}
    context['action']['api_endpoint'] = action.get('api_endpoint')
    return _receipt(action, before['workflow']['workflow_id'], 'accepted_waiting', outcome='claimed',
        reason_code='execution_unconfirmed', message='执行责任已预占；中断后需核查副作用，禁止自动重发', reconciliation=context)


def checkpoint_result(pending, result):
    context = dict(pending['reconciliation'])
    context['result'] = {key: result[key] for key in ('task_id', 'steer_id', 'operation_id', 'timestamp',
        'event_id', 'partial', 'gate_passed', 'ok', 'status', 'checkpoint_id') if key in result}
    return {**pending, 'reconciliation': context, 'outcome': 'unknown', 'reason_code': 'readback_pending'}


def reconcile_pending_receipts(store, workflow_id, *, limit=MAX_PER_TICK):
    """Reconcile persisted effect facts; never dispatch a previously claimed intent."""
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        if not _has_table(conn, 'cockpit_action_receipts'):
            return []
        pending = [(row['identity_key'], json.loads(row['receipt_json'])) for row in conn.execute(
            "SELECT identity_key,receipt_json FROM cockpit_action_receipts WHERE workflow_id=? "
            "AND json_extract(receipt_json,'$.reconciliation') IS NOT NULL "
            "ORDER BY updated_at LIMIT ?", (workflow_id, max(0, min(limit, MAX_PER_TICK))))]
    finally:
        conn.close()
    updated = []
    for key, receipt in pending:
        context = receipt['reconciliation']
        try:
            after = read_action_state(store, context['action'], context.get('result'), since=context['started_at'],
                intent_digest=context['intent_digest'], event_watermark=context['event_watermark'])
            final = classify_receipt(context['action'], {'workflow': context['before_workflow']}, after,
                                     context.get('result') or {})
        except Exception:
            persist_receipt(store, receipt, key=key)  # Rotate one unavailable target behind later work.
            continue
        if final['status'] == 'rejected' and final.get('reason_code') in {'effect_unconfirmed', 'readback_identity_changed'}:
            persist_receipt(store, receipt, key=key)
            continue  # Absence/current replacement does not prove the old effect failed.
        final, _ = persist_receipt(store, final, key=key)
        updated.append(final)
    return updated


def default_handlers(store):
    def workflow_transition(action):
        wid = action['target']['workflow_id']
        kind = controller_action_type(action)
        conn = state_db.get_db_connection(store.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            workflow, _, tasks = recovery_store._snapshot(conn, wid)
            candidates = collect_workflow_actions(tasks, workflow, workflow_paused=workflow.get('status') == 'paused')
            candidates.extend(workflow_control_actions(workflow, workflow['config'], tasks, [], []))
            current_fingerprint = fingerprint(conn, wid)
            for candidate in candidates:
                if not candidate.target.get('task_id'):
                    candidate.expected_identity['workflow_fingerprint'] = current_fingerprint
            current = next((a.to_dict() for a in candidates if a.action_id == action['action_id']), None)
            if (not current or current['expected_identity'] != action['expected_identity']
                    or current['target'] != action['target'] or current['automation'] != action['automation']):
                raise _IdentityChanged('workflow identity or policy changed at write boundary')
            result = state_db.transition_workflow(wid, 'running' if kind == 'resume' else 'paused',
                reason=kind + '_workflow', source='controller-cockpit',
                expected_status='paused' if kind == 'resume' else 'running', conn=conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Task CLIs accept only task_id: none can atomically fence a replaced Run.
    # Existing human/dispatch paths remain available; do not auto-register them.
    return {'resume': workflow_transition, 'pause': workflow_transition}


def execute_action(payload, *, deps, automatic=False):
    """Resolve server-authored action, claim durably, revalidate and execute."""
    wid, aid = str(payload.get('workflow_id') or '').strip(), str(payload.get('action_id') or '').strip()
    if not wid or not aid:
        raise ValueError('workflow_id and action_id required')
    before = _project(deps, wid)
    action = _lookup(deps, wid, aid, before)
    def reject(reason, current=None, *, key=None, outcome='rejected'):
        receipt = _receipt(action or {'action_id': aid}, wid, 'rejected', outcome=outcome,
                           reason_code=reason, refresh_required=True)
        persist_receipt(deps.store, receipt, key=key)
        return {'ok': False, 'receipt': receipt, 'cockpit': current or before}
    if not action:
        return reject('action_obsolete')
    expected = payload.get('expected_identity')
    if not isinstance(expected, dict) or expected != action.get('expected_identity'):
        return reject('identity_changed')
    if (action.get('automation') or {}).get('mode') == 'blocked':
        return reject((action.get('automation') or {}).get('reason') or 'identity_unknown')
    if action['automation']['mode'] == 'confirmation_required' and payload.get('confirmed') is not True:
        return reject('confirmation_required')
    kind = controller_action_type(action)
    if kind == 'confirm_dispatch' and (not str(payload.get('reason') or '').strip()
                                       or not str(payload.get('operator') or '').strip()):
        # Mirror recovery_store.decide_operation's input contract before any
        # durable claim exists, so a blank reason cannot strand the claim.
        return reject('reason_required')
    if automatic and ((action.get('automation') or {}).get('mode') != 'automatic'
                      or action.get('is_destructive') or action.get('risk') in {'destructive', 'evidence_bypass'}
                      or action.get('category') == 'bypass'
                      or kind in {'halt', 'supersede', 'force_pass', 'force_pass_advance', 'accept_and_commit', 'close_workflow'}):
        return reject('automatic_action_unsafe')
    if automatic and any(op.get('status') not in {'resolved', 'superseded'} for op in before.get('recovery', [])):
        return reject('recovery_conflict')
    handlers = getattr(deps, 'handlers', None)
    handlers = default_handlers(deps.store) if handlers is None else handlers
    handler = handlers.get(kind)
    if handler is None:
        return reject('unsupported_handler')
    intent_digest = _intent(action, payload)
    key = _claim_key(action, before, intent_digest)
    initial = read_action_state(deps.store, action)
    claimed = pending_action_receipt(action, before, payload, initial)
    context = claimed['reconciliation']
    receipt, created = persist_receipt(deps.store, claimed, key=key, claim=True)
    if not created:
        return {'ok': receipt['status'] != 'rejected', 'receipt': receipt, 'cockpit': before, 'duplicate': True}
    fresh = _project(deps, wid)
    current = _lookup(deps, wid, aid, fresh)
    if (not current or current.get('target') != action.get('target')
            or current.get('expected_identity') != expected or current.get('automation') != action.get('automation')
            or fresh.get('recovery') != before.get('recovery')):
        return reject('identity_changed', fresh, key=key)
    try:
        # Handler receives only freshly derived action plus the allowed human input.
        current = {**current, 'user_input': {k: payload[k] for k in ('instruction', 'reason', 'confirmed', 'operator') if k in payload}}
        result = handler(current) or {}
    except _IdentityChanged:
        return reject('identity_changed', key=key)
    except Exception:
        uncertain = {**claimed, 'outcome': 'unknown', 'reason_code': 'effect_unknown', 'refresh_required': True}
        uncertain, _ = persist_receipt(deps.store, uncertain, key=key)
        return {'ok': False, 'receipt': uncertain, 'cockpit': _unknown_cockpit(wid, uncertain)}
    # Checkpoint only small structured result identifiers before optional reads.
    pending = checkpoint_result(claimed, result)
    context = pending['reconciliation']
    persist_receipt(deps.store, pending, key=key)
    try:
        after = _project(deps, wid)
        exact = read_action_state(deps.store, current, result, since=context['started_at'],
            intent_digest=intent_digest, event_watermark=context['event_watermark'])
        after['action_readback'] = exact['action_readback']
        receipt = classify_receipt(current, before, after, result)
        if receipt.get('reason_code') in {'effect_unconfirmed', 'readback_identity_changed'}:
            receipt = {**pending, 'reason_code': receipt['reason_code'], 'refresh_required': True}
    except Exception:
        uncertain = {**pending, 'reason_code': 'readback_unavailable', 'refresh_required': True}
        uncertain, _ = persist_receipt(deps.store, uncertain, key=key)
        return {**result, 'ok': False, 'receipt': uncertain, 'cockpit': _unknown_cockpit(wid, uncertain)}
    receipt, _ = persist_receipt(deps.store, receipt, key=key)
    after['receipts'] = [receipt, *[r for r in after.get('receipts', []) if r.get('reconciliation') != context]][:100]
    return {**result, 'ok': receipt['status'] in {'applied', 'accepted_waiting'}, 'receipt': receipt, 'cockpit': after}


def run_automatic_controller_actions(workflow_id, *, deps):
    recovery_store.reconcile(deps.store.db_path, workflow_id)
    reconcile_pending_receipts(deps.store, workflow_id)
    projection = _project(deps, workflow_id)
    candidates = [a for a in projection['actions'] if (a.get('automation') or {}).get('mode') == 'automatic']
    handlers = getattr(deps, 'handlers', None)
    handlers = default_handlers(deps.store) if handlers is None else handlers
    conn = state_db.get_readonly_db_connection(deps.store.db_path)
    try:
        claimed = set()
        if _has_table(conn, 'cockpit_action_receipts'):
            for action in candidates:
                key = _claim_key(action, projection, _intent(action, {}))
                if conn.execute('SELECT 1 FROM cockpit_action_receipts WHERE identity_key=?', (key,)).fetchone():
                    claimed.add(action['action_id'])
    finally:
        conn.close()
    eligible = [a for a in candidates if controller_action_type(a) in handlers and a['action_id'] not in claimed]
    actions = eligible[:MAX_PER_TICK]
    receipts = []
    for action in actions:
        result = execute_action({'workflow_id': workflow_id, 'action_id': action['action_id'],
                                 'expected_identity': action['expected_identity']}, deps=deps, automatic=True)
        if not result.get('duplicate'):
            receipts.append(result['receipt'])
    return receipts
def schedule_automatic_controller_actions(store, workflow_ids):
    """One worker, four in-flight workflows; never block the registry tick."""
    global _scan_cursor
    workflow_ids = sorted(workflow_ids)
    with _lock:
        if not workflow_ids or len(_inflight) >= MAX_PER_TICK:
            return
        start = _scan_cursor % len(workflow_ids)
        _scan_cursor = (start + MAX_PER_TICK) % len(workflow_ids)
    workflow_ids = workflow_ids[start:] + workflow_ids[:start]
    scheduled = 0
    for wid in workflow_ids:
        if scheduled >= MAX_PER_TICK:
            break
        identity = (str(store.db_path), wid)
        with _lock:
            if identity in _inflight or len(_inflight) >= MAX_PER_TICK:
                continue
            _inflight.add(identity)
        scheduled += 1
        def work(wid=wid, identity=identity):
            try:
                run_automatic_controller_actions(wid, deps=SimpleNamespace(store=store))
            except Exception:
                persist_receipt(store, {'workflow_id': wid, 'action_id': 'cockpit:sweep', 'status': 'rejected',
                                         'outcome': 'failed', 'reason_code': 'automation_unavailable', 'refresh_required': True})
            finally:
                with _lock:
                    _inflight.discard(identity)
        try:
            _executor.submit(work)
        except RuntimeError:
            with _lock:
                _inflight.discard(identity)
