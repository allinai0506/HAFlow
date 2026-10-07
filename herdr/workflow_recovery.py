"""Recovery result proofs and orchestration over the existing authoritative store."""
import re
import time
import uuid

from . import recovery_store
from .workflow_progress import active_tasks


def successor_launch_command(cli, workflow, predecessor, successor_id, source, prompt):
    """A new repair round owns a distinct branch rooted at the failed immutable commit."""
    sha = predecessor.get('commit')
    if not re.fullmatch(r'[0-9a-f]{40}', sha or '') or not workflow.get('execution_id'):
        raise ValueError('committed successor requires exact SHA and workflow execution identity')
    return [cli, 'launch', '--task-id', successor_id, '--workflow-id', workflow['workflow_id'],
            '--node', predecessor.get('node') or predecessor['stage'], '--source', source,
            '--agent', 'auto', '--integration-mode', 'git', '--task-type', 'fix',
            '--candidate-sha', sha, '--supersedes', predecessor['task_id'],
            '--supersede-reason', 'failed candidate recovery',
            '--dispatch-role', predecessor.get('dispatch_role') or 'worker',
            '--dispatch-round', str(int(predecessor.get('dispatch_round') or 1) + 1),
            '--execution-id', workflow['execution_id'], '--goal', predecessor.get('goal') or '修复验收阻断',
            '--prompt', prompt]



def repair_coverage(operation, tasks):
    """Return uncovered original IDs; '<unknown>' denotes missing original scope."""
    payload, detail = operation.get('payload') or {}, operation.get('detail') or {}
    expected = sorted(set(payload.get('task_ids' if payload.get('kind') == 'finalize' else 'affected_task_ids') or []))
    if not expected:
        return ['<unknown>']
    mapping = detail.get('repair_map') or {}
    source_runs = detail.get('source_runs') or {}
    by_id = {task.get('task_id'): task for task in tasks}
    counts = {}
    for entry in mapping.values():
        if isinstance(entry, dict):
            counts[entry.get('task_id')] = counts.get(entry.get('task_id'), 0) + 1
    missing = []
    for original_id in expected:
        entry = mapping.get(original_id) or {}
        old = by_id.get(original_id) or {}
        target = by_id.get(entry.get('task_id')) or {}
        valid = (bool(source_runs.get(original_id))
                 and entry.get('source_run_id') == source_runs[original_id] == old.get('run_id')
                 and bool(entry.get('run_id')) and entry['run_id'] == target.get('run_id')
                 and counts.get(entry.get('task_id')) == 1
                 and old.get('workflow_id') == target.get('workflow_id') == operation.get('workflow_id')
                 and bool(detail.get('execution_id'))
                 and old.get('execution_id') == target.get('execution_id') == detail['execution_id']
                 and not target.get('superseded_by') and target.get('status') != 'superseded')
        kind = entry.get('kind')
        if valid and kind == 'successor' and payload.get('kind') != 'finalize':
            lineage = target.get('recovery_lineage') or {}
            required = {'predecessor_id': original_id, 'successor_id': entry['task_id'],
                        'predecessor_run_id': entry['source_run_id'], 'successor_run_id': entry['run_id'],
                        'candidate_sha': payload.get('candidate_sha')}
            valid = (entry['task_id'] != original_id and entry['run_id'] != entry['source_run_id']
                     and old.get('superseded_by') == entry['task_id'] and target.get('supersedes') == original_id
                     and bool(payload.get('candidate_sha')) and old.get('commit') == payload['candidate_sha']
                     and all(lineage.get(key) == value for key, value in required.items()))
        elif valid and kind == 'delivered_successor' and payload.get('kind') != 'finalize':
            parent = by_id.get(target.get('supersedes')) or {}
            lineage = target.get('recovery_lineage') or {}
            valid = (entry['task_id'] == original_id and entry['run_id'] == entry['source_run_id']
                     and parent.get('superseded_by') == original_id
                     and parent.get('workflow_id') == operation.get('workflow_id')
                     and parent.get('execution_id') == detail['execution_id']
                     and all(lineage.get(key) == value for key, value in {
                         'predecessor_id': parent.get('task_id'), 'successor_id': original_id,
                         'predecessor_run_id': parent.get('run_id'), 'successor_run_id': target.get('run_id'),
                         'candidate_sha': payload.get('candidate_sha')}.items()))
        elif valid and kind in {'rework', 'finalize'}:
            valid = (entry['task_id'] == original_id and entry['run_id'] == entry['source_run_id']
                     and ((kind == 'finalize' and payload.get('kind') == 'finalize')
                          or (kind == 'rework' and payload.get('kind') != 'finalize'
                              and target.get('rework_delivery') == 'delivered')))
            if valid and kind == 'rework':
                expected_request = (detail.get('rework_requests') or {}).get(original_id)
                valid = (bool(expected_request) and entry.get('request_id') == expected_request
                         and target.get('rework_request_id') == expected_request
                         and all(key in entry and entry[key] == target.get(key)
                                 for key in ('completion_epoch', 'completion_identity_path')))
                if target.get('completion_protocol') == 'receipt-v1':
                    valid = valid and bool(target.get('completion_epoch')) and bool(target.get('completion_identity_path'))
        else:
            valid = False
        if not valid:
            missing.append(original_id)
    return missing


