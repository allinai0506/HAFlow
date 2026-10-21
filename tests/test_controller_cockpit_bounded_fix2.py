"""Bounded cycle 2 counterexamples: persisted facts and real write boundaries."""
from pathlib import Path
import pytest

from herdr import kernel, node_dispatch_store as dispatch, recovery_store, state_db
from herdr.task_resources import begin_launch_intent, record_launch_resources
from services.controller import cockpit_automation as automation
from tests.test_controller_cockpit_final_fixes import scene
from tests.test_node_dispatch_contract import scene as controller_scene


def policy_config(store, policy):
    wf = store.get_workflow('wf')
    if policy == 'workflow':
        wf['config']['control_policy'] = {'mode': 'supervised'}
    elif policy == 'node':
        wf['config']['nodes'][0]['control_mode'] = 'supervised'
    elif policy == 'launch':
        wf['config']['nodes'][0]['confirm_before'] = ['launch']
    store.save_workflow(wf)


def test_legacy_task_without_workflow_identity_keeps_default_compatibility(tmp_path):
    db = tmp_path / 'legacy.db'
    state_db.save_task({'task_id': 'legacy', 'run_id': 'old-run', 'status': 'completed'}, db_path=db)
    assert state_db.get_task('legacy', db_path=db)['workflow_id'] == 'default'


def test_cockpit_status_epoch_ignores_malformed_legacy_event(scene):
    conn = state_db.get_db_connection(scene.db_path)
    conn.execute("INSERT INTO events(workflow_id,event_type,payload_json,timestamp,source) VALUES(?,?,?,?,?)",
                 ('wf', 'workflow_transition', '{bad', 1, 'legacy'))
    conn.commit()
    view = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    assert view['workflow_id'] == 'wf'


@pytest.mark.parametrize('event_type', ['workflow_transition', 'checkpoint_restored'])
def test_paused_snapshot_treats_malformed_transition_reason_as_unknown(scene, event_type):
    kernel.pause_workflow('wf', store=scene)
    conn = state_db.get_db_connection(scene.db_path)
    conn.execute("INSERT INTO events(workflow_id,event_type,payload_json,timestamp,source) VALUES(?,?,?,?,?)",
                 ('wf', event_type, '{bad', 2, 'legacy'))
    conn.commit()

    workflow, _, _ = recovery_store.read_snapshot(scene.db_path, 'wf')

    assert workflow['status'] == 'paused'
    assert workflow['pause_requires_confirmation'] is (event_type == 'checkpoint_restored')
    assert automation.read_cockpit(scene, 'wf', controller={'status': 'running'})['workflow_id'] == 'wf'


def test_ops_controls_resolve_a_current_cockpit_action_before_execution():
    source = (Path(__file__).parents[1] / 'console/static/console/scripts/operations.js').read_text()
    assert '/api/workflow/controller-cockpit?workflow_id=' in source
    assert 'payload.action_id=action.action_id' in source


def test_task_action_without_current_candidate_identity_is_blocked(scene):
    from herdr.controller_actions import action_automation
    wf = scene.get_workflow('wf')
    wf['candidate_sha'] = 'a' * 40
    task = {'task_id': 'working', 'workflow_id': 'wf', 'node': 'build', 'run_id': 'run',
            'execution_id': 'exec', 'version': 1, 'status': 'working'}
    action = {'api_payload': {'type': 'force_pass_advance', 'task_id': 'working'},
              'target': {'workflow_id': 'wf', 'node_id': 'build', 'task_id': 'working', 'run_id': 'run'},
              'expected_identity': {'task_version': 1, 'execution_id': 'exec'}, 'category': 'bypass'}
    policy, _ = action_automation(action, wf['config'], task, wf)
    assert policy == {'mode': 'blocked', 'source': 'safety', 'reason': 'identity_unknown'}


def test_manual_advance_receives_the_canonical_expected_identity(scene, monkeypatch):
    from console import controller_cockpit_api, control_api
    from herdr.controller_workflow_actions import workflow_control_actions
    wf = scene.get_workflow('wf')
    action = next(a.to_dict() for a in workflow_control_actions(wf, wf['config'], scene.list_tasks('wf'), [], [])
                  if a.action_id == 'wf:advance')
    action['automation'] = {'mode': 'confirmation_required'}
    captured = {}
    monkeypatch.setattr(control_api.workflow_api, 'manual_advance',
                        lambda wid, expected_identity=None: captured.update(wid=wid, identity=expected_identity) or {'ok': True})
    monkeypatch.setattr(controller_cockpit_api, 'execute_action',
                        lambda payload, deps: deps.handlers['advance'](action))
    controller_cockpit_api.execute_controller_action({'workflow_id': 'wf', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'confirmed': True})
    assert captured['identity'] == action['expected_identity']


