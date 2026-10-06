"""Archived panes remain recoverable; IDs alone never authorize teardown."""
import json
from types import SimpleNamespace

import pytest
from herdr import kernel
from herdr.state_store import get_state_store
from tests.test_fix_loop_pr1 import _load_module, HERDR_ROOT


@pytest.fixture
def resource_scene(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    store = get_state_store(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf', 'project_id': 'p', 'status': 'running'})
    task = {'task_id': 'task', 'workflow_id': 'wf', 'node': 'plan', 'stage': 'plan',
            'status': 'working', 'run_id': 'run-fixture', 'pane_id': 'pane',
            'pane_source': 'dynamic', 'agent': 'codex',
            'runtime': {'pane_id': 'pane', 'agent_session_id': 'session', 'agent': 'codex'}}
    store.save_task(task)
    return store


@pytest.mark.parametrize('status', ['failed', 'superseded'])
def test_archive_marks_orphan_in_same_transition(resource_scene, status):
    kernel.transition_task('task', status, reason="fixture", store=resource_scene)
    task = resource_scene.get_task('task')
    assert task['pane_lifecycle'] == 'orphaned'
    assert task['pane_id'] == 'pane'
    assert task['runtime']['pane_id'] == 'pane'


def probe(task, **kwargs):
    return {'status': 'available', 'reason': 'identity_match', 'pane_id': 'pane'}


def test_reap_closes_owned_archived_pane_and_is_idempotent(resource_scene):
    from herdr.task_resources import reap_task_pane
    kernel.transition_task('task', 'superseded', reason="fixture", store=resource_scene)
    closed = []
    close = lambda pid: closed.append(pid) or True
    assert reap_task_pane(resource_scene, 'task', apply=True, probe=probe, close=close)['action'] == 'released'
    task = resource_scene.get_task('task')
    assert task['pane_id'] is None and task['runtime']['pane_id'] is None
    assert task['released_pane_id'] == 'pane'
    assert reap_task_pane(resource_scene, 'task', apply=True, probe=probe, close=close)['action'] == 'already_released'
    assert closed == ['pane']


@pytest.mark.parametrize('verdict', [
    {'status': 'unknown', 'reason': 'probe_failed'},
    {'status': 'unavailable', 'reason': 'identity_mismatch'},
    {'status': 'available', 'reason': 'pane_alive'},
])
def test_reap_never_closes_unproven_instance(resource_scene, verdict):
    from herdr.task_resources import reap_task_pane
    kernel.transition_task('task', 'failed', reason="fixture", store=resource_scene)
    closed = []
    row = reap_task_pane(resource_scene, 'task', apply=True,
        probe=lambda *a, **k: verdict, close=lambda pid: closed.append(pid) or True)
    assert row['action'] == 'retained'
    assert closed == [] and resource_scene.get_task('task')['pane_id'] == 'pane'


@pytest.mark.parametrize('kind', ['prebuilt', 'shared', 'active'])
def test_reap_protects_prebuilt_shared_or_active_pane(resource_scene, kind):
    from herdr.task_resources import reap_task_pane
    if kind != 'active':
        kernel.transition_task('task', 'superseded', reason="fixture", store=resource_scene)
    if kind == 'prebuilt':
        resource_scene.update_task_metadata('task', {'pane_source': 'prebuilt'})
    if kind == 'shared':
        resource_scene.save_task({'task_id': 'live-owner', 'workflow_id': 'other',
                                 'status': 'working', 'pane_id': 'pane'})
    closed = []
    row = reap_task_pane(resource_scene, 'task', apply=True, probe=probe,
                        close=lambda pid: closed.append(pid) or True)
    assert row['action'] == 'retained' and closed == []


def test_reap_failure_remains_retryable(resource_scene):
    from herdr.task_resources import reap_task_pane
    kernel.transition_task('task', 'failed', reason="fixture", store=resource_scene)
    row = reap_task_pane(resource_scene, 'task', apply=True, probe=probe, close=lambda pid: False)
    assert row['action'] == 'retained'
    assert resource_scene.get_task('task')['pane_id'] == 'pane'
    assert reap_task_pane(resource_scene, 'task', apply=True, probe=probe, close=lambda pid: True)['action'] == 'released'


def test_rework_same_task_preserves_identity_and_resources(resource_scene, monkeypatch):
    cli = _load_module('resource_rework_cli', HERDR_ROOT / 'bin/herdr-task')
    kernel.transition_task('task', 'blocked', reason="fixture", store=resource_scene)
    from herdr import task_resources
    monkeypatch.setattr(task_resources, 'probe_live_runtime', probe)
    sent = []
    monkeypatch.setattr(cli, '_herdr', lambda *a: sent.append(a) or SimpleNamespace(returncode=0))
    args = SimpleNamespace(task_id='task', prompt='address review findings', reason='review blocked')
    cli.cmd_rework(args)
    task = resource_scene.get_task('task')
    assert task['status'] == 'rework' and task['run_id'] == 'run-fixture'
    assert task['pane_id'] == 'pane'
    assert len(resource_scene.list_tasks()) == 1
    assert sent[0][:3] == ('agent', 'prompt', 'pane')


def test_blocked_default_action_is_rework_with_same_task():
    from herdr.controller_actions import generate_controller_actions
    task = {'task_id': 'task', 'workflow_id': 'wf', 'node': 'plan',
            'status': 'blocked', 'pane_id': 'pane'}
    actions = generate_controller_actions(task, {'workflow_id': 'wf'})
    recommended = [a for a in actions if a.recommended]
    assert recommended[0].action_id == 'task:rework'
    assert recommended[0].new_task_id == ''
    assert 'rework task' in recommended[0].command_line


def test_console_rework_action_is_executable_and_scoped(resource_scene, monkeypatch):
    from console import herdr_factory_console as console
    monkeypatch.setattr(console, 'tasks_for_workflow', lambda wid: resource_scene.list_tasks(workflow_id=wid))
    calls = []
    monkeypatch.setattr(console, 'run', lambda cmd, *a, **kw:
        calls.append(cmd) or SimpleNamespace(stdout='reused', returncode=0))
    result = console.api_controller_execute_action({'type': 'rework', 'workflow_id': 'wf', 'task_id': 'task'})
    assert result['ok'] is True
    assert calls[0][1:] == ['rework', 'task']
    with pytest.raises(RuntimeError):
        console.api_controller_execute_action({'type': 'rework', 'workflow_id': 'other', 'task_id': 'task'})



def test_console_managed_blocked_rework_requires_recovery_decision(resource_scene, monkeypatch):
    from console import herdr_factory_console as console
    kernel.transition_task('task', 'blocked', reason='fixture', store=resource_scene)
    monkeypatch.setattr(console, 'tasks_for_workflow', lambda wid: resource_scene.list_tasks(workflow_id=wid))
    def forbid(*args, **kwargs):
        raise AssertionError('persistent recovery must own blocked task rework')
    monkeypatch.setattr(console, 'run', forbid)
    with pytest.raises(RuntimeError, match='持久恢复'):
        console.api_controller_execute_action({'type':'rework','workflow_id':'wf','task_id':'task'})
    assert resource_scene.get_task('task')['status'] == 'blocked'

def test_rework_transport_failure_is_retryable_without_task_allocation(resource_scene, monkeypatch):
    cli = _load_module('resource_retry_cli', HERDR_ROOT / 'bin/herdr-task')
    kernel.transition_task('task', 'blocked', reason='fixture', store=resource_scene)
    from herdr import task_resources
    monkeypatch.setattr(task_resources, 'probe_live_runtime', probe)
    monkeypatch.setattr(cli, '_herdr', lambda *a: SimpleNamespace(returncode=1))
    args = SimpleNamespace(task_id='task', prompt='fix', reason='review')
    with pytest.raises(SystemExit):
        cli.cmd_rework(args)
    assert resource_scene.get_task('task')['rework_delivery'] == 'pending'
    monkeypatch.setattr(cli, '_herdr', lambda *a: SimpleNamespace(returncode=0))
    cli.cmd_rework(args)
    assert len(resource_scene.list_tasks()) == 1
    assert resource_scene.get_task('task')['rework_delivery'] == 'delivered'


def test_reap_missing_after_close_recovers_crash_without_closing_again(resource_scene):
    from herdr.task_resources import reap_task_pane
    kernel.transition_task('task', 'superseded', reason='fixture', store=resource_scene)
    missing = lambda *a, **kw: {'status': 'unavailable', 'reason': 'pane_missing'}
    def forbidden(pid):
        raise AssertionError('already absent pane must not be closed')
    assert reap_task_pane(resource_scene, 'task', apply=True, probe=missing, close=forbidden)['action'] == 'released'
    assert resource_scene.get_task('task')['runtime']['pane_id'] is None


def test_reap_never_closes_anchor_even_if_task_claims_dynamic(resource_scene):
    from herdr.task_resources import reap_task_pane
    kernel.transition_task('task', 'superseded', reason='fixture', store=resource_scene)
    resource_scene.update_workflow_metadata('wf', {'config': {'nodes': [{'id': 'plan', 'anchor_pane_id': 'pane'}]}})
    row = reap_task_pane(resource_scene, 'task', apply=True, probe=probe,
        close=lambda pid: pytest.fail('anchor cannot be closed'))
    assert row['reason'] == 'anchor_or_coordinator_pane'


def test_controller_fix_loop_reuses_active_retry_task(monkeypatch):
    ctl = _load_module('pane_rework_controller', HERDR_ROOT / 'services/herdr-controller.py')
    tasks = [{'task_id': 'impl', 'workflow_id': 'wf', 'node': 'implementation',
              'status': 'blocked', 'pane_id': 'pane'},
             {'task_id': 'review', 'workflow_id': 'wf', 'node': 'review',
              'status': 'blocked'}]
    monkeypatch.setattr(ctl, 'load_tasks', lambda: tasks)
    monkeypatch.setattr(ctl, 'get_task', lambda tid: next(t for t in tasks if t['task_id'] == tid))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        task = next(t for t in tasks if t['task_id'] == argv[2])
        task['status'] = 'rework' if argv[1] == 'rework' else 'superseded'
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(ctl.subprocess, 'run', run)
    monkeypatch.setattr(ctl, '_record_invalidation_note', lambda *a, **kw: None, raising=False)
    cfg = {'nodes': [{'id': 'implementation'}, {'id': 'review', 'depends_on': ['implementation']}]}
    ctl.invalidate_for_fix_loop('wf', 'review', cfg, retry_node='implementation')
    assert tasks[0]['status'] == 'rework'
    assert any(c[1:3] == ['rework', 'impl'] for c in calls)
    assert not any(c[1:3] == ['supersede', 'impl'] for c in calls)


def test_selective_redelivery_accepts_same_task_rework():
    from herdr.fix_loop import redelivery_handled
    assert redelivery_handled([{'task_id': 'impl', 'node': 'implementation',
        'status': 'rework', 'created_at': 1, 'rework_started_at': 20,
        'rework_delivery': 'delivered', 'rework_request_id': 'this-gate'}],
        'implementation', 100, ['impl'], {'impl': 'this-gate'})


def test_explicit_launch_replacement_can_link_distinct_runs(resource_scene):
    cli = _load_module('replacement_run_cli', HERDR_ROOT / 'bin/herdr-task')
    resource_scene.save_task({'task_id': 'replacement', 'workflow_id': 'wf', 'node': 'plan',
        'status': 'working', 'run_id': 'new-run'})
    cli.supersede_task('task', new_task_id='replacement', reason='old worker cannot continue', allow_new_run=True)
    old = resource_scene.get_task('task')
    assert old['status'] == 'superseded' and old['superseded_by'] == 'replacement'
    assert old['run_id'] == 'run-fixture'
    assert resource_scene.get_task('replacement')['run_id'] == 'new-run'


def test_rework_request_delivery_is_idempotent(resource_scene, monkeypatch):
    import herdr.task_resources as resources
    cli = _load_module('rework_token_cli', HERDR_ROOT / 'bin/herdr-task')
    monkeypatch.setattr(resources, 'owned_live_pane', lambda task: (True, 'identity_match'))
    delivered = []
    monkeypatch.setattr(cli, '_herdr', lambda *args: delivered.append(args) or SimpleNamespace(returncode=0))
    args = SimpleNamespace(task_id='task', prompt='fix', reason='review', request_id='gate-1')
    cli.cmd_rework(args)
    cli.cmd_rework(args)
    assert len(delivered) == 1
    assert resource_scene.get_task('task')['rework_request_id'] == 'gate-1'


def test_controller_same_gate_retry_reuses_original(monkeypatch):
    ctl = _load_module('same_gate_rework_controller', HERDR_ROOT / 'services/herdr-controller.py')
    task = {'task_id': 'review', 'workflow_id': 'wf', 'node': 'review', 'status': 'blocked', 'stage_verdict': 'blocked'}
    monkeypatch.setattr(ctl, 'load_tasks', lambda: [task])
    calls = []
    def rework(record, gate):
        calls.append((record['task_id'], gate))
        record['status'] = 'rework'
        return True, ''
    monkeypatch.setattr(ctl, '_rework_retry_task', rework)
    monkeypatch.setattr(ctl, '_record_invalidation_note', lambda *a: None)
    result = ctl.invalidate_for_fix_loop('wf', 'review', {'nodes': [{'id': 'review'}]}, retry_node='review')
    assert result == ['review'] and calls == [('review', 'review')]
    assert task['status'] == 'rework'


def test_same_gate_failed_delivery_remains_retryable(resource_scene, monkeypatch):
    import herdr.task_resources as resources
    cli = _load_module('pending_gate_rework_cli', HERDR_ROOT / 'bin/herdr-task')
    ctl = _load_module('pending_gate_rework_ctl', HERDR_ROOT / 'services/herdr-controller.py')
    kernel.update_task_metadata('task', {'stage_verdict': 'blocked', 'stage_verdict_note': 'fix boundary'}, store=resource_scene)
    monkeypatch.setattr(resources, 'owned_live_pane', lambda task: (True, 'identity_match'))
    outcomes = iter([1, 0])
    monkeypatch.setattr(cli, '_herdr', lambda *a: SimpleNamespace(returncode=next(outcomes)))
    monkeypatch.setattr(ctl, 'load_tasks', lambda: resource_scene.list_tasks(workflow_id='wf'))
    monkeypatch.setattr(ctl, 'get_task', resource_scene.get_task)
    monkeypatch.setattr(ctl, '_record_invalidation_note', lambda *a: None)
    def run(argv, **kw):
        args = SimpleNamespace(task_id=argv[2], request_id=argv[4], reason=argv[6], prompt=argv[8])
        try:
            cli.cmd_rework(args)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        except SystemExit as exc:
            return SimpleNamespace(returncode=exc.code, stdout='', stderr='delivery pending')
    monkeypatch.setattr(ctl.subprocess, 'run', run)
    cfg = {'nodes': [{'id': 'plan'}]}
    assert ctl.invalidate_for_fix_loop('wf', 'plan', cfg, retry_node='plan') == []
    pending = resource_scene.get_task('task')
    assert pending['stage_verdict'] == 'blocked' and pending['rework_delivery'] == 'pending'
    assert ctl.invalidate_for_fix_loop('wf', 'plan', cfg, retry_node='plan') == ['task']
    done = resource_scene.get_task('task')
    assert done['rework_request_id'] == pending['rework_request_id']
    assert done['rework_delivery'] == 'delivered' and not done['stage_verdict']
    assert done['pane_id'] == 'pane' and done['run_id'] == 'run-fixture'


def test_same_gate_partial_delivery_does_not_repeat_success(resource_scene, monkeypatch):
    import herdr.task_resources as resources
    cli = _load_module('partial_gate_cli', HERDR_ROOT / 'bin/herdr-task')
    ctl = _load_module('partial_gate_ctl', HERDR_ROOT / 'services/herdr-controller.py')
    kernel.update_task_metadata('task', {'stage_verdict': 'blocked'}, store=resource_scene)
    sibling = {**resource_scene.get_task('task'), 'task_id': 'task-b', 'pane_id': 'pane-b',
               'runtime': {'pane_id': 'pane-b'}}
    resource_scene.save_task(sibling)
    monkeypatch.setattr(resources, 'owned_live_pane', lambda task: (True, 'identity_match'))
    prompts = []
    b_attempts = []
    def prompt(*args):
        prompts.append(args[2])
        if args[2] == 'pane-b':
            b_attempts.append(1)
            return SimpleNamespace(returncode=1 if len(b_attempts) == 1 else 0)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli, '_herdr', prompt)
    monkeypatch.setattr(ctl, 'load_tasks', lambda: resource_scene.list_tasks(workflow_id='wf'))
    monkeypatch.setattr(ctl, 'get_task', resource_scene.get_task)
    monkeypatch.setattr(ctl, '_record_invalidation_note', lambda *a: None)
    def run(argv, **kw):
        try:
            cli.cmd_rework(SimpleNamespace(task_id=argv[2], request_id=argv[4], reason=argv[6], prompt=argv[8]))
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        except SystemExit as exc:
            return SimpleNamespace(returncode=exc.code, stdout='', stderr='pending')
    monkeypatch.setattr(ctl.subprocess, 'run', run)
    cfg = {'nodes': [{'id': 'plan'}]}
    ctl.invalidate_for_fix_loop('wf', 'plan', cfg, retry_node='plan')
    ctl.invalidate_for_fix_loop('wf', 'plan', cfg, retry_node='plan')
    assert prompts.count('pane') == 1
    assert prompts.count('pane-b') == 2


def test_rework_receipt_does_not_erase_new_agent_verdict(resource_scene, monkeypatch):
    import herdr.task_resources as resources
    cli = _load_module('receipt_race_cli', HERDR_ROOT / 'bin/herdr-task')
    kernel.update_task_metadata('task', {'stage_verdict': 'blocked', 'stage_verdict_note': 'old'}, store=resource_scene)
    monkeypatch.setattr(resources, 'owned_live_pane', lambda task: (True, 'identity_match'))
    def prompt(*args):
        kernel.update_task_metadata('task', {'stage_verdict': 'blocked', 'stage_verdict_note': 'new agent result'}, store=resource_scene)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli, '_herdr', prompt)
    cli.cmd_rework(SimpleNamespace(task_id='task', request_id='gate', reason='review', prompt='fix'))
    task = resource_scene.get_task('task')
    assert task['stage_verdict'] == 'blocked'
    assert task['stage_verdict_note'] == 'new agent result'