def result_status(operation, workflow, tasks, acceptance=None):
    """Only fresh, complete gates on the replacement candidate close a repair obligation."""
    if workflow.get('status') != 'running' or workflow.get('startup_ready') is False:
        return None
    detail, payload = operation['detail'], operation['payload']
    if detail.get('execution_id') != workflow.get('execution_id'):
        return 'waiting_human', {'reason': 'recovery_generation_changed'}
    missing = repair_coverage(operation, tasks)
    if missing:
        return 'waiting_human', {'reason': 'recovery_targets_unknown' if missing == ['<unknown>'] else 'recovery_coverage_incomplete',
                                 'missing_task_ids': missing}
    mapped_ids = {entry['task_id'] for entry in detail['repair_map'].values()}
    listed_ids = set((detail.get('successor_ids') or []) + (detail.get('rework_ids') or []))
    if mapped_ids != listed_ids:
        return 'waiting_human', {'reason': 'recovery_target_map_mismatch'}
    owned = active_tasks(workflow, tasks)
    by_id = {t['task_id']: t for t in owned}
    target_ids = sorted(set((detail.get('successor_ids') or []) + (detail.get('rework_ids') or [])))
    if not target_ids:
        return 'waiting_human', {'reason': 'recovery_target_unknown'}
    targets = [by_id.get(tid) for tid in target_ids]
    if any(t is None for t in targets):
        return 'waiting_human', {'reason': 'recovery_successor_missing'}
    target_runs = detail.get('target_runs') or {}
    if any(not t.get('run_id') or target_runs.get(t['task_id']) != t['run_id'] for t in targets):
        return 'waiting_human', {'reason': 'recovery_target_run_unknown'}
    if any(t.get('status') in {'failed', 'blocked', 'interrupted'} or t.get('stage_verdict') == 'blocked'
           for t in targets):
        return 'waiting_human', {'reason': 'recovery_successor_failed'}
    if any(t.get('status') not in {'integrated', 'cleanup_ready', 'cleaned'} for t in targets):
        return None
    if payload.get('kind') == 'finalize':
        if all(not t.get('finalize_escalated') and t.get('status') in {'integrated', 'cleanup_ready', 'cleaned'} for t in targets):
            return 'resolved', {'reason': 'delivery_finalized'}
        return None
    candidate = workflow.get('candidate_sha')
    if not candidate or candidate == payload.get('candidate_sha'):
        return None
    if any(t.get('execution_id') != workflow.get('execution_id') for t in targets):
        return 'waiting_human', {'reason': 'recovery_successor_identity_unknown'}
    gates = [gate for gate in detail.get('gate_nodes') or [] if gate != 'wrapup']
    if not gates:
        return 'waiting_human', {'reason': 'recovery_verifiers_unknown'}
    from .scheduler import candidate_revision_matches
    for gate in gates:
        verifiers = [t for t in owned if (t.get('node') or t.get('stage')) == gate]
        if any(t.get('completion_protocol') == 'receipt-v1'
               and not (acceptance or {}).get(t['task_id']) for t in verifiers):
            return None
        if not verifiers or any(t.get('status') not in {'completed', 'cleanup_ready', 'cleaned'}
                                or t.get('stage_verdict') not in {'pass', 'approved'}
                                or t.get('execution_id') != workflow.get('execution_id')
                                or not candidate_revision_matches(t, candidate) for t in verifiers):
            return None
    return 'resolved', {'reason': 'replacement_reverified', 'verified_candidate_sha': candidate,
                        'verifier_task_ids': sorted(t['task_id'] for t in owned
                            if (t.get('node') or t.get('stage')) in gates)}


