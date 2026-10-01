"""Deliver an exhausted-loop arbitration event only while it is blocked."""
import importlib
from queue import Queue
import subprocess
from unittest.mock import Mock

import pytest
from herdr.state_store import SQLiteStateStore


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_CONTROLLER_TEST", "1")
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = {"task_id": "task-blocker", "workflow_id": "wf-blocker",
            "node": "implementation", "stage": "implementation",
            "status": "working", "agent": "opencode", "pane_id": "pane-task"}
    store.save_task(task)
    monkeypatch.setattr(controller, "_get_store", lambda: store)
    monkeypatch.setattr(controller, "coordinator_queue", Queue())
    monkeypatch.setattr(controller, "queued_events", set())
    monkeypatch.setattr(controller, "coordinator_status", lambda _: "idle")
    monkeypatch.setattr(controller, "coordinator_pane_for_workflow", lambda _: "pane-coordinator")
    monkeypatch.setattr(controller, "attention_clear", lambda _: None)
    transport = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(controller.subprocess, "run", transport)
    task = store.get_task(task["task_id"])
    store.record_event("blocked_marker_observed", {
        "observed_status": task["status"], "observed_version": task["version"],
    }, workflow_id=task["workflow_id"], task_id=task["task_id"])
    assert controller.process_blocked_observations() == 1
    assert store.get_task(task["task_id"])["status"] == "blocked"
    item = controller.coordinator_queue.get_nowait()
    assert item["event_type"] == "inner_loop_exhausted"
    return controller, store, item, transport


def test_exhausted_loop_requests_arbitration_through_real_queue(scene):
    controller, store, item, transport = scene
    controller._handle_coordinator_item(item)
    transport.assert_called_once()
    argv = transport.call_args.args[0]
    assert argv[:4] == ["herdr", "agent", "prompt", "pane-coordinator"]
    assert "HERDR_CONTROLLER_BLOCKER_EVENT" in argv[4]
    assert "task_id: task-blocker" in argv[4]
    assert store.get_task("task-blocker")["status"] == "blocked"
    assert item["key"] not in controller.queued_events


def test_resolved_blocker_drops_its_old_arbitration_event(scene):
    controller, store, item, transport = scene
    store.transition_task("task-blocker", "working", "explicit recovery")
    controller._handle_coordinator_item(item)
    transport.assert_not_called()
    assert store.get_task("task-blocker")["status"] == "working"
