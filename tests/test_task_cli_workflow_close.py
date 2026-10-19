import pytest

from herdr.task_cli_workflow_close import close_workflow_owned


def test_close_unknown_workflow_exits_before_resource_operations():
    calls = []
    with pytest.raises(SystemExit) as error:
        close_workflow_owned(
            'missing', load_tasks=lambda: {'tasks': []},
            load_workflow_entry=lambda workflow_id: (None, []),
            get_store=lambda: calls.append('store'),
            finalize_one=lambda *a, **k: calls.append('finalize'),
            close_pane=lambda pane: calls.append(pane),
            stage_reset=lambda *a: calls.append('reset'),
            mark_workflow_completed=lambda *a, **k: calls.append('complete'),
            workflow_stage_tabs=lambda *a: [], live_workspace_panes=lambda *a: [],
            foreign_panes_in_tab=lambda *a: [], close_owned_pane=lambda *a: False,
            teardown_output_guard=lambda *a: {}, record_output_retention=lambda *a, **k: None,
            clone_root='/tmp',
            teardown_blocking_statuses=set(),
        )
    assert error.value.code == 1
    assert calls == []
