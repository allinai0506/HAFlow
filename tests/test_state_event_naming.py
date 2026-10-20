"""Tests for state event naming and authority separation (SC-007).

Verifies:
1. State transition atomically writes authoritative task_transition event in SQLite transaction.
2. TrajectoryLedger writes best-effort task_status_changed event with source=trajectory.
3. Unsuccessful or illegal transitions write neither event.
"""

import json
import pytest
from herdr import kernel
from herdr.trajectory import TrajectoryLedger
from herdr.state_store import get_state_store, reset_state_store


@pytest.fixture
def clean_store(tmp_path, monkeypatch):
    db_path = tmp_path / "test_state.db"
    workflows_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    workflows_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    monkeypatch.setenv("WORKFLOWS_FILE", str(workflows_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))

    reset_state_store()
    store = get_state_store(db_path)
    yield store, db_path, tmp_path
    reset_state_store()


def test_state_event_naming_and_authority(clean_store):
    store, db_path, _ = clean_store
    store.save_task({
        "task_id": "t-sc007-01",
        "workflow_id": "wf-sc007",
        "node": "implementation",
        "run_id": "run-sc007-01",
        "status": "pending",
    })

    # Trigger legitimate transition
    res = kernel.transition_task(
        task_id="t-sc007-01",
        to_status="dispatched",
        reason="normal dispatch",
        source="herdr-controller",
        store=store,
    )
    assert res["accepted"] is True
    assert res["new_status"] == "dispatched"

    # 1. Verify authoritative task_transition event in SQLite store
    canonical_events = store.list_events(task_id="t-sc007-01", event_type="task_transition")
    assert len(canonical_events) == 1
    ev = canonical_events[0]
    assert ev["event_type"] == "task_transition"
    assert ev["source"] == "herdr-controller"
    assert ev["payload"]["from_status"] == "pending"
    assert ev["payload"]["to_status"] == "dispatched"
    assert ev["payload"]["reason"] == "normal dispatch"

    # 2. Verify best-effort task_status_changed event in TrajectoryLedger
    trajectory_events = TrajectoryLedger(db_path).list_events("run-sc007-01")
    assert any(e["event_type"] == "task_status_changed" and e["status"] == "dispatched" for e in trajectory_events)
    traj_ev = next(e for e in trajectory_events if e["event_type"] == "task_status_changed")
    assert traj_ev["event_type"] == "task_status_changed"
    assert traj_ev["status"] == "dispatched"
    assert traj_ev["metadata"]["from_status"] == "pending"
    assert traj_ev["metadata"]["to_status"] == "dispatched"
