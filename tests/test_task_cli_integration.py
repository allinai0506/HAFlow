import pytest

from herdr.task_cli_integration import integrate_task


def test_integrate_task_missing_task_exits_without_git_operations():
    calls = []
    with pytest.raises(SystemExit) as error:
        integrate_task(
            'missing',
            load_tasks=lambda: {'tasks': []},
            find_task=lambda data, task_id: None,
            get_store=lambda: calls.append('store'),
            ensure_branch_available=lambda *a, **k: calls.append('branch'),
            branch_ownership_error=RuntimeError,
            set_status=lambda *a, **k: calls.append('status'),
            clear_finalize_escalation=lambda *a: None,
            publish_integrated_task=lambda *a, **k: None,
            git_head_sha=lambda *a: None,
            git_is_ancestor=lambda *a: False,
            current_workspace_fingerprint=lambda *a: {},
            project_for_workflow=lambda *a: {},
        )
    assert error.value.code == 1
    assert calls == []
