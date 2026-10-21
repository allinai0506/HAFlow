"""Workflow-scoped Cockpit read and execution adapter."""
from types import SimpleNamespace
from herdr import kernel
from services.controller.cockpit_automation import default_handlers, execute_action, read_cockpit, reconcile_pending_receipts
from console import runtime


def get_controller_cockpit(workflow_id: str) -> dict:
    workflow_id = str(workflow_id or '').strip()
    if not workflow_id:
        raise ValueError('workflow_id 不能为空')
    reconcile_pending_receipts(kernel._get_store(), workflow_id)
    return read_cockpit(kernel._get_store(), workflow_id, controller=lambda: {
        'status': runtime.service_status().get('com.user.herdr-controller') or 'unknown'})


def execute_controller_action(payload: dict) -> dict:
    from console import control_api
    from herdr.controller_actions import controller_action_type
    def handler(action):
        canonical = {**action['api_payload'], **action.get('user_input', {})}
        canonical['expected_version'] = action['expected_identity'].get('task_version')
        if action['expected_identity'].get('pane_id'):
            canonical['expected_pane_id'] = action['expected_identity']['pane_id']
        kind = controller_action_type(action)
        if kind in {'resume', 'pause'}:
            return default_handlers(kernel._get_store())[kind](action)
        return control_api._execute_legacy_controller_action(canonical)
    kinds = {'launch', 'rework', 'force_pass', 'force_pass_advance', 'redrive', 'steer', 'halt',
             'clear_escalation', 'supersede', 'close_workflow', 'advance', 'pause', 'resume',
             'accept_and_commit', 'commit', 'integrate', 'finalize', 'cleanup'}
    return execute_action(payload, deps=SimpleNamespace(store=kernel._get_store(), project=get_controller_cockpit,
                                                       handlers={kind: handler for kind in kinds}))
