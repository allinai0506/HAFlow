"""Regression tests for wf-haflow-1029-02 Fix 2:
1. task_listener recovers a task in `dispatched` status to `agent_done` when gate_verdict_ready is True.
2. sentinel check_dispatch_fuse does not kill a task whose gate verdict is already ready.
"""

from unittest.mock import MagicMock
import pytest

from services.controller import task_listener


def test_task_listener_recovers_dispatched_task_with_gate_verdict():
    mock_ports = MagicMock()
    task = {
        "task_id": "test-gate-task",
        "status": "dispatched",
        "node": "test",
        "stage": "test",
        "completion_protocol": "receipt-v1",
    }
    mock_ports.get_task.return_value = task
    mock_ports._completion_marker_snapshot.return_value = (False, "gate verdict on screen")
    mock_ports.gate_verdict_ready.return_value = True
    mock_ports._set_observed_status.return_value = True

    # Agent is idle, no completion marker, but gate verdict is ready
    task_listener.handle_event(mock_ports, "test-gate-task", "idle")

    # It must set observed status to agent_done with gate_verdict_recovery
    mock_ports._set_observed_status.assert_called_once_with(task, "agent_done", "gate_verdict_recovery")
    mock_ports.emit_done_if_allowed.assert_called_once_with(task, report_text="gate verdict on screen")


def test_sentinel_dispatch_fuse_spares_task_with_gate_verdict(monkeypatch):
    import importlib
    sentinel = importlib.import_module("services.herdr-sentinel")

    task = {
        "task_id": "test-fuse-gate",
        "status": "dispatched",
        "updated_at": 1000.0,
        "node": "review",
        "gate": True,
        "pane_id": "w1:p1",
    }

    # Enable fuse and mock breaches
    monkeypatch.setattr(sentinel, "dispatch_fuse_enabled", lambda: True)
    breach = {
        "task_id": "test-fuse-gate",
        "pane_id": "w1:p1",
        "status": "dispatched",
        "waited_seconds": 1800,
        "requeues": 0,
    }
    monkeypatch.setattr(sentinel.liveness, "evaluate_dispatch_fuse", lambda tasks, ep, now: ([breach], {task["task_id"]: {"episode": 1000.0}}))
    monkeypatch.setattr(sentinel, "_pane_delivery_evidence", lambda pane_id, tid: {"has_marker": False, "agent_status": "idle"})
    mock_update = MagicMock(return_value=True)
    monkeypatch.setattr(sentinel, "update_statuses", mock_update)
    monkeypatch.setattr(sentinel, "notify_dispatch_fuse", lambda b: None)
    monkeypatch.setattr(sentinel, "_task_has_valid_gate_verdict", lambda t: (True, "pass", "all passed", 1050.0))

    state = {}
    changed = sentinel.check_dispatch_fuse([task], state)

    # Must NOT fail the task!
    assert changed is False
    mock_update.assert_not_called()
    assert breach["action"] == "attention"
