from types import SimpleNamespace
from unittest.mock import patch

from herdr import scheduler
from services.controller.stage_state import is_node_complete


def test_dynamic_implementation_waits_for_every_task_in_current_dispatch():
    workflow_id = "wf-dynamic-stage"
    tasks = [
        {
            "task_id": "impl-1",
            "workflow_id": workflow_id,
            "node": "implementation",
            "status": "integrated",
            "integration_mode": "git",
        }
    ]
    ports = SimpleNamespace(
        load_tasks=lambda: tasks,
        workflow_config_for=lambda _wid: {"nodes": [{"id": "implementation"}]},
        scheduler_core=scheduler,
        _reverification_satisfies_node=lambda _wid, _node: False,
        _get_store=lambda: SimpleNamespace(db_path="unused"),
    )
    dispatch = {
        "status": "running",
        "detail": {"expected_task_ids": ["impl-1", "impl-2"]},
    }

    with patch("herdr.node_dispatch_store.operation_for_node", return_value=dispatch):
        assert not is_node_complete(ports, workflow_id, "implementation")

        tasks.append({
            "task_id": "impl-2",
            "workflow_id": workflow_id,
            "node": "implementation",
            "status": "integrated",
            "integration_mode": "git",
        })
        assert is_node_complete(ports, workflow_id, "implementation")


def test_superseded_current_dispatch_keeps_its_launched_task_inventory():
    workflow_id = "wf-dynamic-stage"
    tasks = [{
        "task_id": "impl-1",
        "workflow_id": workflow_id,
        "node": "implementation",
        "status": "integrated",
        "integration_mode": "git",
        "execution_id": "exec-1",
    }]
    ports = SimpleNamespace(
        load_tasks=lambda: tasks,
        workflow_config_for=lambda _wid: {"nodes": [{"id": "implementation"}]},
        scheduler_core=scheduler,
        _reverification_satisfies_node=lambda _wid, _node: False,
        _get_store=lambda: SimpleNamespace(
            db_path="unused", get_workflow=lambda _wid: {"execution_id": "exec-1"}
        ),
    )
    with (
        patch("herdr.node_dispatch_store.operation_for_node",
              return_value={"status": "superseded", "detail": {}}),
        patch("herdr.node_dispatch_store.latest_expected_task_ids",
              return_value=["impl-1", "impl-2"]),
    ):
        assert not is_node_complete(ports, workflow_id, "implementation")
