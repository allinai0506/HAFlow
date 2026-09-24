"""Regression coverage for the test-r4 blocked findings."""

from __future__ import annotations

from herdr.state_store import SQLiteStateStore


def test_completion_requires_two_marker_samples_one_sentinel_cycle_apart(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-fix5", "status": "running"})
    store.save_task({
        "task_id": "task-fix5-completion",
        "workflow_id": "wf-fix5",
        "node": "implementation",
        "status": "working",
        "started_at": 1.0,
    })

    store.observe_completion(
        "task-fix5-completion", marker_present=False,
        agent_status="working", observed_at=100.0,
    )
    first = store.observe_completion(
        "task-fix5-completion", marker_present=True,
        agent_status="idle", observed_at=103.0,
    )
    too_soon = store.observe_completion(
        "task-fix5-completion", marker_present=True,
        agent_status="idle", observed_at=104.5,
    )

    assert first["consecutive_samples"] == 1
    assert first["ready"] is False
    assert too_soon["consecutive_samples"] == 1
    assert too_soon["ready"] is False

    confirmed = store.observe_completion(
        "task-fix5-completion", marker_present=True,
        agent_status="idle", observed_at=106.0,
    )
    assert confirmed["consecutive_samples"] == 2
    assert confirmed["ready"] is True