@pytest.mark.parametrize('policy', ['workflow', 'node', 'launch', 'automatic'])
def test_operationless_registration_obeys_persisted_policy(scene, policy):
    policy_config(scene, policy)
    task = {'task_id': 'unbound', 'workflow_id': 'wf', 'node': 'build',
            'run_id': 'r', 'execution_id': 'exec', 'status': 'working'}
    if policy == 'automatic':
        scene.save_task(task)
        assert scene.get_task('unbound')['run_id'] == 'r'
    else:
        with pytest.raises(ValueError, match='confirmation'):
            scene.save_task(task)
        assert scene.get_task('unbound') is None


@pytest.mark.parametrize('policy', ['workflow', 'node', 'launch', 'automatic'])
def test_operationless_intent_obeys_persisted_policy(scene, policy):
    policy_config(scene, policy)
    def launch():
        return begin_launch_intent(scene, workflow_id='wf', node_id='build', task_id='unbound',
                                   execution_id='exec', run_id='r')
    if policy == 'automatic':
        assert launch()['status'] == 'claimed'
    else:
        with pytest.raises(ValueError, match='confirmation'):
            launch()
        assert scene.list_events(event_type='launch_intent') == []


@pytest.mark.parametrize('policy', ['workflow', 'node', 'launch'])
def test_intake_disabled_admission_retains_confirmation(controller_scene, monkeypatch, policy):
    s = controller_scene
    policy_config(s.store, policy)
    monkeypatch.setenv('HERDR_COORDINATOR_INTAKE', '0')
    s.ctrl._handle_coordinator_item(s.item)
    assert s.sent == []
    op = dispatch.operation_for_node(s.store.db_path, 'wf', 'implementation')
    assert op['status'] == 'waiting_human'
    recovery_store.decide_operation(s.store.db_path, op['id'], op['version'],
        'reviewer', 'confirm_dispatch', 'approved root dispatch', s.clock[0], confirmed=True)
    s.ctrl.check_workflow_stage_advance('wf')
    assert not s.ctrl.coordinator_queue.empty()
    s.ctrl._handle_coordinator_item(s.ctrl.coordinator_queue.get_nowait())
    assert len(s.sent) == 1


def action_for(store, kind):
    return next(a for a in automation.read_cockpit(store, 'wf', controller={'status': 'running'})['actions']
                if a['api_payload'].get('type') == kind)


@pytest.mark.parametrize('kind,entry', [('rollback', 'api_kernel_rollback'),
    ('checkpoint', 'api_kernel_checkpoint_create'), ('restore_checkpoint', 'api_kernel_checkpoint_restore'),
    ('step', 'api_kernel_step')])
def test_workflow_control_rejects_execution_replaced_after_lookup(scene, monkeypatch, kind, entry):
    from console import control_api, runtime
    from console.controller_cockpit_api import execute_controller_action
    monkeypatch.setattr(runtime, 'service_status', lambda: {'com.user.herdr-controller': 'running'})
    monkeypatch.setattr(kernel, 'get_state_store', lambda **kw: scene)
    scene.create_checkpoint('wf')
    action = action_for(scene, kind)
    original = getattr(control_api, entry)
    def interleaved(body):
        wf = scene.get_workflow('wf')
        scene.save_workflow({**wf, 'execution_id': 'new-exec'})
        scene.save_task({'task_id': 'new-task', 'workflow_id': 'wf', 'node': 'build',
            'run_id': 'new-run', 'execution_id': 'new-exec', 'status': 'working'})
        return original(body)
    monkeypatch.setattr(control_api, entry, interleaved)
    result = execute_controller_action({'workflow_id': 'wf', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'confirmed': True, 'reason': 'reviewed'})
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['reason_code'] == 'identity_changed'
    assert scene.get_workflow('wf')['execution_id'] == 'new-exec'
    assert scene.get_task('new-task')['status'] == 'working'
    assert len(scene.list_checkpoints('wf')) == 1


