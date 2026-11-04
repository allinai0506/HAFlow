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


def test_implementation_inventory_survives_candidate_advance_from_input_to_output_sha():
    workflow_id = "wf-candidate-advance"
    tasks = [{
        "task_id": "impl-1",
        "workflow_id": workflow_id,
        "node": "implementation",
        "status": "integrated",
        "integration_mode": "git",
        "execution_id": "exec-1",
        "candidate_sha": "a" * 40,
        "commit": "b" * 40,
        "integrated_commit": "b" * 40,
    }, {
        "task_id": "impl-2",
        "workflow_id": workflow_id,
        "node": "implementation",
        "status": "superseded",
        "execution_id": "exec-1",
        "candidate_sha": "a" * 40,
    }]
    ports = SimpleNamespace(
        load_tasks=lambda: tasks,
        workflow_config_for=lambda _wid: {"nodes": [{"id": "implementation"}]},
        scheduler_core=scheduler,
        _reverification_satisfies_node=lambda _wid, _node: False,
        _get_store=lambda: SimpleNamespace(
            db_path="unused",
            get_workflow=lambda _wid: {"execution_id": "exec-1", "candidate_sha": "b" * 40},
        ),
    )
    dispatch = {"status": "superseded", "detail": {}}

    with (
        patch("herdr.node_dispatch_store.operation_for_node", return_value=dispatch),
        patch("herdr.node_dispatch_store.latest_expected_task_ids",
              return_value=["impl-1", "impl-2"]),
    ):
        assert not is_node_complete(ports, workflow_id, "implementation")


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


def test_bound_controller_declares_store_for_dynamic_inventory():
    from services.controller.bindings import install_modules
    from services.controller.stage_state import DEPENDENCIES

    workflow_id = "wf-bound-dynamic-stage"
    tasks = [{
        "task_id": "impl-1",
        "workflow_id": workflow_id,
        "node": "implementation",
        "status": "integrated",
        "integration_mode": "git",
    }]
    store = SimpleNamespace(db_path="unused")
    namespace = {
        "__name__": "test_bound_controller",
        "_get_store": lambda: store,
        "_reverification_satisfies_node": lambda _wid, _node: False,
        "load_tasks": lambda: tasks,
        "scheduler_core": scheduler,
        "workflow_config_for": lambda _wid: {"nodes": [{"id": "implementation"}]},
    }
    for name in {dep for dependencies in DEPENDENCIES.values() for dep in dependencies}:
        namespace.setdefault(name, lambda *args, **kwargs: None)
    install_modules(namespace, ("stage_state",))
    dispatch = {"status": "running", "detail": {"expected_task_ids": ["impl-1", "impl-2"]}}

    with patch("herdr.node_dispatch_store.operation_for_node", return_value=dispatch) as reader:
        assert namespace["is_node_complete"](workflow_id, "implementation") is False

    reader.assert_called_once_with("unused", workflow_id, "implementation")


def test_current_candidate_reuse_can_satisfy_node_with_old_dispatch_history():
    workflow_id = "wf-reused-verifier"
    tasks = [{
        "task_id": "test-old",
        "workflow_id": workflow_id,
        "node": "test",
        "status": "superseded",
        "execution_id": "exec-1",
    }]
    reused = []
    ports = SimpleNamespace(
        load_tasks=lambda: tasks,
        workflow_config_for=lambda _wid: {"nodes": [{"id": "test"}]},
        scheduler_core=scheduler,
        _reverification_satisfies_node=lambda _wid, _node: reused.append(True) or True,
        _get_store=lambda: SimpleNamespace(db_path="unused"),
    )
    dispatch = {"status": "superseded", "detail": {"expected_task_ids": ["test-old"]}}

    with patch("herdr.node_dispatch_store.operation_for_node", return_value=dispatch):
        assert is_node_complete(ports, workflow_id, "test")

    assert reused == [True]
