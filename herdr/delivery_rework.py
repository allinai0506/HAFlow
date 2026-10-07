"""Bounded repair of uncommitted delivery using existing Task and dispatch receipts."""
import json
import time

from . import state_db
from .git_coordination import GitOperationLock, ensure_no_git_processes
from .supervisor_delivery import deliver, DeliveryUnknown
from .task_delivery import repository_snapshot, writable_task, validate_contract
from .task_resources import owned_live_pane, workflow_launch_lock


def repair_reason(task):
    """Pure admission result: missing candidates do not prevent delivery repair."""
    failure = task.get('delivery_failure') or {}
    if (not task.get('run_id') or not task.get('execution_id') or not writable_task(task)
            or task.get('status') != 'completed' or task.get('commit') or task.get('superseded_by')
            or task.get('completion_protocol') not in (None, 'receipt-v1')
            or failure.get('status') != 'blocked' or failure.get('run_id') != task.get('run_id')
            or failure.get('task_id') != task.get('task_id')
            or failure.get('workflow_id') != task.get('workflow_id')
            or failure.get('execution_id') != task.get('execution_id')):
        return 'delivery_identity_unknown'
    contract = task.get('delivery_contract')
    if not isinstance(contract, dict) or not contract.get('auto_rework'):
        return 'delivery_repair_not_authorized'
    try:
        validate_contract(contract)
    except (ValueError, TypeError):
        return 'delivery_contract_invalid'
    if any(i.get('code') in {'scope_conflict', 'outside_scope', 'unsafe_path'} for i in failure.get('issues', [])):
        return 'delivery_scope_conflict'
    attempts = task.get('delivery_rework_attempts', 0)
    if type(attempts) is not int or attempts < 0:
        return 'delivery_repair_budget_unknown'
    if attempts >= 3:
        return 'delivery_repair_budget_exhausted'
    return None


def validate_repair_transition(task, metadata, source, conn):
    """Authorize only this edge under the Task writer transaction; never force."""
    if source != 'delivery-repair' or repair_reason(task):
        raise ValueError('delivery repair transition refused')
    request = metadata.get('delivery_repair_request_id')
    if not request or request != metadata.get('rework_request_id'):
        raise ValueError('delivery repair request identity missing')
    if metadata.get('delivery_rework_attempts') != int(task.get('delivery_rework_attempts') or 0) + 1:
        raise ValueError('delivery repair budget mismatch')
    row = conn.execute('SELECT status,metadata_json FROM workflows WHERE workflow_id=?', (task.get('workflow_id'),)).fetchone()
    if not row or row['status'] != 'running' or json.loads(row['metadata_json']).get('execution_id') != task.get('execution_id'):
        raise ValueError('delivery workflow changed before repair')


