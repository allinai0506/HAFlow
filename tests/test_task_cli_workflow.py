import json

from herdr.task_cli_workflow import node_status, supersede_task


def test_node_status_empty_node_uses_resolved_labels(capsys):
    node_status(
        'wf-1', 'build',
        resolve_node=lambda node_id, workflow_id: {
            'node_label': 'Build', 'stage_label': 'Build', 'depends_on': ['plan'], 'next': 'review',
        },
        load_tasks=lambda: {'tasks': []},
        safe_workflow=lambda workflow_id: ({}, workflow_id),
        normalize_workflow_nodes=lambda workflow: [],
        node_delivery_complete=lambda *args: False,
    )
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'empty'
    assert result['node_label'] == 'Build'
    assert result['depends_on'] == ['plan']


def test_supersede_missing_task_exits_before_store_access():
    calls = []
    import pytest
    with pytest.raises(SystemExit) as error:
        supersede_task(
            'missing', new_task_id='replacement', load_tasks=lambda: {'tasks': []},
            find_task=lambda data, task_id: None,
            get_store=lambda: calls.append('store'), transitions={},
            sync_tasks_projection=lambda **kwargs: None,
            autosave_clone_wip=lambda task: None,
        )
    assert error.value.code == 1
    assert calls == []
