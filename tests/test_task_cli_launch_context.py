from types import SimpleNamespace

import pytest

from herdr.task_cli_launch import prepare_launch_context
from herdr.task_cli_launch import resolve_launch_agent


def test_prepare_launch_context_resolves_workflow_identity_and_run(monkeypatch):
    args = SimpleNamespace(
        agent_policy='{"max_concurrency": 2}', node='build', workflow_id='wf-1',
        run_id=None, execution_id=None,
    )
    data = {'tasks': []}
    project = {'execution_id': 'exec-9'}
    monkeypatch.setattr('herdr.task_cli_launch.json.loads', lambda value: {'max_concurrency': 2})

    result = prepare_launch_context(
        args,
        load_tasks=lambda: data,
        project_for_workflow=lambda workflow_id: project,
        execution_fields_from_workflow=lambda workflow: {'execution_mode': 'git'},
        new_run_id=lambda: 'run-3',
    )

    assert result == (data, 'build', 'run-3', project,
                      '{"max_concurrency": 2}', {'max_concurrency': 2},
                      {'execution_mode': 'git'})
    assert args.execution_id == 'exec-9'


def test_prepare_launch_context_rejects_manual_execution_identity_override():
    args = SimpleNamespace(agent_policy=None, node='build', workflow_id='wf-1',
                           run_id='run-1', execution_id='old-exec')

    with pytest.raises(SystemExit) as error:
        prepare_launch_context(
            args,
            load_tasks=lambda: {'tasks': []},
            project_for_workflow=lambda workflow_id: {'execution_id': 'current-exec'},
            execution_fields_from_workflow=lambda workflow: {},
            new_run_id=lambda: 'unused',
        )

    assert error.value.code == 2


def test_prepare_launch_context_rejects_missing_node():
    args = SimpleNamespace(agent_policy=None, node=None, stage=None, workflow_id='wf-1',
                           run_id='run-1')

    with pytest.raises(SystemExit) as error:
        prepare_launch_context(
            args,
            load_tasks=lambda: {'tasks': []},
            project_for_workflow=lambda workflow_id: {},
            execution_fields_from_workflow=lambda workflow: {},
            new_run_id=lambda: 'unused',
        )

    assert error.value.code == 1


def test_resolve_launch_agent_forwards_dispatch_identity():
    args = SimpleNamespace(
        agent='auto', workflow_id='wf-2', task_type='code', task_id='task-4',
        run_id='run-5', dispatch_role='reviewer', allow_reuse=False,
    )
    calls = []

    result = resolve_launch_agent(
        args, 'review', object(),
        choose_agent=lambda *pos, **kw: calls.append((pos, kw)) or 'codex',
        workflow_config_for=lambda workflow_id: {},
        is_isolation_rejection=lambda error: False,
        record_router_failure_task=lambda *a, **k: None,
        release_agent_reservation=lambda task_id: None,
    )

    assert result == ('codex', 'auto')
    assert calls == [(
        ('wf-2', 'review', 'code', 'auto'),
        {'reservation_key': 'task-4', 'run_id': 'run-5', 'dispatch_role': 'reviewer'},
    )]


def test_resolve_launch_agent_requires_reason_for_reuse_opt_out():
    args = SimpleNamespace(allow_reuse=True, reuse_reason='  ')

    with pytest.raises(SystemExit) as error:
        resolve_launch_agent(
            args, 'review', object(),
            choose_agent=lambda *a, **k: pytest.fail('router must not run'),
            workflow_config_for=lambda workflow_id: {},
            is_isolation_rejection=lambda error: False,
            record_router_failure_task=lambda *a, **k: None,
            release_agent_reservation=lambda task_id: None,
        )

    assert error.value.code == 2
