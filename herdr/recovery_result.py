"""Verified recovery progress and result settlement over the authoritative store."""
import json


def settle_result(db_path, operation_id, expected_version, now):
    """Settle only from fresh same-transaction task facts and the core proof."""
    from .recovery_store import _get, _snapshot, _transaction, _event, _human_wait_detail
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
        from .recovery_policy import verified_progress
        batch = verified_progress(reader, prepared_op, prepared_workflow, prepared_tasks, observations)
        progress_watermark = reader.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE workflow_id=? AND event_type='task_artifact_checkpoint'", (prepared_op['workflow_id'],)).fetchone()[0]
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
        watermark = conn.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE workflow_id=? AND event_type='task_artifact_checkpoint'", (op['workflow_id'],)).fetchone()[0]
        if watermark != progress_watermark:
            raise ValueError('recovery progress snapshot changed')
        from .recovery_policy import consume_progress, artifact_stamp
        for path, stamp in batch['artifact_stamps'].items():
            if artifact_stamp(path) != stamp:
                raise ValueError('recovery checkpoint artifact changed after verification')
        if 'first_started_at' in op['detail']:
            op['detail'] = consume_progress(op['detail'], batch['progress'], now)
            op['detail']['progress_scan_cursors'] = batch['scan_cursors']
        outcome = result_status(op, workflow, tasks, acceptance=acceptance)
        if outcome and outcome[0] == 'resolved':
            from herdr.recovery_successor import confirmed_rework
            by_id = {task['task_id']:task for task in tasks}
            for original_id, mapping in (op['detail'].get('repair_map') or {}).items():
                if mapping.get('kind') == 'rework' and not confirmed_rework(conn, by_id.get(original_id, {}), mapping.get('request_id')):
                    outcome = ('waiting_human', {'reason':'rework_delivery_unconfirmed'})
                    break
        if outcome is None:
            detail = op['detail']
            if 'first_started_at' in detail and detail['total_deadline_at'] <= now:
                outcome = ('waiting_human', {'reason': 'recovery_total_budget_exhausted'})
            elif detail.get('result_deadline_at') is None:
                outcome = ('waiting_human', {'reason': 'recovery_legacy_budget_unknown'})
            elif detail['result_deadline_at'] <= now and not batch['incomplete']:
                outcome = ('waiting_human', {'reason': 'recovery_no_progress_timeout' if 'first_started_at' in detail else 'recovery_result_timeout'})
        if outcome is None:
            if op['detail'] != prepared_op['detail']:
                conn.execute('UPDATE workflow_recovery_operations SET detail_json=?,next_due_at=?,version=version+1,updated_at=? WHERE id=?',
                             (json.dumps(op['detail']), now + 30, now, operation_id))
                result = _get(conn, operation_id)
                _event(conn, result, 'progress', now, op['detail'])
                return result
            conn.execute('UPDATE workflow_recovery_operations SET next_due_at=? WHERE id=?', (now + 30, operation_id))
            return _get(conn, operation_id)
        status, detail = outcome if isinstance(outcome, tuple) else (outcome, {})
        if status not in ('resolved', 'waiting_human'):
            raise ValueError('invalid recovery result status')
        receipt = _human_wait_detail(op['payload'], status, {**op['detail'], **detail})
        conn.execute('UPDATE workflow_recovery_operations SET status=?,detail_json=?,version=version+1,updated_at=? WHERE id=? AND version=?', (status, json.dumps(receipt), now, operation_id, expected_version))
        result = _get(conn, operation_id)
        _event(conn, result, 'result', now, receipt)
        return result

