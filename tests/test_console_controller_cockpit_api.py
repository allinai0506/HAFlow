"""Cockpit reads use the selected SQLite store, never JSON inventories."""
import pytest
from herdr.state_store import SQLiteStateStore
from herdr import recovery_store


@pytest.fixture
def cockpit_store(tmp_path, monkeypatch):
    from console import runtime
    store = SQLiteStateStore(tmp_path / 'state.db')
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setattr(runtime, 'service_status', lambda: {'com.user.herdr-controller': 'running'})
    for wid in ('wf-1', 'foreign'):
        store.save_workflow({'workflow_id': wid, 'status': 'running', 'execution_id': 'exec-1',
                             'config': {'nodes': [{'id': 'build', 'node_type': 'agent'}]}})
    for tid, wid in [('T1', 'wf-1'), ('T2', 'wf-1'), ('foreign-task', 'foreign')]:
        store.save_task({'task_id': tid, 'workflow_id': wid, 'node': 'build', 'stage': 'build',
                         'run_id': tid + '-run', 'execution_id': 'exec-1', 'version': 2,
                         'status': 'working', 'pane_id': tid + '-pane'})
        store.record_event(event_type='test.fact', workflow_id=wid, task_id=tid, payload={'fact': tid})
    recovery_store.reconcile(store.db_path, 'wf-1')
    return store


def test_cockpit_scopes_real_snapshot_tasks_events_and_actions(cockpit_store):
    from console.controller_cockpit_api import get_controller_cockpit
    payload = get_controller_cockpit('wf-1')
    assert payload['workflow']['workflow_id'] == 'wf-1'
    assert {t['task_id'] for n in payload['nodes'] for t in n['tasks']} == {'T1', 'T2'}
    assert all(a['target']['workflow_id'] == 'wf-1' for a in payload['actions'])
    assert {e['task_id'] for e in payload['timeline'] if e['event_type'] == 'test.fact'} == {'T1', 'T2'}
    assert payload['read_state'] == 'confirmed'


def test_cockpit_reads_two_tasks_and_durable_recovery_in_same_workflow(cockpit_store):
    from console.controller_cockpit_api import get_controller_cockpit
    cockpit_store.save_task({**cockpit_store.get_task('T2'), 'status': 'failed', 'stage_verdict': 'blocked'})
    recovery_store.reconcile(cockpit_store.db_path, 'wf-1')
    payload = get_controller_cockpit('wf-1')
    assert {task['task_id'] for task in payload['tasks']} == {'T1', 'T2'}
    assert payload['recovery']
    assert all(operation['workflow_id'] == 'wf-1' for operation in payload['recovery'])
    assert not any(action['api_payload'].get('type') in {'launch', 'rework', 'force_pass_advance'}
                   for action in payload['actions'])


def test_optional_runtime_failure_keeps_authoritative_snapshot(cockpit_store, monkeypatch):
    from console import runtime
    from console.controller_cockpit_api import get_controller_cockpit
    def fail():
        raise OSError('runtime unavailable')
    monkeypatch.setattr(runtime, 'service_status', fail)
    payload = get_controller_cockpit('wf-1')
    assert {t['task_id'] for t in payload['tasks']} == {'T1', 'T2'}
    assert payload['read_state'] == 'unknown'
    assert 'controller' in payload['errors']
    assert all(a['automation']['mode'] == 'blocked' for a in payload['actions'])


def test_runtime_probe_cannot_mix_new_task_versions_into_old_graph(cockpit_store, monkeypatch):
    from console import runtime
    from console.controller_cockpit_api import get_controller_cockpit
    old_version = cockpit_store.get_task('T1')['version']
    def concurrent_change():
        cockpit_store.update_task_metadata('T1', {'pane_id': 'new-pane'})
        return {'com.user.herdr-controller': 'running'}
    monkeypatch.setattr(runtime, 'service_status', concurrent_change)
    cockpit = get_controller_cockpit('wf-1')
    task = next(t for t in cockpit['tasks'] if t['task_id'] == 'T1')
    action = next(a for a in cockpit['actions'] if a['action_id'] == 'T1:halt')
    assert task['version'] == old_version
    assert action['expected_identity']['task_version'] == old_version
    assert action['target']['pane_id'] == 'T1-pane'
    assert cockpit_store.get_task('T1')['pane_id'] == 'new-pane'


