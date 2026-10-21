"""Cockpit I/O and Controller execution boundary. No Console or browser imports.

Claims and receipts live in the authoritative StateStore database. A committed
claim is never replayed: an interrupted external effect remains explicitly
unknown until an operator reconciles it.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from herdr import recovery_store, state_db
from herdr.controller_actions import collect_workflow_actions, controller_action_type
from herdr.controller_cockpit import build_cockpit_projection
from herdr.workflow_graph import workflow_graph_projection

MAX_PER_TICK = 4
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cockpit')
_inflight = set()
_lock = threading.Lock()
_scan_cursor = 0


def _has_table(conn, name):
    return bool(conn.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', name)).fetchone())


def read_cockpit(store, workflow_id, *, controller=None, errors=None):
    """Read graph/action/recovery/event facts from one bounded SQLite snapshot."""
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        conn.execute('BEGIN')
        workflow, config, tasks = recovery_store._snapshot(conn, workflow_id)
        epoch = conn.execute("SELECT id FROM events WHERE workflow_id=? AND event_type='workflow_transition' "
                             "AND json_extract(payload_json,'$.to_status')=? ORDER BY timestamp DESC,id DESC LIMIT 1",
                             (workflow_id, workflow['status'])).fetchone()
        operations = ([recovery_store._decode(row) for row in conn.execute(
            'SELECT * FROM workflow_recovery_operations WHERE workflow_id=? '
            'ORDER BY CASE WHEN status IN (\'resolved\',\'superseded\') THEN 1 ELSE 0 END,id DESC LIMIT 100',
            (workflow_id,))] if _has_table(conn, 'workflow_recovery_operations') else [])
        events = []
        for row in conn.execute('SELECT id,workflow_id,node_id,task_id,event_type,timestamp,source,'
                                'substr(payload_json,1,16384) AS payload_json FROM events '
                                'WHERE workflow_id=? ORDER BY timestamp DESC,id DESC LIMIT 100', (workflow_id,)):
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
    graph = workflow_graph_projection({**config, **workflow}, tasks)
    result = build_cockpit_projection(workflow=workflow, config=config, tasks=tasks, graph=graph,
                                      actions=actions, recovery=operations, controller=controller,
                                      events=events, errors=errors)
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


def _receipt(action, wid, status, **extra):
    return {'status': status, 'action_id': action.get('action_id'), 'workflow_id': wid,
            'target': action.get('target') or {}, 'task_id': (action.get('target') or {}).get('task_id'),
            'operation_id': None, **extra}


def classify_receipt(action, before, after, result):
    """Transport completion is never business completion; require readback facts."""
    wid = before['workflow']['workflow_id']
    kind = controller_action_type(action)
    target = action.get('target') or {}
    expected = action.get('expected_identity') or {}
    tid = result.get('task_id') or target.get('task_id')
    task = next((t for t in after.get('tasks', []) if t.get('task_id') == tid), {})
    identity = task if target.get('task_id') else after['workflow']
    if (kind != 'launch' and any(value is not None and value != '' and identity.get(key) != value
            for fields in (target, expected) for key, value in fields.items()
            if key in {'run_id', 'execution_id', 'candidate_sha'})):
        return _receipt(action, wid, 'rejected', outcome='failed', reason_code='readback_identity_changed',
                        refresh_required=True)
    if result.get('partial') and result.get('gate_passed') and task.get('stage_verdict') == 'pass':
        return _receipt(action, wid, 'partial', outcome='failed', applied_steps=['force_pass'], failed_step='advance')
    applied = ((kind == 'resume' and after['workflow'].get('status') == 'running'
                and before['workflow'].get('status') == 'paused')
               or (kind == 'pause' and after['workflow'].get('status') == 'paused'
                   and before['workflow'].get('status') == 'running')
               or (kind in {'force_pass', 'force_pass_advance'} and task.get('stage_verdict') == 'pass')
               or (kind in {'commit', 'integrate', 'cleanup', 'finalize', 'accept_and_commit'}
                   and task.get('status') in {'commit': {'committed', 'integrated', 'cleanup_ready', 'cleaned'},
                                              'integrate': {'integrated', 'cleanup_ready', 'cleaned'},
                                              'cleanup': {'cleaned'}, 'finalize': {'cleanup_ready', 'cleaned'},
                                              'accept_and_commit': {'committed', 'integrated', 'cleanup_ready', 'cleaned'}}[kind]))
    if applied:
        return _receipt(action, wid, 'applied', outcome='success', task_id=tid, message='权威状态已回读确认')
    operations = [op for op in after.get('recovery', []) if op.get('status') not in {'resolved', 'superseded'}
                  and ((op.get('payload') or {}).get('task_id') == tid
                       or tid in ((op.get('payload') or {}).get('task_ids') or [])
                       or result.get('operation_id') == op.get('id'))]
    if operations:
        return _receipt(action, wid, 'accepted_waiting', outcome='waiting', task_id=tid,
                        operation_id=operations[0]['id'], message='派发责任已建立，等待 Task 登记' if not task else '等待执行结果')
    return _receipt(action, wid, 'rejected', outcome='skipped', reason_code='effect_unconfirmed',
                    refresh_required=True, message='未取得状态变化或持久派发责任的证据')


def default_handlers(store):
    def workflow_transition(action):
        wid = action['target']['workflow_id']
        kind = controller_action_type(action)
        conn = state_db.get_db_connection(store.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            workflow, _, tasks = recovery_store._snapshot(conn, wid)
            candidates = collect_workflow_actions(tasks, workflow, workflow_paused=workflow.get('status') == 'paused')
            current = next((a.to_dict() for a in candidates if a.action_id == action['action_id']), None)
            if (not current or current['expected_identity'] != action['expected_identity']
                    or current['target'] != action['target'] or current['automation'] != action['automation']):
                raise ValueError('workflow identity or policy changed at write boundary')
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

    def mechanical(action):
        if action.get('command_base') != 'herdr-task' or not action.get('commands'):
            raise ValueError('unsupported command handler')
        binary = Path(__file__).resolve().parents[2] / 'bin' / 'herdr-task'
        for command in action['commands']:
            subprocess.run([str(binary), *map(str, command)], check=True, capture_output=True, text=True, timeout=60,
                           env={**os.environ, 'HERDR_STATE_DB': str(store.db_path)})
        return {'ok': True, 'task_id': (action.get('target') or {}).get('task_id')}
    return {'resume': workflow_transition, 'pause': workflow_transition,
            **{kind: mechanical for kind in ('commit', 'integrate', 'cleanup', 'finalize')}}


def execute_action(payload, *, deps, automatic=False):
    """Resolve server-authored action, claim durably, revalidate and execute."""
    wid, aid = str(payload.get('workflow_id') or '').strip(), str(payload.get('action_id') or '').strip()
    if not wid or not aid:
        raise ValueError('workflow_id and action_id required')
    before = _project(deps, wid)
    action = next((a for a in before['actions'] if a.get('action_id') == aid), None)
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
    kind = controller_action_type(action)
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
    key = hashlib.sha256(json.dumps({'workflow_id': wid, 'action_id': aid, 'target': action['target'],
        'expected': expected, 'workflow_status_epoch': before.get('workflow_status_epoch', 0)
        if not action['target'].get('task_id') else None}, sort_keys=True).encode()).hexdigest()
    claimed = _receipt(action, wid, 'accepted_waiting', outcome='claimed', reason_code='execution_unconfirmed',
                       message='执行责任已预占；中断后需核查副作用，禁止自动重发')
    receipt, created = persist_receipt(deps.store, claimed, key=key, claim=True)
    if not created:
        return {'ok': receipt['status'] != 'rejected', 'receipt': receipt, 'cockpit': before, 'duplicate': True}
    fresh = _project(deps, wid)
    current = next((a for a in fresh['actions'] if a.get('action_id') == aid), None)
    if (not current or current.get('target') != action.get('target')
            or current.get('expected_identity') != expected or current.get('automation') != action.get('automation')
            or fresh.get('recovery') != before.get('recovery')):
        return reject('identity_changed', fresh, key=key)
    try:
        # Handler receives only freshly derived action plus the allowed human input.
        current = {**current, 'user_input': {k: payload[k] for k in ('instruction', 'reason', 'confirmed', 'operator') if k in payload}}
        result = handler(current) or {}
        after = _project(deps, wid)
        receipt = classify_receipt(current, before, after, result)
    except Exception:
        return reject('execution_failed', key=key, outcome='failed')
    persist_receipt(deps.store, receipt, key=key)
    after['receipts'] = [receipt, *[r for r in after.get('receipts', []) if r != claimed]][:100]
    return {**result, 'ok': receipt['status'] in {'applied', 'accepted_waiting'}, 'receipt': receipt, 'cockpit': after}


def run_automatic_controller_actions(workflow_id, *, deps):
    recovery_store.reconcile(deps.store.db_path, workflow_id)
    projection = _project(deps, workflow_id)
    actions = [a for a in projection['actions'] if (a.get('automation') or {}).get('mode') == 'automatic'][:MAX_PER_TICK]
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
