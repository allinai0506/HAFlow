"""Final review counterexamples over authoritative SQLite, not browser policy."""
import json
from types import SimpleNamespace

import pytest

from herdr import kernel, node_dispatch_store as dispatch, recovery_store, state_db
from herdr.state_store import SQLiteStateStore
from services.controller import cockpit_automation as automation
from tests.test_node_dispatch_contract import scene as controller_scene


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf', 'status': 'running', 'execution_id': 'exec',
        'startup_ready': True, 'config': {'nodes': [{'id': 'build', 'depends_on': []}]}})
    return store


@pytest.mark.parametrize('policy', ['workflow', 'node', 'launch'])
def test_real_dispatch_requires_durable_confirmation_before_claim(scene, policy):
    wf = scene.get_workflow('wf')
    if policy == 'workflow':
        wf['config']['control_policy'] = {'mode': 'supervised'}
    elif policy == 'node':
        wf['config']['nodes'][0]['control_mode'] = 'supervised'
    else:
        wf['config']['nodes'][0]['confirm_before'] = ['launch']
    scene.save_workflow(wf)
    op = dispatch.reconcile_workflow(scene.db_path, 'wf', now=100)[0]
    assert op['status'] == 'waiting_human'
    assert op['detail']['reason'] == 'dispatch_confirmation_required'
    assert dispatch.claim(scene.db_path, op['id'], 'controller', now=101) is None
    # A retry is not a domain confirmation.
    retried = recovery_store.decide_operation(scene.db_path, op['id'], op['version'],
        'operator', 'retry', 'transport checked', 102)
    assert dispatch.claim(scene.db_path, op['id'], 'controller', now=103) is None
    op = dispatch.operation_for_node(scene.db_path, 'wf', 'build')
    approved = recovery_store.decide_operation(scene.db_path, op['id'], op['version'],
        'operator', 'confirm_dispatch', 'reviewed launch scope', 104, confirmed=True)
    reopened = SQLiteStateStore(scene.db_path)
    claimed = dispatch.claim(reopened.db_path, approved['id'], 'controller', now=105)
    assert claimed and claimed['started'] == 0
    started = dispatch.start(reopened.db_path, approved['id'], 'controller', now=106)
    assert started['started'] == 1
    assert dispatch.transport_finished(reopened.db_path, approved['id'], 'controller',
        reason='sent', now=107)['status'] == 'awaiting_result'
    assert dispatch.claim(reopened.db_path, approved['id'], 'other', now=108) is None


def test_automatic_default_still_claims_and_starts(scene):
    op = dispatch.reconcile_workflow(scene.db_path, 'wf', now=100)[0]
    assert op['status'] == 'pending'
    assert dispatch.claim(scene.db_path, op['id'], 'controller', now=101)
    assert dispatch.start(scene.db_path, op['id'], 'controller', now=102)['started']


def test_automatic_sweep_never_undoes_explicit_kernel_pause(scene):
    kernel.pause_workflow('wf', store=scene)
    automation.run_automatic_controller_actions('wf', deps=SimpleNamespace(store=scene))
    assert scene.get_workflow('wf')['status'] == 'paused'
    projection = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    action = next(a for a in projection['actions'] if a['action_id'] == 'wf:resume_workflow')
    assert action['automation']['mode'] == 'confirmation_required'
    result = automation.execute_action({'workflow_id': 'wf', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'confirmed': True}, deps=SimpleNamespace(store=scene))
    assert result['receipt']['status'] == 'applied'
    assert scene.get_workflow('wf')['status'] == 'running'


def test_restoring_an_explicitly_paused_snapshot_does_not_auto_resume(scene):
    kernel.pause_workflow('wf', store=scene)
    checkpoint = scene.create_checkpoint('wf')
    kernel.resume_workflow('wf', store=scene)
    scene.restore_checkpoint('wf', checkpoint['checkpoint_id'])
    automation.run_automatic_controller_actions('wf', deps=SimpleNamespace(store=scene))
    assert scene.get_workflow('wf')['status'] == 'paused'


