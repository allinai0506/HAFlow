"""Workflow-scoped Cockpit read and execution adapter."""
from types import SimpleNamespace
from herdr import kernel
from services.controller.cockpit_automation import default_handlers, execute_action, read_cockpit, reconcile_pending_receipts
from console import runtime


def get_controller_cockpit(workflow_id: str, action_id=None, target_task_id=None) -> dict:
    workflow_id = str(workflow_id or '').strip()
    if not workflow_id:
        raise ValueError('workflow_id 不能为空')
    reconcile_pending_receipts(kernel._get_store(), workflow_id)
    return read_cockpit(kernel._get_store(), workflow_id, action_id=action_id,
                        target_task_id=str(target_task_id or '').strip() or None, controller=lambda: {
        'status': runtime.service_status().get('com.user.herdr-controller') or 'unknown'})


def execute_controller_action(payload: dict) -> dict:
    from console import control_api
    from herdr.controller_actions import controller_action_type
    def handler(action):
        canonical = {**action['api_payload'], **action.get('user_input', {})}
        canonical['expected_version'] = action['expected_identity'].get('task_version')
        canonical['expected_identity'] = action['expected_identity']
        if action['expected_identity'].get('pane_id'):
            canonical['expected_pane_id'] = action['expected_identity']['pane_id']
        kind = controller_action_type(action)
        if kind in {'resume', 'pause'}:
            return default_handlers(kernel._get_store())[kind](action)
        if kind == 'confirm_dispatch':
            import time
            from herdr import recovery_store
            op = recovery_store.decide_operation(kernel._get_store().db_path, action['target']['operation_id'],
                action['expected_identity']['operation_version'], canonical.get('operator'), kind,
                canonical.get('reason'), time.time(), confirmed=canonical.get('confirmed'))
            return {'ok': True, 'operation_id': op['id']}
        controls = {'step': control_api.api_kernel_step, 'rollback': control_api.api_kernel_rollback,
                    'checkpoint': control_api.api_kernel_checkpoint_create,
                    'restore_checkpoint': control_api.api_kernel_checkpoint_restore,
                    'advance': lambda body: control_api.workflow_api.manual_advance(
                        body['workflow_id'], expected_identity=body.get('expected_identity'))}
        if kind in controls:
            return controls[kind](canonical)
        return control_api._execute_legacy_controller_action(canonical)
    kinds = {'launch', 'rework', 'force_pass', 'force_pass_advance', 'redrive', 'steer', 'halt',
             'clear_escalation', 'supersede', 'close_workflow', 'advance', 'pause', 'resume',
             'accept_and_commit', 'commit', 'integrate', 'finalize', 'cleanup',
             'confirm_dispatch', 'step', 'rollback', 'checkpoint', 'restore_checkpoint'}
    return execute_action(payload, deps=SimpleNamespace(store=kernel._get_store(), project=get_controller_cockpit,
                                                       lookup=get_controller_cockpit,
                                                       handlers={kind: handler for kind in kinds}))
