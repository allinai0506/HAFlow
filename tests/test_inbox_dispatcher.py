"""Unit tests for InboxDispatcher: single-task execution, priority dispatching, and auto-continuation."""

import pytest
import time
from pathlib import Path
from unittest.mock import patch

from herdr.inbox.dispatcher import InboxDispatcher
from herdr.inbox.store import InboxStore
from herdr.state.workflow_task_store import save_workflow


def test_workspace_busy_and_single_task_lock(tmp_path: Path):
    db_file = tmp_path / "dispatcher_busy.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    # Initially workspace w1K is free
    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        is_busy, _ = dispatcher.is_workspace_busy("w1K")
        assert is_busy is False

        # Create two tasks on w1K
        t1 = store.create_task(
            title="Task 1", instruction="Do 1", project_id="p1",
            workspace_id="w1K", priority="NORMAL", auto_confirm=True,
        )
        t2 = store.create_task(
            title="Task 2", instruction="Do 2", project_id="p1",
            workspace_id="w1K", priority="NORMAL", auto_confirm=True,
        )

        # Claim Task 1
        claimed1 = dispatcher.claim_next_task("w1K")
        assert claimed1 is not None
        assert claimed1["id"] == t1["id"]
        assert claimed1["status"] == "CLAIMED"

        # Now w1K is busy
        is_busy, reason = dispatcher.is_workspace_busy("w1K")
        assert is_busy is True
        assert "is CLAIMED" in reason

        # Trying to claim another task on w1K must return None
        claimed2 = dispatcher.claim_next_task("w1K")
        assert claimed2 is None


def test_independent_workspaces_can_run_in_parallel(tmp_path: Path):
    db_file = tmp_path / "dispatcher_parallel.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        # Task on w1K and Task on w2H
        t_w1 = store.create_task(
            title="Task W1", instruction="Do W1", project_id="p1",
            workspace_id="w1K", priority="NORMAL", auto_confirm=True,
        )
        t_w2 = store.create_task(
            title="Task W2", instruction="Do W2", project_id="p2",
            workspace_id="w2H", priority="NORMAL", auto_confirm=True,
        )

        # Claim w1
        claimed_w1 = dispatcher.claim_next_task("w1K")
        assert claimed_w1 is not None
        assert claimed_w1["id"] == t_w1["id"]

        # w2H is NOT blocked by w1K!
        claimed_w2 = dispatcher.claim_next_task("w2H")
        assert claimed_w2 is not None
        assert claimed_w2["id"] == t_w2["id"]


def test_priority_scheduling_order(tmp_path: Path):
    db_file = tmp_path / "dispatcher_priority.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        # Submit Low, Normal, Urgent on same workspace w1K
        t_low = store.create_task(
            title="Low task", instruction="...", project_id="p1",
            workspace_id="w1K", priority="LOW", auto_confirm=True,
        )
        t_norm = store.create_task(
            title="Normal task", instruction="...", project_id="p1",
            workspace_id="w1K", priority="NORMAL", auto_confirm=True,
        )
        t_urg = store.create_task(
            title="Urgent task", instruction="...", project_id="p1",
            workspace_id="w1K", priority="URGENT", auto_confirm=True,
        )

        # Claim should pick URGENT first!
        claimed = dispatcher.claim_next_task("w1K")
        assert claimed["id"] == t_urg["id"]
        assert claimed["priority"] == "URGENT"


def test_auto_continuation_pipeline(tmp_path: Path):
    """Core acceptance test:

    Task A runs. Task B (NORMAL) and Task C (HIGH) are pending.
    When A finishes, C automatically starts.
    When C finishes, B automatically starts.
    Entire pipeline executes continuously with zero human intervention.
    """
    db_file = tmp_path / "dispatcher_pipeline.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        # Submit Task A, B, C
        task_a = store.create_task(
            title="Task A", instruction="Req A", project_id="p1",
            workspace_id="w1K", priority="NORMAL", auto_confirm=True,
        )
        task_b = store.create_task(
            title="Task B", instruction="Req B", project_id="p1",
            workspace_id="w1K", priority="NORMAL", auto_confirm=True,
        )
        task_c = store.create_task(
            title="Task C", instruction="Req C", project_id="p1",
            workspace_id="w1K", priority="HIGH", auto_confirm=True,
        )

        # Mock runner function that generates a workflow for each task
        launched_wfs = {}

        def mock_runner(t):
            wf_id = f"wf_{t['id']}"
            save_workflow(
                {"workflow_id": wf_id, "title": t["title"], "status": "running"},
                db_path=db_file,
            )
            launched_wfs[t["id"]] = wf_id
            return wf_id

        # 1. First sweep: should launch Task A (FIFO between A & B, but wait, C has HIGH!)
        # Actually C has HIGH priority! So C should be launched first!
        sweep1 = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep1["launched"]) == 1
        first_launched = sweep1["launched"][0]
        assert first_launched["id"] == task_c["id"]  # High priority wins!
        assert first_launched["status"] == "RUNNING"
        assert first_launched["workflow_id"] == f"wf_{task_c['id']}"

        # 2. Second sweep while Task C is still running:
        # Workspace is busy, nothing new launched.
        sweep2 = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep2["launched"]) == 0
        assert len(sweep2["busy_workspaces"]) == 1

        # 3. Simulate Task C's workflow completing in engine
        save_workflow(
            {"workflow_id": f"wf_{task_c['id']}", "title": "Task C", "status": "completed"},
            db_path=db_file,
        )

        # 4. Third sweep: should reconcile Task C to COMPLETED, free w1K,
        # and AUTOMATICALLY launch Task A (Task A was submitted before B)!
        sweep3 = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep3["reconciled"]) == 1
        assert sweep3["reconciled"][0]["id"] == task_c["id"]
        assert sweep3["reconciled"][0]["status"] == "COMPLETED"

        assert len(sweep3["launched"]) == 1
        assert sweep3["launched"][0]["id"] == task_a["id"]
        assert sweep3["launched"][0]["status"] == "RUNNING"

        # 5. Complete Task A's workflow
        save_workflow(
            {"workflow_id": f"wf_{task_a['id']}", "title": "Task A", "status": "completed"},
            db_path=db_file,
        )

        # 6. Fourth sweep: reconciles A, automatically launches Task B!
        sweep4 = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep4["reconciled"]) == 1
        assert sweep4["reconciled"][0]["id"] == task_a["id"]
        assert sweep4["reconciled"][0]["status"] == "COMPLETED"

        assert len(sweep4["launched"]) == 1
        assert sweep4["launched"][0]["id"] == task_b["id"]
        assert sweep4["launched"][0]["status"] == "RUNNING"

        # 7. Complete Task B's workflow
        save_workflow(
            {"workflow_id": f"wf_{task_b['id']}", "title": "Task B", "status": "completed"},
            db_path=db_file,
        )

        # 8. Fifth sweep: reconciles B, queue is now empty, workspace free!
        sweep5 = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep5["reconciled"]) == 1
        assert sweep5["reconciled"][0]["id"] == task_b["id"]
        assert sweep5["reconciled"][0]["status"] == "COMPLETED"
        assert len(sweep5["launched"]) == 0
        assert len(sweep5["busy_workspaces"]) == 0

        # Verify all three tasks are now COMPLETED
        final_tasks, total = store.list_tasks(status="COMPLETED")
        assert total == 3


