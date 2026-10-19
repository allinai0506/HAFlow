import json

from herdr.task_cli_workflow import node_status


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