@pytest.mark.parametrize('confirmed', [None, False, 'true', 1])
@pytest.mark.parametrize('automatic', [False, True])
def test_server_rejects_unconfirmed_supervised_action(scene, confirmed, automatic):
    wf = scene.get_workflow('wf')
    wf.update(status='paused')
    wf['config']['control_policy'] = {'mode': 'supervised'}
    scene.save_workflow(wf)
    action = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})['actions'][0]
    result = automation.execute_action({'workflow_id': 'wf', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'confirmed': confirmed}, deps=SimpleNamespace(store=scene), automatic=automatic)
    assert result['receipt']['reason_code'] == 'confirmation_required'
    assert scene.get_workflow('wf')['status'] == 'paused'


def test_execution_finds_action_beyond_display_cap(scene):
    for i in range(40):
        scene.save_task({'task_id': f't{i:03}', 'workflow_id': 'wf', 'node': 'build',
            'run_id': f'r{i}', 'execution_id': 'exec', 'pane_id': f'p{i}', 'status': 'working'})
    shown = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    assert shown['actions_truncated']
    assert not any(a['action_id'] == 't039:steer' for a in shown['actions'])
    task = scene.get_task('t039')
    result = automation.execute_action({'workflow_id': 'wf', 'action_id': 't039:steer',
        'expected_identity': {'task_version': task['version'], 'pane_id': 'p39', 'execution_id': 'exec'},
        'instruction': 'use the exact target'}, deps=SimpleNamespace(store=scene,
            handlers={'steer': lambda action: {'task_id': action['target']['task_id']}}))
    assert result['receipt'].get('reason_code') != 'action_obsolete'
    assert result['receipt']['task_id'] == 't039'


def test_projection_reads_one_extra_row_to_report_truncation(scene):
    with recovery_store._transaction(scene.db_path) as conn:
        for i in range(101):
            conn.execute('INSERT INTO workflow_recovery_operations '
                '(identity_key,workflow_id,payload_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                (f'op{i}', 'wf', '{}', 'resolved', i, i))
            state_db.record_event({'workflow_id': 'wf', 'event_type': 'test.row',
                'timestamp': i, 'source': 'test', 'payload': {}}, conn=conn)
    result = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    assert len(result['timeline']) == len(result['recovery']) == 100
    assert result['timeline_truncated'] is True
    assert result['recovery_truncated'] is True


def test_unregistered_automatic_actions_offer_honest_manual_execution(scene):
    scene.save_task({'task_id': 'T', 'workflow_id': 'wf', 'node': 'build', 'run_id': 'r',
        'execution_id': 'exec', 'pane_id': 'p', 'status': 'working'})
    result = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    for kind in ('steer', 'redrive'):
        action = next(a for a in result['actions'] if a['action_id'] == f'T:{kind}')
        assert action['automation']['mode'] == 'automatic'
        assert action['execution']['mode'] == 'manual'
        assert action['execution']['reason'] == 'automatic_handler_unavailable'


def test_workflow_controls_and_dispatch_confirmation_have_authoritative_actions(scene, monkeypatch):
    wf = scene.get_workflow('wf')
    wf['config']['nodes'][0]['control_mode'] = 'supervised'
    scene.save_workflow(wf)
    op = dispatch.reconcile_workflow(scene.db_path, 'wf')[0]
    projection = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    kinds = {a['api_payload'].get('type') for a in projection['actions']}
    assert {'pause', 'step', 'rollback', 'checkpoint', 'confirm_dispatch'} <= kinds
    action = next(a for a in projection['actions'] if a['api_payload'].get('type') == 'confirm_dispatch')
    assert action['target']['operation_id'] == op['id']
    from console import runtime
    from console.controller_cockpit_api import execute_controller_action
    monkeypatch.setattr(runtime, 'service_status', lambda: {'com.user.herdr-controller': 'running'})
    result = execute_controller_action({'workflow_id': 'wf', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'confirmed': True,
        'operator': 'reviewer', 'reason': 'verified scope'})
    assert result['receipt']['status'] == 'applied'
    assert dispatch.claim(scene.db_path, op['id'], 'controller')


@pytest.mark.parametrize('mode', ['workflow', 'node', 'launch'])
def test_real_controller_dispatch_waits_for_policy_and_never_resends(controller_scene, mode):
    scene = controller_scene
    wf = scene.store.get_workflow('wf')
    if mode == 'workflow':
        wf['config']['control_policy'] = {'mode': 'supervised'}
    elif mode == 'node':
        wf['config']['nodes'][0]['control_mode'] = 'supervised'
    else:
        wf['config']['nodes'][0]['confirm_before'] = ['launch']
    scene.store.save_workflow(wf)
    scene.ctrl._handle_coordinator_item(scene.item)
    assert scene.sent == []
    op = dispatch.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert op['status'] == 'waiting_human'
    recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
        'reviewer', 'confirm_dispatch', 'reviewed implementation scope', scene.clock[0], confirmed=True)
    scene.ctrl._handle_coordinator_item(scene.item)
    assert len(scene.sent) == 1
    op = dispatch.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert op['status'] == 'awaiting_result'
    scene.ctrl._handle_coordinator_item(scene.item)
    assert len(scene.sent) == 1


