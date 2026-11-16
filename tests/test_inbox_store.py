"""Unit tests for InboxStore and InboxTransitions."""

import pytest
from pathlib import Path
from herdr.inbox.store import InboxStore
from herdr.inbox.transitions import (
    InvalidInboxTransitionError,
    validate_inbox_transition,
)


def test_transition_rules():
    # Valid transitions
    validate_inbox_transition("DRAFT", "PENDING")
    validate_inbox_transition("PENDING", "CLAIMED")
    validate_inbox_transition("CLAIMED", "RUNNING")
    validate_inbox_transition("RUNNING", "COMPLETED")
    validate_inbox_transition("RUNNING", "WAITING")
    validate_inbox_transition("WAITING", "RUNNING")
    validate_inbox_transition("FAILED", "PENDING")

    # Invalid transitions
    with pytest.raises(InvalidInboxTransitionError):
        validate_inbox_transition("DRAFT", "CLAIMED")

    with pytest.raises(InvalidInboxTransitionError):
        validate_inbox_transition("DRAFT", "RUNNING")

    with pytest.raises(InvalidInboxTransitionError):
        validate_inbox_transition("COMPLETED", "RUNNING")

    with pytest.raises(InvalidInboxTransitionError):
        validate_inbox_transition("CANCELLED", "PENDING")


def test_inbox_store_crud_and_idempotency(tmp_path: Path):
    db_file = tmp_path / "inbox_test.db"
    store = InboxStore(db_file)

    # 1. Create draft task (no scope)
    t1 = store.create_task(
        title="Fix bug",
        instruction="Fix the bug in module X",
        priority="HIGH",
    )
    assert t1["id"].startswith("inbox_")
    assert t1["status"] == "DRAFT"
    assert t1["priority"] == "HIGH"
    assert t1["priority_weight"] == 3

    # 2. Confirm scope moves to PENDING
    confirmed = store.confirm_task_scope(
        task_id=t1["id"],
        project_id="haflow-123",
        workspace_id="w1K",
    )
    assert confirmed["status"] == "PENDING"
    assert confirmed["project_id"] == "haflow-123"
    assert confirmed["workspace_id"] == "w1K"

    # 3. Create with scope and auto_confirm=True -> directly PENDING
    t2 = store.create_task(
        title="Add feature",
        instruction="Add new API endpoint",
        project_id="haflow-123",
        workspace_id="w1K",
        priority="URGENT",
        auto_confirm=True,
        idempotency_key="key-add-feature-1",
    )
    assert t2["status"] == "PENDING"
    assert t2["priority"] == "URGENT"
    assert t2["priority_weight"] == 4

    # 4. Idempotency test
    t2_dup = store.create_task(
        title="Add feature again",
        instruction="Duplicate submission",
        idempotency_key="key-add-feature-1",
    )
    assert t2_dup["id"] == t2["id"]
    assert t2_dup["title"] == "Add feature"


def test_inbox_queue_position_and_sorting(tmp_path: Path):
    db_file = tmp_path / "inbox_queue.db"
    store = InboxStore(db_file)

    # Insert 3 tasks with different priorities in workspace w1K
    t_normal = store.create_task(
        title="Normal Task", instruction="...", project_id="p1",
        workspace_id="w1K", priority="NORMAL", auto_confirm=True,
    )
    t_low = store.create_task(
        title="Low Task", instruction="...", project_id="p1",
        workspace_id="w1K", priority="LOW", auto_confirm=True,
    )
    t_urgent = store.create_task(
        title="Urgent Task", instruction="...", project_id="p1",
        workspace_id="w1K", priority="URGENT", auto_confirm=True,
    )

    pending_tasks, total = store.list_tasks(status="PENDING", workspace_id="w1K")
    assert total == 3
    # Order should be URGENT (#1), NORMAL (#2), LOW (#3)
    assert pending_tasks[0]["id"] == t_urgent["id"]
    assert pending_tasks[0]["queue_position"] == 1

    assert pending_tasks[1]["id"] == t_normal["id"]
    assert pending_tasks[1]["queue_position"] == 2

    assert pending_tasks[2]["id"] == t_low["id"]
    assert pending_tasks[2]["queue_position"] == 3


