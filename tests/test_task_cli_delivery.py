import pytest

from herdr.task_cli_delivery import cleanup_task, commit_task, purge_task


def test_commit_task_missing_task_exits_without_git_calls():
    calls = []
    with pytest.raises(SystemExit) as error:
        commit_task(
            'missing',
            load_tasks=lambda: {'tasks': []},
            find_task=lambda data, task_id: None,
            get_store=lambda: calls.append('store'),
            is_internal_untracked=lambda path: False,
            adopt_head_if_attributable=lambda *a, **k: None,
            set_status=lambda *a, **k: None,
            run_commit_index_command=lambda *a, **k: None,
        )
    assert error.value.code == 1
    assert calls == []


@pytest.mark.parametrize('handler', [cleanup_task, purge_task])
def test_resource_cleanup_missing_task_exits_without_mutation(handler):
    calls = []
    dependencies = {
        'load_tasks': lambda: {'tasks': []},
        'find_task': lambda data, task_id: None,
        'get_store': lambda: calls.append('store'),
    }
    if handler is cleanup_task:
        dependencies['set_status'] = lambda *a, **k: calls.append('status')
    else:
        dependencies.update(
            teardown_blocking_statuses=set(),
            delete_clone_safely=lambda path: calls.append(path),
            run_command=lambda *a, **k: calls.append('command'),
            path_exists=lambda path: False,
        )
    with pytest.raises(SystemExit) as error:
        handler('missing', **dependencies)
    assert error.value.code == 1
    assert calls == []
