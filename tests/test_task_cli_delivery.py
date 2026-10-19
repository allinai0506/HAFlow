import pytest

from herdr.task_cli_delivery import commit_task


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