def test_action_id_executes_server_resume_and_persists_receipt(cockpit_store):
    from console import control_api
    from console.controller_cockpit_api import get_controller_cockpit
    from herdr import kernel
    kernel.pause_workflow('wf-1', store=cockpit_store)
    action = next(a for a in get_controller_cockpit('wf-1')['actions'] if a['action_id'] == 'wf-1:resume_workflow')
    result = control_api.api_controller_execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'type': 'supersede',
        'api_endpoint': '/malicious', 'commands': [['supersede', 'T1']], 'automation': {'mode': 'automatic'}})
    assert result['receipt']['status'] == 'applied'
    assert cockpit_store.get_workflow('wf-1')['status'] == 'running'
    assert cockpit_store.get_task('T1')['status'] == 'working'
    assert get_controller_cockpit('wf-1')['receipts'][0] == result['receipt']


def test_action_id_rejects_stale_task_pane_without_mutation(cockpit_store):
    from console import control_api
    from console.controller_cockpit_api import get_controller_cockpit
    action = next(a for a in get_controller_cockpit('wf-1')['actions'] if a['action_id'] == 'T1:halt')
    cockpit_store.update_task_metadata('T1', {'pane_id': 'new-pane'})
    result = control_api.api_controller_execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']})
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['refresh_required']
    assert cockpit_store.get_task('T1')['status'] == 'working'


def test_receipt_dispatch_waits_for_real_responsibility_and_force_pass_partial(cockpit_store):
    from services.controller.cockpit_automation import classify_receipt, read_cockpit
    before = read_cockpit(cockpit_store, 'wf-1', controller={'status': 'running'})
    action = {'action_id': 'T1:launch', 'target': {'workflow_id': 'wf-1', 'task_id': 'T1'},
              'api_payload': {'type': 'launch'}}
    after = {**before, 'recovery': [{'id': 12, 'status': 'pending', 'payload': {'task_id': 'new-task'}}]}
    waiting = classify_receipt(action, before, after, {'ok': True, 'task_id': 'new-task'})
    assert waiting['status'] == 'accepted_waiting'
    assert waiting['operation_id'] == 12
    assert classify_receipt(action, before, before, {'ok': True, 'task_id': 'new-task'})['status'] == 'rejected'
    # Real gate mutation/readback; the failed advance must not erase applied work.
    from herdr import kernel
    kernel.force_pass_gate('wf-1', gate_node_id='build', task_id='T1', note='reviewed',
                           expected_version=cockpit_store.get_task('T1')['version'], store=cockpit_store)
    after = read_cockpit(cockpit_store, 'wf-1', controller={'status': 'running'})
    action['api_payload']['type'] = 'force_pass_advance'
    partial = classify_receipt(action, before, after, {'partial': True, 'gate_passed': True, 'task_id': 'T1'})
    assert partial['status'] == 'partial'
    assert partial['applied_steps'] == ['force_pass']
    assert partial['failed_step'] == 'advance'


def test_partial_force_pass_receipt_is_returned_by_real_control_boundary(cockpit_store, monkeypatch):
    from console import control_api, workflow_api
    def fail(wid):
        raise RuntimeError('controlled advance failure')
    monkeypatch.setattr(workflow_api, 'manual_advance', fail)
    result = control_api.api_controller_execute_action({'workflow_id': 'wf-1', 'type': 'force_pass_advance',
        'task_id': 'T1', 'stage': 'build', 'gate_node_id': 'build',
        'expected_version': cockpit_store.get_task('T1')['version'], 'expected_pane_id': 'T1-pane',
        'confirmed': True, 'reason': '人工核查产物后确认'})
    assert result['receipt']['status'] == 'partial'
    assert result['receipt']['applied_steps'] == ['force_pass']
    assert result['receipt']['failed_step'] == 'advance'
    assert cockpit_store.get_task('T1')['stage_verdict'] == 'pass'
    from console.controller_cockpit_api import get_controller_cockpit
    assert get_controller_cockpit('wf-1')['receipts'][0] == result['receipt']


def test_review_distinct_steer_intents_and_retry_have_truthful_queue_receipts(cockpit_store, monkeypatch):
    from console import control_api
    from console.controller_cockpit_api import get_controller_cockpit
    from herdr import steering
    from types import SimpleNamespace
    monkeypatch.setattr(steering, 'get_agent_adapter', lambda agent: SimpleNamespace(supports_soft_steer=True))
    action = next(a for a in get_controller_cockpit('wf-1')['actions'] if a['action_id'] == 'T1:steer')
    request = {'workflow_id': 'wf-1', 'action_id': action['action_id'],
               'expected_identity': action['expected_identity'], 'instruction': 'first instruction'}
    first = control_api.api_controller_execute_action(request)
    retry = control_api.api_controller_execute_action(request)
    second = control_api.api_controller_execute_action({**request, 'instruction': 'second instruction'})
    queued = cockpit_store.list_steers(task_id='T1')
    assert {item['instruction'] for item in queued} == {'first instruction', 'second instruction'}
    assert len(queued) == 2
    assert first['receipt']['status'] == second['receipt']['status'] == 'applied'
    assert retry['receipt'] == first['receipt']
    assert first['receipt']['steer_id'] != second['receipt']['steer_id']


