"""Execute durable recovery operations through existing task repair primitives.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def execute_workflow_recovery(ports, operation, owner):
    """Run existing Task primitives only after the durable recovery claim."""
    from herdr import recovery_store
    from herdr.workflow_recovery import successor_launch_command, repair_coverage
    from herdr.recovery_successor import has_confirmed_delivery, has_confirmed_rework, choose_recovery_source
    from herdr.repo_hygiene import check_source_cleanliness
    store = ports._get_store()
    wid, payload = operation['workflow_id'], operation['payload']
    workflow, config, snapshot_tasks = recovery_store.read_snapshot(store.db_path, wid)
    if payload.get('kind') == 'delivery':
        from herdr.delivery_rework import execute_delivery_recovery
        return execute_delivery_recovery(store, operation, owner)
    source = workflow.get('project_root')
    if not source:
        return 'waiting_human', {'reason': 'project_identity_unknown'}
    from herdr.workflow_recovery import existing_delivery_details
    bound = existing_delivery_details(operation, workflow, snapshot_tasks, store)
    if bound:
        operation = dict(operation, detail={**operation['detail'], **bound})
    verifying = (operation['detail'].get('action') == 'verify'
                 or bool(operation['detail'].get('successor_ids') or operation['detail'].get('rework_ids')))
    if not workflow.get('execution_id') or not config.get('nodes'):
        return 'waiting_human', {'reason': 'workflow_identity_or_config_unknown'}
    if not verifying:
        clean, paths = check_source_cleanliness(source)
        if not clean:
            return 'waiting_human', {'reason': 'source_wip_requires_decision', 'dirty_paths': paths[:50]}
    target_ids = payload.get('task_ids') if payload['kind'] == 'finalize' else payload.get('affected_task_ids')
    by_id = {t['task_id']: t for t in snapshot_tasks}
    targets = [by_id.get(tid) for tid in target_ids or []]
    if not targets or any(t is None for t in targets):
        return 'waiting_human', {'reason': 'recovery_target_unknown'}
    details = {'execution_id': workflow.get('execution_id'), 'successor_ids': [], 'rework_ids': [], 'target_runs': {}, 'source_runs': {}, 'repair_map': {}, 'rework_requests': {},
               'gate_nodes': sorted({n['id'] for n in config.get('nodes') or []
                   if n['id'] != 'wrapup' and (ports.resolve_gate_config(n, n['id']) or {}).get('retry_node') == payload.get('retry_node')})}
    if verifying:
        details.update(operation['detail'])
    details['gate_nodes'] = [gate for gate in details['gate_nodes'] if gate != 'wrapup']
    def step(name, validate=True):
        if validate:
            recovery_store.renew_owner(store.db_path, operation['id'], owner, ports.time.time(), lease_seconds=180)
        recovery_store.record_step(store.db_path, operation['id'], owner, name, details, ports.time.time(), validate=validate)
    if verifying:
        from herdr.recovery_successor import link_committed_successor
        confirmed = details.get('successor_ids') or []
        reworked = details.get('rework_ids') or []
        if not confirmed and not reworked:
            return 'waiting_human', {'reason': 'delivery_inventory_missing'}
        for successor_id in confirmed:
            successor = store.get_task(successor_id)
            if not successor or not has_confirmed_delivery(store, successor):
                return 'waiting_human', {'reason': 'delivery_unknown'}
            details.setdefault('target_runs', {}).setdefault(successor_id, successor['run_id'])
            predecessor = store.get_task(successor.get('recovery_predecessor') or successor.get('supersedes'))
            if not predecessor:
                return 'waiting_human', {'reason': 'recovery_predecessor_unknown'}
            if not predecessor.get('superseded_by'):
                step('existing_delivery_linking')
                link_committed_successor(store, predecessor['task_id'], successor_id,
                    successor.get('expected_predecessor_version'), payload['candidate_sha'], 'verified recovery delivery')
            if (details.get('repair_map', {}).get(successor_id) or {}).get('kind') == 'delivered_successor':
                continue
            details.setdefault('repair_map', {})[predecessor['task_id']] = {
                'kind': 'successor', 'task_id': successor_id, 'run_id': details['target_runs'][successor_id],
                'source_run_id': details.get('source_runs', {}).get(predecessor['task_id'])}
        for task_id in reworked:
            task = store.get_task(task_id) or {}
            expected_request = details.get('rework_requests', {}).get(task_id)
            if payload['kind'] != 'finalize' and not has_confirmed_rework(store, task, expected_request):
                return 'waiting_human', {'reason': 'rework_delivery_unconfirmed'}
            details.setdefault('repair_map', {}).setdefault(task_id, {
                'kind': 'finalize' if payload['kind'] == 'finalize' else 'rework',
                'task_id': task_id, 'run_id': details.get('target_runs', {}).get(task_id),
                'source_run_id': details.get('source_runs', {}).get(task_id),
                'request_id': expected_request, 'completion_epoch':task.get('completion_epoch'),
                'completion_identity_path':task.get('completion_identity_path')})
        missing = repair_coverage(dict(operation, detail=details), store.list_tasks(workflow_id=wid))
        if missing:
            return 'waiting_human', {'reason': 'repair_coverage_incomplete', 'missing_task_ids': missing, **details}
        step('existing_delivery_verified', validate=False)
        if payload['kind'] == 'finalize':
            return 'awaiting_result', {'step': 'awaiting_delivery', **details}
    if payload['kind'] == 'finalize':
        # A failed candidate is never integrated as a prerequisite for repairing it.
        if any(t.get('stage_verdict') == 'blocked' for t in store.list_tasks(workflow_id=wid)
               if t.get('status') != 'superseded' and not t.get('superseded_by')):
            return 'waiting_human', {'reason': 'candidate_requires_repair_before_integration'}
        for task in targets:
            details['rework_ids'].append(task['task_id'])
            details['source_runs'][task['task_id']] = task.get('run_id')
            details['repair_map'][task['task_id']] = {'kind':'finalize', 'task_id':task['task_id'],
                'run_id':task.get('run_id'), 'source_run_id':task.get('run_id')}
            details['target_runs'][task['task_id']] = task.get('run_id')
            step('finalize_started')
            if not ports.clear_finalize_escalation(task['task_id']):
                return 'waiting_human', {'reason': 'escalation_clear_failed', **details}
            ports.finalize_completed_task(task['task_id'])
            fresh = store.get_task(task['task_id']) or {}
            if fresh.get('finalize_escalated'):
                return 'waiting_human', {'reason': fresh.get('finalize_escalate_reason') or 'finalize_refused', **details}
        return 'awaiting_result', {'step': 'awaiting_delivery', **details}
    for task in ([] if verifying else targets):
        tid = task['task_id']
        details['source_runs'][tid] = task.get('run_id')
        if task.get('status') == 'committed':
            if not workflow.get('execution_id'):
                return 'waiting_human', {'reason': 'workflow_execution_unknown'}
            next_id = ports.next_recovery_task_id(tid, store.list_tasks(workflow_id=wid))
            details['successor_ids'].append(next_id)
            from herdr.supervisor.state import redact_text
            prompt = ('仅修复当前授权范围内的验收阻断，不扩大范围；若需要修改约定范围外文件，'
                      '持久报告 blocked 请求人工裁决。\n' + '\n'.join(
                redact_text(str(f.get('stage_verdict_note') or f.get('blocker') or ''))[:2000]
                for f in (payload.get('facts') or [])[:20]))
            try:
                recovery_source = choose_recovery_source(task, source)
            except ValueError as exc:
                return 'waiting_human', {'reason':str(exc), **details}
            details.setdefault('source_paths', {})[tid] = recovery_source
            command = successor_launch_command(ports.TASK_MANAGER, workflow, task, next_id, recovery_source, prompt)
            step('successor_launch_started')
            result = ports.subprocess.run(command, text=True, capture_output=True, timeout=120)
            successor = store.get_task(next_id)
            if result.returncode or not successor or not has_confirmed_delivery(store, successor):
                return 'waiting_human', {'reason': 'successor_delivery_unconfirmed', **details}
            if (store.get_task(tid) or {}).get('superseded_by') != next_id:
                return 'waiting_human', {'reason': 'successor_lineage_unconfirmed', **details}
            details['target_runs'][next_id] = successor['run_id']
            details['repair_map'][tid] = {'kind':'successor', 'task_id':next_id,
                'run_id':successor['run_id'], 'source_run_id':task.get('run_id')}
            step('successor_delivery_confirmed', validate=False)
        elif task.get('status') in ports.REWORKABLE_STATUSES:
            details['rework_ids'].append(tid)
            details['target_runs'][tid] = task.get('run_id')
            request_id = 'recovery:' + ports.uuid.uuid4().hex
            details['rework_requests'][tid] = request_id
            step('rework_started')
            ok, _ = ports._rework_retry_task(task, '+'.join(payload['gate_nodes']), request_id=request_id, failure_facts=payload.get('facts') or [])
            fresh = store.get_task(tid) or {}
            if not ok or not has_confirmed_rework(store, fresh, request_id):
                return 'waiting_human', {'reason': 'rework_delivery_unconfirmed', **details}
            details['repair_map'][tid] = {'kind':'rework', 'task_id':tid,
                'run_id':task.get('run_id'), 'source_run_id':task.get('run_id'),
                'request_id':request_id, 'completion_epoch':fresh.get('completion_epoch'),
                'completion_identity_path':fresh.get('completion_identity_path')}
            step('rework_delivery_confirmed', validate=False)
        else:
            return 'waiting_human', {'reason': 'target_requires_explicit_replacement', 'missing_task_ids':[tid], **details}
    missing = repair_coverage(dict(operation, detail=details), store.list_tasks(workflow_id=wid))
    if missing:
        return 'waiting_human', {'reason': 'repair_coverage_incomplete', 'missing_task_ids': missing, **details}
    # Existing invalidation receives no retry node: targets are already delivered
    # and must not be finalized/reworked a second time.
    if verifying and workflow.get('candidate_sha') != payload.get('candidate_sha'):
        return 'awaiting_result', {'step': 'awaiting_new_candidate', **details}
    step('gates_invalidating')
    for gate in payload['gate_nodes']:
        if gate in details.get('invalidated_gate_nodes', []):
            continue
        ports.invalidate_for_fix_loop(wid, gate, config, retry_node=None)
        details.setdefault('invalidated_gate_nodes', []).append(gate)
        step('gate_invalidated', validate=False)
    remaining = [t for t in store.list_tasks(workflow_id=wid) if t.get('status') != 'superseded'
                 and not t.get('superseded_by') and t.get('stage_verdict') == 'blocked'
                 and (t.get('node') or t.get('stage')) in payload['gate_nodes']]
    if remaining:
        return 'waiting_human', {'reason': 'gate_invalidation_incomplete', **details}
    ports.clear_stage_advance(wid, payload['retry_node'])
    return 'awaiting_result', {'step': 'awaiting_new_candidate', **details}

def next_recovery_task_id(ports, task_id, tasks):
    from herdr.controller_actions import next_replacement_id
    occupied = {t.get('task_id') for t in tasks}
    next_id = next_replacement_id(task_id)
    while next_id in occupied:
        next_id = next_replacement_id(next_id)
    return next_id

def check_workflow_recovery(ports, workflow_id):
    ports.reconcile_node_dispatches(workflow_id, discover=False)
    from herdr.workflow_recovery import drive_recovery
    return drive_recovery(ports._get_store(), workflow_id, ports.execute_workflow_recovery)


DEPENDENCIES = {'execute_workflow_recovery': ('REWORKABLE_STATUSES', 'TASK_MANAGER', '_get_store',
                               '_rework_retry_task', 'clear_finalize_escalation',
                               'clear_stage_advance', 'finalize_completed_task',
                               'invalidate_for_fix_loop', 'next_recovery_task_id',
                               'resolve_gate_config', 'subprocess', 'time', 'uuid'),
 'next_recovery_task_id': (),
 'check_workflow_recovery': ('_get_store', 'execute_workflow_recovery',
                             'reconcile_node_dispatches')}

__all__ = tuple(DEPENDENCIES)
