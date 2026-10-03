"""Handoff regressions: truthful adoption, acceptance boundaries, review notes."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from herdr.git_adoption import explain, REFUSED
from tests.test_auto_acceptance import _load_controller, _task, BASELINE_CHANGED

ROOT = Path(__file__).resolve().parents[1]


def test_branch_mismatch_reports_identity_without_inventing_zero_commits():
    message = explain(REFUSED, {'reason': 'current_branch_mismatch', 'commits': 0,
                               'current_branch': 'agent/wrong',
                               'expected_branches': ['task/owned']})
    assert 'actual=agent/wrong' in message
    assert 'expected=task/owned' in message
    assert 'commits=0' not in message


@pytest.mark.parametrize('extra', [
    {'stage_verdict': 'blocked'},
    {'dispatch_role': 'adversarial'},
    {'dispatch_role': 'reviewer'},
])
def test_auto_accept_does_not_complete_blocked_or_review_task(extra):
    ctrl = _load_controller('ctrl_bug1002')
    task = {**_task(), **extra}
    with patch.object(ctrl, 'get_task', return_value=task), \
         patch.object(ctrl, 'node_is_gate', return_value=False), \
         patch.object(ctrl, 'task_changes_recorded', return_value=True), \
         patch.object(ctrl, 'set_task_status') as transition:
        assert ctrl.try_auto_accept('t1') is False
        transition.assert_not_called()


def test_failed_baseline_command_cannot_auto_accept_partial_output():
    ctrl = _load_controller('ctrl_bug1002_baseline')
    with patch.object(ctrl.subprocess, 'run', return_value=subprocess.CompletedProcess(
        [], 1, BASELINE_CHANGED, 'verification failed')):
        assert ctrl.task_changes_recorded(_task()) is False


def test_review_note_cli_roundtrip(tmp_path):
    env = {**os.environ, 'HERDR_WORKFLOW_DOCS_DIR': str(tmp_path / 'docs'),
           'HERDR_STATE_DB': str(tmp_path / 'state.db')}
    result = subprocess.run([sys.executable, str(ROOT / 'bin/herdr-task'), 'note-add',
        '--workflow-id', 'wf-review', '--kind', 'review', '--title', 'Review',
        '--text', 'Blocker BL-1'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    result = subprocess.run([sys.executable, str(ROOT / 'bin/herdr-task'), 'note-list',
        'wf-review', '--json'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'review' in result.stdout and 'Blocker BL-1' in result.stdout


def test_auto_accept_persists_explicit_evidence_and_rejects_stale_snapshot(tmp_path, monkeypatch):
    from herdr.state_store import SQLiteStateStore
    from herdr import kernel
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.delenv('HERDR_CONTROLLER_TEST', raising=False)
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_task({**_task(), 'run_id': 'run-1', 'version': 1})
    ctrl = _load_controller('ctrl_bug1002_store')
    with patch.object(ctrl, '_get_store', return_value=store), \
         patch.object(ctrl, 'get_task', side_effect=store.get_task), \
         patch.object(ctrl, 'node_is_gate', return_value=False), \
         patch.object(ctrl, 'task_changes_recorded', return_value=True):
        assert ctrl.try_auto_accept('t1')
    saved = store.get_task('t1')
    assert saved['status'] == 'completed'
    assert saved['auto_accept_reason'] == 'controlled_changes'
    assert saved['acceptance_mode'] == 'auto'
    assert saved.get('stage_verdict') != 'pass'
    assert any('controlled_changes' in json.dumps(e) for e in store.list_events(task_id='t1'))

    store.save_task({**_task(task_id='t2'), 'run_id': 'run-2', 'version': 1})
    stale = store.get_task('t2')
    def changes_then_block(task):
        result = kernel.transition_task('t2', 'rework', 'concurrent-blocker', store=store)
        assert result.get('accepted', True)
        return True
    with patch.object(ctrl, '_get_store', return_value=store), \
         patch.object(ctrl, 'get_task', return_value=stale), \
         patch.object(ctrl, 'node_is_gate', return_value=False), \
         patch.object(ctrl, 'task_changes_recorded', side_effect=changes_then_block):
        assert ctrl.try_auto_accept('t2') is False
    assert store.get_task('t2')['status'] == 'rework'


@pytest.mark.parametrize('required', ['t1', [42], [''], ['  '], ['t1', 't1']])
def test_required_ids_reject_invalid_config(required):
    from herdr.workflow import normalize_workflow
    with pytest.raises(ValueError, match='required_task_ids'):
        normalize_workflow({'nodes': [{'id': 'implementation', 'required_task_ids': required}]})


def test_ops_exposes_foreign_required_id_without_accepting_it():
    from tests.test_herdr_task_ops_center import _ht
    owned = {**_task(), 'status': 'integrated'}
    foreign = {**_task(task_id='foreign'), 'workflow_id': 'wf-other', 'status': 'cleaned'}
    config = {'nodes': [{'id': 'implementation', 'required_task_ids': ['foreign']}]}
    with patch.object(_ht, '_safe_workflow', return_value=(config, 'Workflow')):
        cards = _ht._build_workflow_cards({'wf-1': [owned], 'wf-other': [foreign]}, 1)
    node = next(c for c in cards if c['workflow_id'] == 'wf-1')['nodes'][0]
    assert node['status'] == 'pending'
    assert node['completion_issues'] == [
        {'task_id': 'foreign', 'reason': 'required_task_out_of_workflow'}]



def test_project_config_rejects_known_foreign_required_task(tmp_path):
    from herdr import projects
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(tmp_path / 'config.db')
    config_file = tmp_path / 'workflow.json'
    config_file.write_text(json.dumps({'nodes': [
        {'id': 'implementation', 'required_task_ids': ['foreign']}]}))
    store.save_workflow({'workflow_id': 'wf-1', 'status': 'running',
                         'workflow_file': str(config_file)})
    store.save_task({**_task(task_id='foreign'), 'workflow_id': 'wf-other'})
    with patch.object(projects, '_get_store', return_value=store):
        with pytest.raises(ValueError, match='required_task_out_of_workflow'):
            projects.workflow_config_for('wf-1')
    config_file.write_text(json.dumps({'nodes': [
        {'id': 'implementation', 'required_task_ids': ['not-yet-dispatched']}]}))
    with patch.object(projects, '_get_store', return_value=store):
        assert projects.workflow_config_for('wf-1')['nodes'][0]['required_task_ids'] == ['not-yet-dispatched']


def test_cli_config_load_does_not_hide_schema_error(tmp_path):
    from tests.test_herdr_task_ops_center import _ht
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(tmp_path / 'cli-config.db')
    path = tmp_path / 'workflow.json'
    path.write_text(json.dumps({'nodes': [{'id': 'implementation', 'required_task_ids': 'bad'}]}))
    store.save_workflow({'workflow_id': 'wf-1', 'status': 'running', 'workflow_file': str(path)})
    with patch.object(_ht, 'get_state_store', return_value=store), \
         patch.object(_ht, 'workflow_config_for', return_value={'nodes': []}):
        with pytest.raises(ValueError, match='required_task_ids'):
            _ht.load_workflow('wf-1')


@pytest.mark.parametrize('enforce', [False, True])
def test_supervisor_log_distinguishes_observation_from_enforcement(enforce):
    from tests.test_supervisor_interception import _config
    from tests.test_semantic_supervisor import ALL_SIGNALS, FakeStore, StubProvider
    from herdr.supervisor.engine import SemanticSupervisor
    from herdr.supervisor.harness import run_checkpoint
    config = _config(enforce)
    signals = dict(ALL_SIGNALS, work_off_track=0.95)
    messages = []
    result = run_checkpoint(task={**_task(), 'runtime': {'status': 'running'}},
        trigger='agent_done', store=FakeStore(), config=config,
        supervisor=SemanticSupervisor(config, StubProvider(signals=signals)),
        actions={'PAUSE': lambda task, decision: None}, log=messages.append)
    assert result['decision']['action'] == 'PAUSE'
    assert result['continue_flow'] is (not enforce)
    assert any(f'mode={"enforce" if enforce else "observe"}' in m for m in messages)


def test_ops_config_error_cannot_look_completed():
    from tests.test_herdr_task_ops_center import _ht
    with patch.object(_ht, 'load_workflow', side_effect=ValueError('required_task_ids invalid')):
        cards = _ht._build_workflow_cards({'wf-1': [{**_task(), 'status': 'integrated'}]}, 1)
    node = cards[0]['nodes'][0]
    assert node['status'] == 'pending'
    assert node['completion_issues'] == [{'task_id': None, 'reason': 'workflow_config_invalid'}]


def test_cli_load_rejects_known_foreign_reference(tmp_path):
    from tests.test_herdr_task_ops_center import _ht
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(tmp_path / 'cli-foreign.db')
    path = tmp_path / 'workflow.json'
    path.write_text(json.dumps({'nodes': [{'id': 'implementation', 'required_task_ids': ['foreign']}]}))
    store.save_workflow({'workflow_id': 'wf-1', 'status': 'running', 'workflow_file': str(path)})
    store.save_task({**_task(task_id='foreign'), 'workflow_id': 'wf-other'})
    with patch.object(_ht, 'get_state_store', return_value=store):
        with pytest.raises(ValueError, match='required_task_out_of_workflow'):
            _ht.load_workflow('wf-1')



def test_worker_retains_identity_if_pane_allocated_before_identity_write_failure(tmp_path, capsys):
    from tests.test_worker_readiness_contract import load
    worker = load('services/herdr-worker.py', 'worker_bug1002')
    root = tmp_path / 'clones'
    argv = ['worker', '--task-id', 't-launch', '--source', str(tmp_path), '--agent', 'codex',
            '--execution-mode', 'context', '--parent-pane', 'parent',
            '--launch-intent-id', 'intent-1', '--run-id', 'run-1']
    from herdr.task_resources import write_worker_launch_identity
    def fail_after_pane(clone, identity, **kwargs):
        if identity.get('pane_id'):
            raise OSError('identity write failed')
        write_worker_launch_identity(clone, identity, **kwargs)
    with patch.object(worker, 'CLONE_ROOT', root), \
         patch.object(worker, 'verify_request_preflight', return_value={'request_verified': True}), \
         patch.object(worker, 'create_pane', return_value='allocated-pane'), \
         patch.object(worker, 'is_task_active_in_registry', return_value=False), \
         patch('herdr.task_resources.write_worker_launch_identity', side_effect=fail_after_pane), \
         patch.object(worker, 'start_agent') as start, patch.object(sys, 'argv', argv):
        with pytest.raises(OSError, match='identity write failed'):
            worker.main()
        start.assert_not_called()
    failure = json.loads(next(line.split('=', 1)[1] for line in capsys.readouterr().err.splitlines()
                             if line.startswith('HERDR_WORKER_FAILURE=')))
    assert failure['recovery_required'] is True
    assert failure['pane_source'] == 'dynamic'
    assert (root / 't-launch' / '.herdr-launch-identity.json').exists()


def test_worker_context_states_assigned_branch_and_no_push(tmp_path):
    from tests.test_worker_readiness_contract import load
    worker = load('services/herdr-worker.py', 'worker_bug1002_context')
    with patch.object(worker, 'measure_complexity_baseline', return_value=1):
        context, _ = worker.write_task_context(tmp_path, 'codex', 'task/assigned')
    text = context.read_text()
    assert 'branch=task/assigned' in text
    assert 'branch_policy=stay_on_assigned_branch' in text
    assert 'delivery_policy=no_push_or_pr_before_integration' in text