def repair_delivery(task_id, run_id, request_id, *, store, probe=None, send=None):
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        raise ValueError('bounded delivery repair request required')
    probe = probe or owned_live_pane
    with workflow_launch_lock(store.db_path, 'delivery:' + task_id):
        task = store.get_task(task_id) or {}
        if not run_id or task.get('run_id') != run_id:
            raise ValueError('delivery repair run changed')
        if task.get('delivery_repair_request_id') == request_id:
            from .recovery_successor import confirmed_rework_receipt
            conn = state_db.get_db_connection(store.db_path)
            try:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
                fresh = state_db._decode_task_row(row) if row else {}
                if (fresh.get('run_id') != run_id or fresh.get('delivery_repair_request_id') != request_id
                        or not confirmed_rework_receipt(conn, fresh, request_id)):
                    raise DeliveryUnknown('REWORK', request_id)
                if fresh.get('rework_delivery') != 'delivered':
                    state_db.update_task_metadata(task_id, {'rework_delivery': 'delivered'}, conn=conn)
                conn.commit()
                return {'rework_dispatched': True, 'already_applied': True}
            finally:
                conn.close()
        if task.get('rework_delivery') == 'pending':
            raise DeliveryUnknown('REWORK', task.get('rework_request_id'))
        reason = repair_reason(task)
        if reason:
            raise ValueError(reason)
        owned, why = probe(task)
        if not owned:
            raise ValueError('delivery repair pane ownership unknown: ' + str(why))
        with GitOperationLock(task['clone_path']):
            ensure_no_git_processes(task['clone_path'])
            snapshot = repository_snapshot(task)
            if snapshot['head'] != (task.get('delivery_failure') or {}).get('head') or snapshot['head'] != task.get('baseline_commit'):
                raise ValueError('delivery repair has an unregistered commit')
            fresh = store.get_task(task_id) or {}
            if fresh.get('version') != task.get('version') or fresh.get('run_id') != run_id:
                raise ValueError('delivery repair task changed during probe')
            failure = task['delivery_failure']
            prompt = ('Repository delivery was rejected. Preserve existing business code and repair only the pinned authorized scope.\n'
                      'Do not bypass hooks, rename the branch, invent test results, or change business scope.\n'
                      'For a scope conflict or unknown outcome report a blocker to the coordinator.\n'
                      'Failure evidence:\n' + json.dumps(failure, ensure_ascii=False) + '\n'
                      'Re-run delivery-check and the required regression before declaring completion.\n')
            def prepare(conn):
                current = state_db._decode_task_row(conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
                result = state_db.transition_task(task_id, 'rework', source='delivery-repair', reason='repository_delivery_incomplete',
                    expected_status='completed', expected_version=task['version'], conn=conn,
                    metadata={'rework_prompt': prompt, 'rework_request_id': request_id, 'rework_delivery': 'pending',
                              'delivery_repair_request_id': request_id,
                              'delivery_rework_attempts': int(task.get('delivery_rework_attempts') or 0) + 1,
                              'delivery_failure': None, 'finalize_escalated': False, 'finalize_escalate_reason': None,
                              'stage_verdict': None, 'stage_verdict_note': None, 'rework_started_at': time.time()})
                if not result.get('accepted') or current.get('run_id') != run_id:
                    raise ValueError('delivery repair task CAS changed')
            def native_send(pane, text):
                current = store.get_task(task_id) or {}
                if current.get('run_id') != run_id or current.get('rework_request_id') != request_id:
                    raise ValueError('delivery repair ownership changed before effect')
                owned, why = probe(current)
                if not owned:
                    raise ValueError('delivery repair pane ownership changed: ' + str(why))
                fresh = store.get_task(task_id) or {}
                if any(fresh.get(k) != current.get(k) for k in ('run_id', 'version', 'pane_id', 'rework_request_id', 'completion_epoch', 'completion_identity_path')):
                    raise ValueError('delivery repair changed during native ownership probe')
                if send is None:
                    import subprocess
                    from .node_capacity import pane_reference
                    result = subprocess.run(['herdr', 'agent', 'prompt', str(pane_reference(current)), text],
                                            capture_output=True, text=True, timeout=30)
                    if result.returncode:
                        raise RuntimeError('delivery repair native transport failed')
                else:
                    send(pane, text)
            result = deliver(task, store, 'REWORK', {'intervention_id': request_id, 'action': 'REWORK'},
                             prompt, native_send, prepare_task=prepare)
        conn = state_db.get_db_connection(store.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            current = state_db._decode_task_row(conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            if current.get('run_id') != run_id or current.get('rework_request_id') != request_id:
                raise DeliveryUnknown('REWORK', request_id)
            state_db.update_task_metadata(task_id, {'rework_delivery': 'delivered'}, conn=conn)
            conn.commit()
        finally:
            conn.close()
        return result


def execute_delivery_recovery(store, operation, owner):
    """One durable operation delegates only its exact source runs to the owned pane."""
    from . import recovery_store
    payload = operation['payload']
    workflow = store.get_workflow(operation['workflow_id']) or {}
    detail = dict(operation.get('detail') or {})
    facts = {f['task_id']: f for f in payload.get('facts', [])}
    detail.update(execution_id=workflow.get('execution_id'), successor_ids=[], gate_nodes=[])
    for key in ('rework_ids', 'source_runs', 'target_runs', 'rework_requests', 'repair_map'):
        detail.setdefault(key, [] if key == 'rework_ids' else {})
    for tid in payload.get('task_ids') or []:
        task = store.get_task(tid) or {}
        original = facts.get(tid) or {}
        if not task.get('run_id') or task.get('run_id') != original.get('run_id') or task.get('execution_id') != workflow.get('execution_id'):
            return 'waiting_human', {**detail, 'reason': 'delivery_repair_identity_changed'}
        request = detail['rework_requests'].setdefault(tid, 'delivery:' + str(operation['id']) + ':' + tid)
        detail['source_runs'][tid] = detail['target_runs'][tid] = task['run_id']
        recovery_store.record_step(store.db_path, operation['id'], owner, 'delivery_repair_started', detail, time.time())
        result = repair_delivery(tid, task['run_id'], request, store=store)
        if not result.get('rework_dispatched'):
            return 'waiting_human', {**detail, 'reason': 'delivery_repair_unconfirmed'}
        fresh = store.get_task(tid) or {}
        from .recovery_successor import has_confirmed_rework
        if not has_confirmed_rework(store, fresh, request):
            return 'waiting_human', {**detail, 'reason': 'delivery_repair_unconfirmed'}
        if tid not in detail['rework_ids']:
            detail['rework_ids'].append(tid)
        detail['repair_map'][tid] = {'kind': 'rework', 'task_id': tid, 'run_id': task['run_id'],
                                    'source_run_id': task['run_id'], 'request_id': request,
                                    'completion_epoch': fresh.get('completion_epoch'),
                                    'completion_identity_path': fresh.get('completion_identity_path')}
        recovery_store.record_step(store.db_path, operation['id'], owner, 'delivery_repair_delivered', detail, time.time(), validate=False)
    return 'awaiting_result', {**detail, 'action': 'verify', 'step': 'awaiting_delivery'}
