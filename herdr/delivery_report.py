"""Read-only projection of bound execution evidence; never infer deployment."""
from . import state_db
from .observation import ObservationStore
from .task_checkpoint import has_business_acceptance, read_task_checkpoints


def _verification(task, candidate, store):
    unknown = {'status': 'unknown', 'reason': 'no_bound_execution_evidence'}
    if not task.get('run_id'):
        return unknown
    rows = state_db.list_trajectory_events(task['run_id'], event_type='verification_completed',
                                          task_id=task['task_id'], limit=1, desc=True, db_path=store.db_path)
    if not rows:
        return unknown
    event = rows[0]
    fact = (event.get('payload') or {}).get('verification')
    if not isinstance(fact, dict):
        return unknown
    result = {key: fact.get(key) for key in ('command', 'counts', 'execution_mode', 'environment',
                                            'runtime_release', 'observation_id', 'evidence_id', 'epoch', 'exit_code')}
    result.update(event_id=event.get('event_id') or f"evt_{event.get('id')}", run_id=task['run_id'],
                  candidate_sha=fact.get('candidate_sha'), status='unknown',
                  observed_result='pass' if fact.get('passed') is True else 'fail' if fact.get('passed') is False else 'unknown')
    if task.get('completion_protocol') == 'receipt-v1' and (not task.get('completion_epoch') or fact.get('epoch') != task.get('completion_epoch')):
        result['reason'] = 'execution_epoch_mismatch'
    elif not candidate or fact.get('candidate_sha') != candidate or task.get('candidate_sha') != candidate:
        result['reason'] = 'candidate_mismatch_or_unknown'
    elif fact.get('execution_mode') != 'execute':
        result['reason'] = 'execution_not_attested'
    elif fact.get('passed') is False:
        result['status'] = 'fail'
    elif fact.get('passed') is True:
        observation = state_db.get_observation(str(fact.get('observation_id') or ''), db_path=store.db_path)
        if (observation and observation.get('run_id') == task['run_id']
                and observation.get('task_id') == task['task_id'] and observation.get('sha256')
                and task.get('verified_candidate_sha') == candidate
                and ObservationStore(store.db_path).verify(observation['observation_id']).get('valid')):
            result['status'] = 'pass'
            result['artifact_hash'] = observation['sha256']
        else:
            result['reason'] = 'artifact_or_verified_candidate_missing'
    return result


def build_delivery_report(workflow_id, store):
    workflow = store.get_workflow(workflow_id)
    if workflow is None:
        raise ValueError('Unknown workflow')
    candidate = workflow.get('candidate_sha')
    tasks = []
    records = state_db.list_tasks(workflow_id=workflow_id, limit=1001, db_path=store.db_path)
    truncated = len(records) > 1000
    for task in records[:1000]:
        evaluation = store.get_latest_eval_result(task['run_id']) if task.get('run_id') else None
        if evaluation and (evaluation.get('task_id') != task['task_id'] or evaluation.get('workflow_id') != workflow_id):
            evaluation = None
        checkpoints = {'status': 'unknown', 'segments': []}
        if task.get('run_id') and task.get('completion_epoch'):
            try:
                checkpoints = read_task_checkpoints(task['task_id'], task['run_id'], task['completion_epoch'], store=store)
            except (ValueError, OSError):
                checkpoints = {'status': 'invalid', 'segments': []}
        business = 'pass' if has_business_acceptance(store, task, candidate) else 'unknown'
        tasks.append({'task_id': task['task_id'], 'run_id': task.get('run_id'), 'status': task['status'],
                      'candidate_sha': task.get('candidate_sha'), 'stage_verdict': task.get('stage_verdict'),
                      'verification': _verification(task, candidate, store),
                      'evaluation': evaluation, 'business_acceptance': business, 'checkpoints': checkpoints,
                      'outcome': state_db.get_execution_outcome(task['task_id'], task['run_id'], db_path=store.db_path)
                      if task.get('run_id') else None})
    gates = store.list_events(workflow_id=workflow_id, event_type='join_gate_verdict', limit=100, desc=True)
    return {'schema_version': 1, 'workflow_id': workflow_id, 'workflow_status': workflow['status'],
            'candidate_sha': candidate, 'tasks': tasks, 'tasks_truncated': truncated, 'gates': gates,
            'all_verifications_passed': not truncated and bool(tasks) and all(t['verification']['status'] == 'pass' for t in tasks),
            'business_acceptance': 'pass' if not truncated and bool(tasks) and all(t['business_acceptance'] == 'pass' for t in tasks) else 'unknown',
            'merge_status': 'unknown', 'deployment_status': 'unknown', 'production_validation': 'unknown',
            'limitations': ['Completion declarations do not certify delivery or production acceptance.',
                            'Generic runner scores do not certify business acceptance.',
                            'Unattested execution mode, candidate, artifact, or runtime remains unknown.']}