@pytest.mark.parametrize('interrupt_ok', [True, False])
def test_review_halt_uses_own_interrupt_evidence_not_recovery(cockpit_store, monkeypatch, interrupt_ok):
    from console import control_api
    from console.controller_cockpit_api import get_controller_cockpit
    from herdr import steering
    from types import SimpleNamespace
    monkeypatch.setattr(steering, 'get_agent_adapter', lambda agent: SimpleNamespace(
        supports_interrupt=True, protocol_level='tty', name='test', interrupt=lambda *a, **kw: interrupt_ok))
    cockpit_store.save_task({**cockpit_store.get_task('T1'), 'stage_verdict': 'blocked'})
    recovery_store.reconcile(cockpit_store.db_path, 'wf-1')
    action = next(a for a in get_controller_cockpit('wf-1')['actions'] if a['action_id'] == 'T1:halt')
    result = control_api.api_controller_execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'reason': 'reviewed interrupt'})
    assert result['receipt']['status'] == ('applied' if interrupt_ok else 'rejected')
    assert cockpit_store.get_task('T1')['status'] == ('interrupted' if interrupt_ok else 'working')
    assert result['receipt'].get('operation_id') is None


@pytest.mark.parametrize('kind', ['steer', 'halt'])
def test_review_legacy_intervention_receipt_uses_exact_operation_readback(cockpit_store, monkeypatch, kind):
    from console import control_api
    from herdr import steering
    from types import SimpleNamespace
    monkeypatch.setattr(steering, 'get_agent_adapter', lambda agent: SimpleNamespace(
        supports_soft_steer=True, supports_interrupt=True, protocol_level='tty', name='test', interrupt=lambda *a, **kw: True))
    result = control_api.api_controller_execute_action({'type': kind, 'workflow_id': 'wf-1', 'task_id': 'T1',
        'instruction': 'legacy intent', 'reason': 'legacy interrupt'})
    assert result['receipt']['status'] == 'applied'


def test_review_legacy_post_effect_read_failure_is_uncertain_and_reconciles(cockpit_store, monkeypatch):
    from console import control_api
    from console.controller_cockpit_api import get_controller_cockpit
    from herdr import steering
    from services.controller import cockpit_automation as automation
    from types import SimpleNamespace
    monkeypatch.setattr(steering, 'get_agent_adapter', lambda agent: SimpleNamespace(supports_soft_steer=True))
    original, reads = automation.read_cockpit, 0
    def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError('post-effect read unavailable')
        return original(*args, **kwargs)
    monkeypatch.setattr(automation, 'read_cockpit', read)
    result = control_api.api_controller_execute_action({'type': 'steer', 'workflow_id': 'wf-1',
        'task_id': 'T1', 'instruction': 'must survive readback failure'})
    assert result['receipt']['status'] == 'accepted_waiting'
    assert result['cockpit']['read_state'] == 'unknown'
    assert get_controller_cockpit('wf-1')['receipts'][0]['status'] == 'applied'


def test_review_legacy_handler_exception_after_queue_keeps_unknown_receipt(cockpit_store, monkeypatch):
    from console import control_api, task_api
    from console.controller_cockpit_api import get_controller_cockpit
    from herdr import steering
    from types import SimpleNamespace
    monkeypatch.setattr(steering, 'get_agent_adapter', lambda agent: SimpleNamespace(supports_soft_steer=True))
    real = task_api.api_task_steer
    def effect_then_error(payload):
        real(payload)
        raise OSError('handler ended after committed queue')
    monkeypatch.setattr(task_api, 'api_task_steer', effect_then_error)
    with pytest.raises(OSError) as error:
        control_api.api_controller_execute_action({'type': 'steer', 'workflow_id': 'wf-1', 'task_id': 'T1',
            'instruction': 'committed before exception'})
    assert error.value.receipt['status'] == 'accepted_waiting'
    assert error.value.receipt['outcome'] == 'unknown'
    assert get_controller_cockpit('wf-1')['receipts'][0]['status'] == 'applied'
