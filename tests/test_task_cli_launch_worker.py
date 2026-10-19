from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr.task_cli_launch_worker import run_launch_worker


def test_run_launch_worker_returns_identity_and_records_resources(monkeypatch, tmp_path):
    recorded = []
    intent = {'intent_id': 'intent-1'}
    output = SimpleNamespace(
        returncode=0,
        stdout='noise\nHERDR_WORKER_RESULT={"clone":"/tmp/clone","branch":"agent/t",'
               '"pane_id":"pane-1"}\n',
        stderr='',
    )
    monkeypatch.setattr(
        'herdr.task_resources.record_launch_resources',
        lambda store, current, resources: recorded.append((store, current, resources)) or current,
    )
    command = []

    result, updated_intent = run_launch_worker(
        SimpleNamespace(task_id='t-1', workflow_id='wf-1', source=str(tmp_path),
                        task_type='code', candidate_sha='sha-1'),
        'build', 'run-1', object(), intent, tmp_path / 'clone', 'codex',
        {'execution_mode': 'git'}, 'main', 'agent/base', 'anchor', None,
        Path('/herdr'), run_worker=lambda cmd, **kwargs: command.extend([cmd, kwargs]) or output,
        get_store=lambda: None,
    )

    assert result == {'clone': '/tmp/clone', 'branch': 'agent/t', 'pane_id': 'pane-1'}
    assert updated_intent is intent
    assert command[0][1:8] == ['--task-id', 't-1', '--source', str(tmp_path),
                              '--agent', 'codex', '--task-type']
    assert recorded[0][2] == {
        'clone_path': '/tmp/clone', 'pane_id': 'pane-1', 'pane_source': 'dynamic',
        'run_id': 'run-1', 'agent_session_id': None,
    }


def test_run_launch_worker_timeout_marks_uncertain_resources(tmp_path):
    class Store:
        events = []

        def record_event(self, *args, **kwargs):
            self.events.append((args, kwargs))

    store = Store()
    import subprocess

    with pytest.raises(SystemExit) as error:
        run_launch_worker(
            SimpleNamespace(task_id='t-1', workflow_id='wf-1', source=str(tmp_path),
                            task_type='code'),
            'build', 'run-1', store, {'intent_id': 'intent-1'}, tmp_path / 'clone',
            'codex', {'execution_mode': 'git'}, 'main', None, 'anchor', None,
            Path('/herdr'),
            run_worker=lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired('worker', 300)),
            get_store=lambda: store,
        )

    assert error.value.code == 75
    assert store.events[0][0][0] == 'launch_recovery_required'
    assert store.events[0][0][1]['side_effects'] == 'unknown'
