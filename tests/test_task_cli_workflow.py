import json

from herdr.task_cli_workflow import node_status, stage_reset, supersede_task


def test_safe_workflow_uses_injected_loader_and_fails_closed():
    from herdr.task_cli_tasks import safe_workflow

    assert safe_workflow('wf-1', load_workflow=lambda _: {'name': 'Workflow'}) == (
        {'name': 'Workflow'}, 'Workflow')
    assert safe_workflow('wf-1', load_workflow=lambda _: {}) == ({}, 'wf-1')
    assert safe_workflow('wf-1', load_workflow=lambda _: (_ for _ in ()).throw(ValueError())) == (
        {'configuration_error': 'workflow_config_invalid'}, 'wf-1')


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


def test_stage_reset_removes_only_the_selected_workflow_stage(tmp_path):
    from pathlib import Path

    state_path = tmp_path / 'stage-state.json'
    state_path.write_text(json.dumps({
        'wf-1:build': {'phase': 'queued'}, 'wf-1:test': {'phase': 'notified'},
        'wf-2:test': {'phase': 'queued'},
    }))
    stage_reset(
        'wf-1', 'test', stage_state_file=str(state_path),
        path_exists=lambda path: Path(path).exists(),
    )
    assert json.loads(state_path.read_text()) == {
        'wf-1:build': {'phase': 'queued'}, 'wf-2:test': {'phase': 'queued'},
    }