def test_failed_and_cancelled_workflows_release_workspace(tmp_path: Path):
    """P1-03: Workflows in failed, cancelled, or abandoned statuses do NOT block the workspace."""
    db_file = tmp_path / "dispatcher_terminal.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        # 1. Failed workflow in database
        save_workflow(
            {"workflow_id": "wf_failed_1", "title": "Failed Run", "status": "failed", "config": {"workspace_id": "w_test"}},
            db_path=db_file,
        )
        is_busy, reason = dispatcher.is_workspace_busy("w_test")
        assert is_busy is False, f"Failed workflow must not block workspace, but got reason: {reason}"

        # 2. Cancelled workflow in database
        save_workflow(
            {"workflow_id": "wf_cancel_1", "title": "Cancelled Run", "status": "cancelled", "config": {"workspace_id": "w_test"}},
            db_path=db_file,
        )
        is_busy, reason = dispatcher.is_workspace_busy("w_test")
        assert is_busy is False, f"Cancelled workflow must not block workspace, but got reason: {reason}"

        # 3. Abandoned workflow in database
        save_workflow(
            {"workflow_id": "wf_aban_1", "title": "Abandoned Run", "status": "abandoned", "config": {"workspace_id": "w_test"}},
            db_path=db_file,
        )
        is_busy, reason = dispatcher.is_workspace_busy("w_test")
        assert is_busy is False, f"Abandoned workflow must not block workspace, but got reason: {reason}"

        # 4. Running workflow DOES block workspace
        save_workflow(
            {"workflow_id": "wf_run_1", "title": "Active Run", "status": "running", "config": {"workspace_id": "w_test"}},
            db_path=db_file,
        )
        is_busy, reason = dispatcher.is_workspace_busy("w_test")
        assert is_busy is True
        assert "Active Run" in reason


def test_reconcile_waiting_tasks(tmp_path: Path):
    """P2-01: Verify WAITING tasks participate in reconciliation and transition cleanly."""
    db_file = tmp_path / "dispatcher_waiting.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    # 1. Create a task that enters WAITING
    t = store.create_task(
        title="Waiting Task", instruction="Needs human signoff", project_id="p1",
        workspace_id="w1", auto_confirm=True,
    )
    store.update_task_status(t["id"], "CLAIMED")
    store.update_task_status(t["id"], "RUNNING", workflow_id="wf_human_1")
    store.update_task_status(t["id"], "WAITING")

    # Workflow is paused -> remains WAITING
    save_workflow({"workflow_id": "wf_human_1", "title": "Human WF", "status": "paused"}, db_path=db_file)
    reconciled = dispatcher.reconcile_running_tasks()
    assert len(reconciled) == 0
    assert store.get_task(t["id"])["status"] == "WAITING"

    # Human provides signoff, workflow resumes to 'running' -> inbox task reconciles back to RUNNING!
    save_workflow({"workflow_id": "wf_human_1", "title": "Human WF", "status": "running"}, db_path=db_file)
    reconciled = dispatcher.reconcile_running_tasks()
    assert len(reconciled) == 1
    assert reconciled[0]["id"] == t["id"]
    assert reconciled[0]["status"] == "RUNNING"
    assert store.get_task(t["id"])["status"] == "RUNNING"

    # Workflow eventually completes -> inbox task reconciles to COMPLETED!
    save_workflow({"workflow_id": "wf_human_1", "title": "Human WF", "status": "completed"}, db_path=db_file)
    reconciled2 = dispatcher.reconcile_running_tasks()
    assert len(reconciled2) == 1
    assert reconciled2[0]["id"] == t["id"]
    assert reconciled2[0]["status"] == "COMPLETED"
    assert store.get_task(t["id"])["status"] == "COMPLETED"
