"""Recovery commands emitted to coordinators/humans must work in the real CLI."""
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_CONTROLLER_TEST', '1')
    controller = importlib.import_module('services.herdr-controller')
    store = SQLiteStateStore(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf-command', 'status': 'running'})
    store.save_task({'task_id': 'blocked-command', 'workflow_id': 'wf-command',
                     'status': 'blocked', 'stage': 'implementation', 'node': 'implementation',
                     'agent': 'opencode', 'pane_id': 'owned-test-pane',
                     'sentinel_reason': 'inner_loop_exhausted'})
    return controller, store


def execute_hint(store, message, target='working'):
    task = store.get_task('blocked-command')
    match = re.search(r'herdr-task set blocked-command (\w+)', message)
    assert match, message
    # Only remap the executable to the isolated Candidate; exact emitted args.
    env = os.environ.copy()
    env['HERDR_STATE_DB'] = str(store.db_path)
    env.pop('TASKS_FILE', None)
    env.pop('WORKFLOWS_FILE', None)
    result = subprocess.run([sys.executable, str(ROOT / 'bin/herdr-task'), 'set', task['task_id'], match[1]],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    after = store.get_task(task['task_id'])
    assert after['status'] == target
    assert after['status_history'][-1]['from'] == 'blocked'
    assert after['status_history'][-1]['source'] == 'herdr-task'


def test_arbitration_card_resume_command_executes_without_force(scene):
    controller, store = scene
    message = controller.build_coordinator_message(store.get_task('blocked-command'), 'inner_loop_exhausted')
    execute_hint(store, message)


def test_human_upgrade_resume_command_executes_without_force(scene, monkeypatch):
    controller, store = scene
    notify = Mock(return_value=True)
    notifier = SimpleNamespace(build_console_url=lambda **kw: 'http://localhost/test',
                               notify_human_upgrade=notify)
    original_import = importlib.import_module
    def controlled_import(name, *a, **kw):
        return notifier if name == 'services.herdr-notifier' else original_import(name, *a, **kw)
    monkeypatch.setattr(importlib, 'import_module', controlled_import)
    assert controller._notify_blocked_human_upgrade(store.get_task('blocked-command'), 'episode-test', 1800)
    message = notify.call_args.args[2]  # No real notification leaves the fixture.
    execute_hint(store, message)


def test_arbitration_failure_strategy_still_executes(scene):
    controller, store = scene
    message = controller.build_coordinator_message(store.get_task('blocked-command'), 'inner_loop_exhausted')
    failure = re.search(r'herdr-task set blocked-command failed', message)
    assert failure
    execute_hint(store, failure[0], target='failed')