def test_workflow_controls_visible_with_101_tasks(scene):
    for i in range(101):
        scene.save_task({'task_id': f't{i:03}', 'workflow_id': 'wf', 'node': 'build',
            'run_id': f'r{i}', 'pane_id': f'p{i}', 'execution_id': 'exec', 'status': 'working'})
    kernel.pause_workflow('wf', store=scene)
    view = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    assert any(a['action_id'] == 'wf:resume_workflow' for a in view['actions'])
    assert {'checkpoint', 'rollback', 'step'} <= {a['api_payload'].get('type') for a in view['actions']}
    assert len(view['actions']) <= 100
    assert view['actions_truncated']


def test_checkpoint_metadata_change_gets_new_receipt_identical_retry_dedupes(scene, monkeypatch):
    from console import runtime
    from console.controller_cockpit_api import execute_controller_action
    monkeypatch.setattr(runtime, 'service_status', lambda: {'com.user.herdr-controller': 'running'})
    monkeypatch.setattr(kernel, 'get_state_store', lambda **kw: scene)
    def save():
        a = action_for(scene, 'checkpoint')
        return execute_controller_action({'workflow_id': 'wf', 'action_id': a['action_id'],
            'expected_identity': a['expected_identity'], 'confirmed': True})
    first = save()
    assert first['receipt']['status'] == 'applied'
    scene.update_workflow_metadata('wf', {'agent_override': 'claude', 'custom_context': {'scope': 'second'}})
    second = save()
    assert second['receipt']['status'] == 'applied'
    assert second['checkpoint_id'] != first['checkpoint_id']
    snapshot = scene.get_checkpoint('wf', second['checkpoint_id'])
    assert snapshot['workflow']['agent_override'] == 'claude'
    assert save()['duplicate'] is True
    assert len(scene.list_checkpoints('wf')) == 2


def test_restore_receipt_finds_target_a_before_limit_on_later_b_events(scene):
    a = scene.create_checkpoint('wf')['checkpoint_id']
    b = scene.create_checkpoint('wf')['checkpoint_id']
    action = next(x for x in automation.read_cockpit(scene, 'wf', controller={'status': 'running'})['actions']
                  if x['target'].get('checkpoint_id') == a)
    before = automation.read_action_state(scene, action)['action_readback']['event_watermark']
    scene.restore_checkpoint('wf', a)
    scene.restore_checkpoint('wf', b)
    scene.restore_checkpoint('wf', b)
    readback = automation.read_action_state(scene, action, {'checkpoint_id': a}, event_watermark=before)
    assert readback['action_readback']['control_event']['event_type'] == 'checkpoint_restored'


def test_checkpoint_restore_replays_supervised_snapshot_without_new_dispatch(scene):
    scene.save_task({'task_id': 'existing', 'workflow_id': 'wf', 'node': 'build',
        'run_id': 'run-existing', 'execution_id': 'exec', 'status': 'working'})
    policy_config(scene, 'workflow')
    cp = scene.create_checkpoint('wf')['checkpoint_id']
    scene.restore_checkpoint('wf', cp)
    assert scene.get_task('existing')['run_id'] == 'run-existing'
    assert scene.list_events(event_type='launch_intent') == []


def test_replacing_legacy_run_cannot_bypass_supervised_registration(scene):
    scene.save_task({'task_id': 'existing', 'workflow_id': 'wf', 'node': 'build',
        'run_id': 'old-run', 'execution_id': 'exec', 'status': 'working'})
    policy_config(scene, 'workflow')
    with pytest.raises(ValueError, match='confirmation'):
        scene.save_task({**scene.get_task('existing'), 'run_id': 'new-run'})
    assert scene.get_task('existing')['run_id'] == 'old-run'


@pytest.mark.parametrize('kind', ['pause', 'resume'])
def test_transition_rejects_metadata_race_at_write_boundary(scene, kind):
    from types import SimpleNamespace
    if kind == 'resume':
        kernel.pause_workflow('wf', store=scene)
    view = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    action = next(a for a in view['actions'] if automation.controller_action_type(a) == kind)
    actual = automation.default_handlers(scene)[kind]
    def interleaved(current):
        scene.update_workflow_metadata('wf', {'agent_override': 'claude'})
        return actual(current)
    result = automation.execute_action({'workflow_id': 'wf', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'confirmed': True},
        deps=SimpleNamespace(store=scene, handlers={kind: interleaved}))
    assert result['receipt']['reason_code'] == 'identity_changed'
    assert scene.get_workflow('wf')['status'] == ('paused' if kind == 'resume' else 'running')


