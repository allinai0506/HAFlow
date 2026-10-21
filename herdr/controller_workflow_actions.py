"""Pure action inventory for existing workflow controls and dispatch approval."""
import hashlib
import json

from .controller_actions import ControllerAction, _bind_actions


def workflow_control_actions(workflow, config, tasks, operations, checkpoints):
    wid = workflow['workflow_id']
    actions = []
    fingerprint = hashlib.sha256(json.dumps({
        'config': config, 'status': workflow.get('status'),
        'tasks': sorted((t['task_id'], t.get('version')) for t in tasks),
        'history': workflow.get('history', []),
    }, sort_keys=True).encode()).hexdigest()

    def add(kind, title, *, node=None, checkpoint=None, operation=None, destructive=False):
        suffix = str(operation['id']) if operation else checkpoint or node or ''
        action = ControllerAction(action_id=f'{wid}:{kind}' + (':' + suffix if suffix else ''),
            title=title, description=title, category='recovery', command_line='',
            api_endpoint='/api/controller/execute-action',
            api_payload={'type': kind, 'workflow_id': wid}, is_destructive=destructive,
            group='workflow', recommended=kind == 'confirm_dispatch')
        _bind_actions([action], {}, workflow)
        action.expected_identity['workflow_fingerprint'] = fingerprint
        if node:
            action.target['node_id'] = node
            action.api_payload['target_node_id'] = node
        if checkpoint:
            action.target['checkpoint_id'] = checkpoint
            action.api_payload['checkpoint_id'] = checkpoint
        if operation:
            action.target.update(operation_id=operation['id'], node_id=operation['payload']['node_id'])
            action.expected_identity['operation_version'] = operation['version']
            action.api_payload['operation_id'] = operation['id']
        actions.append(action)

    if workflow.get('status') == 'running':
        add('pause', '暂停自动调度')
        add('advance', '重新检查阶段并尝试推进')
    if workflow.get('status') in {'running', 'paused'}:
        add('step', '检查下一就绪节点并保持暂停')
        add('checkpoint', '保存工作流快照')
        for node in config.get('nodes') or []:
            add('rollback', '回溯至 ' + str(node.get('label') or node['id']), node=node['id'], destructive=True)
        for cp in checkpoints:
            add('restore_checkpoint', '恢复快照 ' + str(cp.get('tag') or cp['checkpoint_id']),
                checkpoint=cp['checkpoint_id'], destructive=True)
    for op in operations:
        if (op['status'] == 'waiting_human' and not op['started']
                and op.get('detail', {}).get('reason') == 'dispatch_confirmation_required'):
            add('confirm_dispatch', '确认启动节点 ' + op['payload']['node_id'], operation=op)
    return actions
