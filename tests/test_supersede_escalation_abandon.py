"""The console supersede escalation route executes end to end (real CLI).

A finalize_escalated task's content is quarantined from candidate assembly,
so an explicit human abandon discards it safely. Covers the privileged
``escalation-abandon`` transition, the console action's command shape, the
guard boundaries (non-escalated post-delivery tasks stay protected) and the
real adapter → CLI → persistence → readback chain with receipt settlement.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from herdr.liveness import EpisodeStore
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


def env(tmp_path):
    return dict(os.environ, HOME=str(tmp_path), HERDR_STATE_DB=str(tmp_path / 'state.db'),
                TASKS_FILE=str(tmp_path / 'tasks.json'), WORKFLOWS_FILE=str(tmp_path / 'workflows.json'),
                STAGE_STATE_FILE=str(tmp_path / 'stage-state.json'),
                HERDR_ATTENTION_FILE=str(tmp_path / 'attention.json'))


def cli(tmp_path, *args):
    return subprocess.run([sys.executable, str(ROOT / 'bin/herdr-task'), 'supersede', *args],
                          env=env(tmp_path), text=True, capture_output=True, timeout=60)


def seed(tmp_path, status='committed', escalated=True):
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf-1', 'status': 'running', 'execution_id': 'exec-1',
                         'config': {'nodes': [{'id': 'build', 'node_type': 'agent'}]}})
    store.save_task({'workflow_id': 'wf-1', 'task_id': 'T1', 'node': 'build', 'stage': 'build',
                     'run_id': 'T1-run', 'execution_id': 'exec-1', 'version': 1, 'candidate_sha': 'c' * 40,
                     'status': status,
                     **({'finalize_escalated': True, 'finalize_escalate_reason': 'rebase conflict'}
                        if escalated else {})})
    return store


def test_escalated_task_abandons_via_real_cli(tmp_path):
    store = seed(tmp_path, status='committed')
    EpisodeStore(env(tmp_path)['HERDR_ATTENTION_FILE']).upsert('T1:finalize', {'attempts': 3})

    result = cli(tmp_path, 'T1', '--abandon', '--reason', 'operator discards quarantined output')
    assert result.returncode == 0, (result.stdout, result.stderr)
    task = store.get_task('T1')
    assert task['status'] == 'superseded'
    assert task.get('finalize_escalated') is False
    assert task.get('supersede_reason') == 'operator discards quarantined output'
    assert EpisodeStore(env(tmp_path)['HERDR_ATTENTION_FILE']).get('T1:finalize') is None


@pytest.mark.parametrize('status', ['integrated', 'completed'])
def test_non_escalated_post_delivery_supersede_stays_rejected(tmp_path, status):
    store = seed(tmp_path, status=status, escalated=False)
    result = cli(tmp_path, 'T1', '--abandon', '--reason', 'not allowed without escalation')
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert store.get_task('T1')['status'] == status


def test_escalated_supersede_still_requires_explicit_abandon(tmp_path):
    store = seed(tmp_path, status='committed')
    result = cli(tmp_path, 'T1', '--reason', 'no abandon flag')
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert store.get_task('T1')['status'] == 'committed'


def test_escalation_action_command_carries_abandon():
    from herdr.controller_actions import _escalation_actions
    task = {'task_id': 'T1', 'node': 'build', 'stage': 'build', 'finalize_escalated': True,
            'finalize_escalate_reason': 'conflict'}
    action = next(a for a in _escalation_actions(task, 'wf-1') if a.action_id == 'T1:supersede')
    assert action.commands[0][:2] == ['supersede', 'T1']
    assert '--abandon' in action.commands[0] and '--reason' in action.commands[0]


@pytest.fixture
def console_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('HERDR_ATTENTION_FILE', str(tmp_path / 'attention.json'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    monkeypatch.setenv('STAGE_STATE_FILE', str(tmp_path / 'stage-state.json'))
    from console import runtime
    monkeypatch.setattr(runtime, 'service_status', lambda: {'com.user.herdr-controller': 'running'})
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf-1', 'status': 'running', 'execution_id': 'exec-1',
                         'config': {'nodes': [{'id': 'build', 'node_type': 'agent'}]}})
    store.save_task({'workflow_id': 'wf-1', 'task_id': 'T1', 'node': 'build', 'stage': 'build',
                     'run_id': 'T1-run', 'execution_id': 'exec-1', 'version': 1, 'candidate_sha': 'c' * 40,
                     'status': 'committed', 'finalize_escalated': True,
                     'finalize_escalate_reason': 'rebase conflict'})
    return store


def test_console_supersede_escalation_action_executes_end_to_end(console_env):
    from console.controller_cockpit_api import execute_controller_action
    from services.controller import cockpit_automation as automation
    projection = automation.read_cockpit(console_env, 'wf-1', controller={'status': 'running'})
    action = next(a for a in projection['actions'] if a['action_id'] == 'T1:supersede')

    result = execute_controller_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'reason': 'human superseded from console',
        'confirmed': True, 'operator': 'human_console'})

    receipt = result['receipt']
    assert receipt['status'] == 'applied', receipt
    assert receipt['task_id'] == 'T1'
    assert console_env.get_task('T1')['status'] == 'superseded'
    assert console_env.get_task('T1').get('finalize_escalated') is False