@pytest.mark.parametrize('kind', ['pause', 'resume'])
def test_legacy_control_endpoint_preserves_expected_identity(scene, kind):
    from console import control_api
    from herdr.workflow_control_store import WorkflowIdentityChanged
    if kind == 'resume':
        kernel.pause_workflow('wf', store=scene)
    view = automation.read_cockpit(scene, 'wf', controller={'status': 'running'})
    action = next(a for a in view['actions'] if automation.controller_action_type(a) == kind)
    scene.update_workflow_metadata('wf', {'execution_id': 'replacement'})
    with pytest.raises(WorkflowIdentityChanged):
        getattr(control_api, 'api_kernel_' + kind)({'workflow_id': 'wf',
            'expected_identity': action['expected_identity']})
    assert scene.get_workflow('wf')['status'] == ('paused' if kind == 'resume' else 'running')


def test_rollback_holds_cross_connection_write_fence_and_rolls_back_all_changes(scene, monkeypatch):
    import sqlite3
    for i in range(2):
        scene.save_task({'task_id': f't{i}', 'workflow_id': 'wf', 'node': 'build',
            'run_id': f'r{i}', 'execution_id': 'exec', 'status': 'working'})
    action = action_for(scene, 'rollback')
    original = state_db.transition_task
    attempts = []
    def interleaved(*args, **kw):
        other = sqlite3.connect(scene.db_path, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        finally:
            other.close()
        attempts.append(args[0])
        if len(attempts) == 2:
            raise RuntimeError('injected second transition failure')
        return original(*args, **kw)
    monkeypatch.setattr(state_db, 'transition_task', interleaved)
    with pytest.raises(RuntimeError, match='second transition'):
        kernel.rollback_workflow('wf', 'build', store=scene, expected_identity=action['expected_identity'])
    assert len(attempts) == 2
    assert [scene.get_task(f't{i}')['status'] for i in range(2)] == ['working', 'working']
    assert scene.list_events(event_type='task_transition') == []


def test_restore_lookup_uses_exact_identity_index(scene):
    conn = state_db.get_readonly_db_connection(scene.db_path)
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM events WHERE workflow_id=? "
            "AND event_type=? AND source=? AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.checkpoint_id')=? "
            "AND id>? AND timestamp>=? ORDER BY id DESC LIMIT 1",
            ('wf', 'checkpoint_restored', 'checkpoint_store', 'A', 0, 0)).fetchall()
        assert any('idx_events_checkpoint_identity' in row['detail'] for row in plan)
        assert not any('TEMP B-TREE' in row['detail'] for row in plan)
    finally:
        conn.close()


def test_step_rechecks_after_business_evidence_without_holding_writer_lock(scene, monkeypatch):
    import sqlite3
    from herdr import business_gate
    from herdr.workflow_control_store import WorkflowIdentityChanged
    action = action_for(scene, 'step')
    def evidence(*args):
        conn = sqlite3.connect(scene.db_path, timeout=0)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE workflows SET title='changed during evidence' WHERE workflow_id='wf'")
            conn.commit()
        finally:
            conn.close()
        return []
    monkeypatch.setattr(business_gate, 'business_gate_blockers', evidence)
    with pytest.raises(WorkflowIdentityChanged):
        kernel.step_workflow('wf', expected_identity=action['expected_identity'])
    assert scene.get_workflow('wf')['status'] == 'running'


def test_policy_change_keeps_resource_evidence_but_denies_new_registration(scene):
    intent = begin_launch_intent(scene, workflow_id='wf', node_id='build', task_id='T',
                                execution_id='exec', run_id='r')['intent']
    policy_config(scene, 'workflow')
    saved = record_launch_resources(scene, intent, {'pane_id': 'p', 'run_id': 'r'})
    assert saved['resources']['pane_id'] == 'p'
    assert scene.list_events(event_type='launch_intent', desc=True)[0]['payload']['resources']['pane_id'] == 'p'
    with pytest.raises(ValueError, match='confirmation'):
        scene.save_task({'task_id': 'T', 'workflow_id': 'wf', 'node': 'build',
            'run_id': 'r', 'execution_id': 'exec', 'status': 'working'})