def test_confirmation_does_not_authorize_next_generation_or_bypass_registration(scene):
    wf = scene.get_workflow('wf')
    wf['config']['control_policy'] = {'mode': 'supervised'}
    scene.save_workflow(wf)
    op = dispatch.reconcile_workflow(scene.db_path, 'wf', now=100)[0]
    with pytest.raises(ValueError, match='confirmation'):
        recovery_store.decide_operation(scene.db_path, op['id'], op['version'],
            'operator', 'confirm_dispatch', 'scope', 101, confirmed='true')
    # Simulate an inherited pre-policy operation: transport state alone is not approval.
    with recovery_store._transaction(scene.db_path) as conn:
        conn.execute('UPDATE workflow_recovery_operations SET status=?,started=1 WHERE id=?', ('running', op['id']))
        with pytest.raises(ValueError, match='confirmation required'):
            dispatch.validate_task_registration(conn, {'workflow_id': 'wf', 'task_id': 'T',
                'node': 'build', 'dispatch_operation_id': op['id']}, 102)
        conn.execute('UPDATE workflow_recovery_operations SET status=?,started=0 WHERE id=?', ('waiting_human', op['id']))
    recovery_store.decide_operation(scene.db_path, op['id'], op['version'],
        'operator', 'confirm_dispatch', 'scope', 103, confirmed=True)
    wf['execution_id'] = 'new-execution'
    scene.save_workflow(wf)
    new = dispatch.reconcile_workflow(scene.db_path, 'wf', now=104)[0]
    assert new['id'] != op['id']
    assert new['status'] == 'waiting_human'
    assert dispatch.claim(scene.db_path, new['id'], 'controller', now=105) is None


def test_policy_changed_after_claim_is_rechecked_before_start(scene):
    op = dispatch.reconcile_workflow(scene.db_path, 'wf', now=100)[0]
    dispatch.claim(scene.db_path, op['id'], 'controller', now=101)
    wf = scene.get_workflow('wf')
    wf['config']['control_policy'] = {'mode': 'supervised'}
    scene.save_workflow(wf)
    with pytest.raises(ValueError, match='confirmation required'):
        dispatch.start(scene.db_path, op['id'], 'controller', now=102)


def test_confirmed_unsent_lease_recovery_keeps_approval_and_terminal_cleanup(scene):
    wf = scene.get_workflow('wf')
    wf['config']['control_policy'] = {'mode': 'supervised'}
    scene.save_workflow(wf)
    op = dispatch.reconcile_workflow(scene.db_path, 'wf', now=100)[0]
    recovery_store.decide_operation(scene.db_path, op['id'], op['version'],
        'reviewer', 'confirm_dispatch', 'scope reviewed', 101, confirmed=True)
    assert dispatch.claim(scene.db_path, op['id'], 'crashed-owner', now=102)
    dispatch.reconcile_workflow(scene.db_path, 'wf', discover=False, now=763)
    assert dispatch.claim(scene.db_path, op['id'], 'replacement-owner', now=764)
    assert dispatch.start(scene.db_path, op['id'], 'replacement-owner', now=765)['started']
    # A newly discovered, unconfirmed generation must still retire on completion.
    wf['execution_id'] = 'next-generation'
    scene.save_workflow(wf)
    next_op = dispatch.reconcile_workflow(scene.db_path, 'wf', now=766)[0]
    scene.save_workflow({**wf, 'status': 'completed'})
    dispatch.reconcile_workflow(scene.db_path, 'wf', discover=False, now=767)
    assert dispatch.operation_for_node(scene.db_path, 'wf', 'build')['status'] == 'superseded'