def drive_recovery(store, workflow_id, execute, now=None):
    """Durable claims precede effects; an executor receipt never means business acceptance."""
    now = time.time() if now is None else now
    operations = recovery_store.reconcile(store.db_path, workflow_id, now, active_only=True, limit=32)
    for op in operations:
        if (op.get('payload') or {}).get('kind') == 'node_dispatch':
            continue  # Only the coordinator queue can send a first-node dispatch.
        if op['status'] == 'awaiting_result':
            recovery_store.settle_result(store.db_path, op['id'], op['version'], now)
            continue
        if op['status'] != 'pending':
            continue
        owner = uuid.uuid4().hex
        claimed = recovery_store.claim_operation(store.db_path, op['id'], owner, now, lease_seconds=180)
        if claimed is None:
            continue
        try:
            status, detail = execute(claimed, owner)
        except Exception as exc:
            status, detail = 'waiting_human', {'reason': 'recovery_execution_failed',
                                              'error_type': type(exc).__name__}
        if status == 'awaiting_result':
            detail = {**detail, 'result_deadline_at': detail.get('result_deadline_at', now + 3600),
                      'next_due_at': now + 30}
        recovery_store.finish_operation(store.db_path, op['id'], owner, status, detail,
                                        time.time() if now > 1_000_000_000 else now)
    return recovery_store.list_operations(store.db_path, workflow_id)


def existing_delivery_details(operation, workflow, tasks, store):
    """Bind current durable work before planning another transport action.

    An existing request inventory is never replaced by a new UUID. Partial or
    pending inventories remain explicit unknown coverage, rather than resend.
    """
    from . import state_db
    from .recovery_successor import _receipts, confirmed_rework, delivery_confirmed
    payload = operation['payload']
    if payload.get('kind') == 'finalize':
        return None
    original_ids = payload.get('affected_task_ids') or []
    by_id = {t['task_id']: t for t in tasks}
    detail = dict(operation.get('detail') or {})
    if detail.get('successor_ids') or detail.get('rework_ids'):
        return detail
    result = {'action': 'verify', 'execution_id': workflow.get('execution_id'),
              'successor_ids': [], 'rework_ids': [], 'source_runs': {}, 'target_runs': {},
              'repair_map': {}, 'rework_requests': {}}
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        for tid in original_ids:
            task = by_id.get(tid) or {}
            if (task.get('workflow_id') != workflow.get('workflow_id') or not task.get('run_id')
                    or task.get('execution_id') != workflow.get('execution_id')
                    or (task.get('candidate_sha') or task.get('commit')) != payload.get('candidate_sha')):
                continue
            parent = by_id.get(task.get('supersedes')) or {}
            if (task.get('status') in ('pending', 'working') and not task.get('stage_verdict')
                    and parent.get('superseded_by') == tid
                    and parent.get('workflow_id') == workflow.get('workflow_id')
                    and parent.get('execution_id') == workflow.get('execution_id')
                    and delivery_confirmed(task, _receipts(conn, task))):
                result['successor_ids'].append(tid)
                result['source_runs'][tid] = result['target_runs'][tid] = task['run_id']
                result['repair_map'][tid] = {'kind': 'delivered_successor', 'task_id': tid,
                    'run_id': task['run_id'], 'source_run_id': task['run_id']}
                continue
            successor = by_id.get(task.get('superseded_by')) or {}
            if (successor.get('supersedes') == tid and successor.get('execution_id') == workflow.get('execution_id')
                    and successor.get('workflow_id') == workflow.get('workflow_id')
                    and delivery_confirmed(successor, _receipts(conn, successor))):
                result['successor_ids'].append(successor['task_id'])
                result['source_runs'][tid] = task['run_id']
                result['target_runs'][successor['task_id']] = successor['run_id']
                result['repair_map'][tid] = {'kind': 'successor', 'task_id': successor['task_id'],
                    'run_id': successor['run_id'], 'source_run_id': task['run_id']}
                continue
            request = task.get('rework_request_id')
            if request and task.get('rework_delivery') in ('pending', 'delivered'):
                # Bind even pending/unknown inventory so the executor verifies
                # and waits instead of issuing a second intervention.
                result['rework_ids'].append(tid)
                result['source_runs'][tid] = result['target_runs'][tid] = task['run_id']
                result['rework_requests'][tid] = request
                if confirmed_rework(conn, task, request):
                    result['repair_map'][tid] = {'kind': 'rework', 'task_id': tid,
                        'run_id': task['run_id'], 'source_run_id': task['run_id'], 'request_id': request,
                        'completion_epoch': task.get('completion_epoch'),
                        'completion_identity_path': task.get('completion_identity_path')}
        return result if result['successor_ids'] or result['rework_ids'] else None
    finally:
        conn.close()
