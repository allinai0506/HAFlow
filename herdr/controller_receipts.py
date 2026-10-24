"""Pure Controller action receipt classification, shared by all entry points."""
from .controller_actions import controller_action_type


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
    facts = after.get('action_readback') or {}
    if kind == 'confirm_dispatch' and facts.get('dispatch_confirmation'):
        return _receipt(action, wid, 'applied', outcome='success',
            operation_id=facts['dispatch_confirmation']['operation_id'], message='派发确认已持久记录，等待 Controller 派发')
    if kind in {'checkpoint', 'restore_checkpoint', 'step'} and facts.get('control_event'):
        return _receipt(action, wid, 'applied', outcome='success', event_id=facts['control_event']['id'],
            message='操作事件已回读确认' if kind != 'step' else '已核验就绪节点并保持暂停；尚未派发任务')
    if kind == 'rollback' and facts.get('rollback'):
        return _receipt(action, wid, 'applied', outcome='success', message='回溯历史与受影响任务已持久记录')
    task = facts.get('task') if 'task' in facts else next((t for t in after.get('tasks', []) if t.get('task_id') == tid), {})
    if kind in {'resume', 'pause'} and facts.get('workflow_transition'):
        return _receipt(action, wid, 'applied', outcome='success', event_id=facts['workflow_transition']['id'],
                        message='已核验本次动作的权威状态流转事件')
    after_workflow = facts.get('workflow') or after['workflow']
    identity = task if target.get('task_id') else after_workflow
    if (kind != 'launch' and any(value is not None and value != '' and identity.get(key) != value
            for fields in (target, expected) for key, value in fields.items()
            if key in {'run_id', 'execution_id', 'candidate_sha'})):
        return _receipt(action, wid, 'rejected', outcome='failed', reason_code='readback_identity_changed',
                        refresh_required=True)
    if result.get('partial') and result.get('gate_passed') and task.get('stage_verdict') == 'pass':
        return _receipt(action, wid, 'partial', outcome='failed', applied_steps=['force_pass'], failed_step='advance')
    if kind == 'steer':
        steer = facts.get('steer') or {}
        if steer.get('status') in {'pending', 'dispatched', 'completed'}:
            return _receipt(action, wid, 'applied', outcome='success', steer_id=steer['steer_id'],
                            effect='queued' if steer['status'] == 'pending' else 'delivered', message='指导已持久记录')
        if steer.get('status') == 'blocked':
            return _receipt(action, wid, 'partial', outcome='failed', steer_id=steer['steer_id'],
                            applied_steps=['steer_queued'], failed_step='steer_delivery')
    if kind == 'halt':
        event = facts.get('halt_event') or {}
        if (event.get('event_type') == 'steering.task_halted' and task.get('status') == 'interrupted'
                and task.get('interrupted_at') == event.get('timestamp')):
            return _receipt(action, wid, 'applied', outcome='success', event_id=event['id'], effect='interrupted')
        if event.get('event_type') == 'steering.task_halt_failed':
            return _receipt(action, wid, 'rejected', outcome='failed', reason_code='interrupt_failed', refresh_required=True)
    if kind == 'supersede' and task.get('status') == 'superseded':
        return _receipt(action, wid, 'applied', outcome='success', task_id=tid,
                        message='目标工位已按请求作废，替换责任已回读确认')
    if kind == 'close_workflow' and after_workflow.get('status') in {'completed', 'cleaned', 'archived'}:
        return _receipt(action, wid, 'applied', outcome='success', message='工作流终态已回读确认')
    if kind == 'clear_escalation' and tid and task and not task.get('finalize_escalated'):
        return _receipt(action, wid, 'applied', outcome='success', task_id=tid,
                        message='升级标记撤销已回读确认')
    applied = ((kind == 'resume' and after_workflow.get('status') == 'running'
                and before['workflow'].get('status') == 'paused')
               or (kind == 'pause' and after_workflow.get('status') == 'paused'
                   and before['workflow'].get('status') == 'running')
               or (kind in {'force_pass', 'force_pass_advance'} and task.get('stage_verdict') == 'pass')
               or (kind in {'commit', 'integrate', 'cleanup', 'finalize', 'accept_and_commit'}
                   and task.get('status') in {'commit': {'committed', 'integrated', 'cleanup_ready', 'cleaned'},
                                              'integrate': {'integrated', 'cleanup_ready', 'cleaned'},
                                              'cleanup': {'cleaned'}, 'finalize': {'cleanup_ready', 'cleaned'},
                                              'accept_and_commit': {'committed', 'integrated', 'cleanup_ready', 'cleaned'}}[kind]))
    if applied:
        return _receipt(action, wid, 'applied', outcome='success', task_id=tid, message='权威状态已回读确认')
    operations = [op for op in after.get('recovery', []) if kind in {'advance', 'launch', 'rework'}
                  and op.get('status') not in {'resolved', 'superseded'}
                  and ((op.get('payload') or {}).get('task_id') == tid
                       or tid in ((op.get('payload') or {}).get('task_ids') or [])
                       or result.get('operation_id') == op.get('id'))]
    if operations:
        return _receipt(action, wid, 'accepted_waiting', outcome='waiting', task_id=tid,
                        operation_id=operations[0]['id'], message='派发责任已建立，等待 Task 登记' if not task else '等待执行结果')
    return _receipt(action, wid, 'rejected', outcome='skipped', reason_code='effect_unconfirmed',
                    refresh_required=True, message='未取得状态变化或持久派发责任的证据')
