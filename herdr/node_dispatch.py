"""Pure first-node dispatch identity, registration evidence and wait projection."""
import hashlib
import json

WAIT_SECONDS = 900
CHECK_SECONDS = 30
TERMINAL = frozenset({'resolved', 'superseded'})


def active(workflow):
    return (workflow.get('status') == 'running' and workflow.get('startup_ready') is not False
            and not workflow.get('startup_not_ready') and not workflow.get('completed_at')
            and workflow.get('outcome') not in {'delivered', 'abandoned'})


def payload(workflow, config, node):
    identity = {'workflow_id': workflow['workflow_id'],
                'generation': [workflow.get('execution_id'), workflow.get('created_at'), workflow.get('reopened_at')],
                'node_id': node['id'],
                'config_hash': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()}
    return {**identity, 'kind': 'node_dispatch', 'responsible': 'coordinator',
            'recovery_responsible': 'controller',
            'required_task_ids': node.get('required_task_ids'),
            'identity_key': 'node_dispatch:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()}


def current(operation, workflow, config):
    node = next((n for n in config.get('nodes') or [] if n.get('id') == operation['payload']['node_id']), None)
    return bool(node and not node.get('depends_on') and node.get('node_type', 'agent') == 'agent'
                and payload(workflow, config, node)['identity_key'] == operation['identity_key'])


def registered_tasks(operation, workflow, tasks):
    facts = operation['payload']
    # Legacy generations lacking execution identity cannot prove a new launch.
    if not workflow.get('execution_id'):
        return []
    return [t for t in tasks if t.get('workflow_id') == operation['workflow_id']
            and (t.get('node') or t.get('stage')) == facts['node_id']
            and t.get('execution_id') == workflow['execution_id']
            and t.get('dispatch_operation_id') == operation['id']
            and t.get('task_id') and t.get('run_id') and t.get('launch_intent_id')
            and t.get('status') != 'superseded' and not t.get('superseded_by')]


def result(operation, workflow, config, tasks, now):
    if not current(operation, workflow, config):
        return ('waiting_human' if operation['started'] else 'superseded', {'reason': 'dispatch_generation_changed'})
    if not active(workflow):
        return None
    registered = registered_tasks(operation, workflow, tasks)
    required = operation['payload'].get('required_task_ids')
    ids = sorted(t['task_id'] for t in registered)
    valid_required = required is None or (isinstance(required, list) and bool(required)
                     and len(required) <= 64 and all(isinstance(t, str) and t.strip() for t in required))
    if registered and valid_required and (required is None or set(required).issubset(ids)):
        return 'resolved', {'reason': 'dispatch_registered', 'registered_task_ids': ids,
                            'registered_runs': {t['task_id']: t['run_id'] for t in registered}}
    if operation['detail'].get('deadline_at', float('inf')) <= now:
        return 'waiting_human', {'reason': 'dispatch_task_missing',
                                'decision_needed': '核对已有派发与需求；结果未知时先核验现有任务，禁止直接重发'}
    return None


def wait_projection(operation, now):
    facts, detail = operation['payload'], operation['detail']
    deadline = detail.get('deadline_at')
    stalled = operation['status'] == 'waiting_human' or bool(deadline and deadline <= now)
    return {'is_stalled': stalled, 'stall_type': 'node_dispatch' if stalled else None,
            'message': f"节点 {facts['node_id']} 等待{'人工核对派发与需求' if stalled else '总指挥建立任务'}",
            'suggested_action': None, 'target_task_id': None,
            'dispatch': {'operation_id': operation['id'], 'node_id': facts['node_id'],
                         'status': operation['status'], 'responsible': facts['responsible'],
                         'recovery_responsible': facts['recovery_responsible'],
                         'next_due_at': operation['next_due_at'], 'deadline_at': deadline,
                         'reason': detail.get('reason'), 'decision_needed': detail.get('decision_needed')}}