def test_inbox_lifecycle_and_state_guards(tmp_path: Path):
    db_file = tmp_path / "inbox_lifecycle.db"
    store = InboxStore(db_file)

    t = store.create_task(
        title="Deploy app", instruction="...", project_id="p1",
        workspace_id="w1K", priority="NORMAL", auto_confirm=True,
    )
    tid = t["id"]
    assert t["status"] == "PENDING"

    # PENDING -> CLAIMED
    claimed = store.update_task_status(tid, "CLAIMED", worker_id="worker-01")
    assert claimed["status"] == "CLAIMED"
    assert claimed["worker_id"] == "worker-01"

    # CLAIMED -> RUNNING
    running = store.update_task_status(tid, "RUNNING", workflow_id="wf-101")
    assert running["status"] == "RUNNING"
    assert running["workflow_id"] == "wf-101"
    assert running["started_at"] is not None

    # RUNNING cannot jump to PENDING
    with pytest.raises(InvalidInboxTransitionError):
        store.update_task_status(tid, "PENDING")

    # RUNNING -> COMPLETED
    completed = store.update_task_status(tid, "COMPLETED")
    assert completed["status"] == "COMPLETED"
    assert completed["completed_at"] is not None

    # Cannot transition from COMPLETED
    with pytest.raises(InvalidInboxTransitionError):
        store.update_task_status(tid, "RUNNING")


def test_inbox_priority_and_cancel(tmp_path: Path):
    db_file = tmp_path / "inbox_ops.db"
    store = InboxStore(db_file)

    t = store.create_task(
        title="Some task", instruction="...", project_id="p1",
        workspace_id="w1K", priority="LOW", auto_confirm=True,
    )
    tid = t["id"]

    # Change priority to HIGH
    updated = store.set_task_priority(tid, "HIGH")
    assert updated["priority"] == "HIGH"
    assert updated["priority_weight"] == 3

    # Cancel task
    cancelled = store.cancel_task(tid, reason="No longer needed")
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["error"] == "No longer needed"

    # Stats
    stats = store.get_inbox_stats()
    assert stats["counts"]["CANCELLED"] == 1
    assert stats["total"] == 1


def test_reorder_dense_normalization_four_tasks(tmp_path: Path):
    """P2-02: Verify that moving C up in [A, B, C, D] results in strictly [A, C, B, D]."""
    db_file = tmp_path / "inbox_reorder.db"
    store = InboxStore(db_file)

    ta = store.create_task(title="A", instruction=".", project_id="p1", workspace_id="w1", auto_confirm=True)
    tb = store.create_task(title="B", instruction=".", project_id="p1", workspace_id="w1", auto_confirm=True)
    tc = store.create_task(title="C", instruction=".", project_id="p1", workspace_id="w1", auto_confirm=True)
    td = store.create_task(title="D", instruction=".", project_id="p1", workspace_id="w1", auto_confirm=True)

    # Initial order: A, B, C, D
    tasks, _ = store.list_tasks(workspace_id="w1", status="PENDING")
    assert [t["title"] for t in tasks] == ["A", "B", "C", "D"]

    # Move C up
    store.reorder_pending_task(tc["id"], direction="up")
    tasks_after, _ = store.list_tasks(workspace_id="w1", status="PENDING")
    assert [t["title"] for t in tasks_after] == ["A", "C", "B", "D"]

    # Move B down
    store.reorder_pending_task(tb["id"], direction="down")
    tasks_after2, _ = store.list_tasks(workspace_id="w1", status="PENDING")
    assert [t["title"] for t in tasks_after2] == ["A", "C", "D", "B"]
