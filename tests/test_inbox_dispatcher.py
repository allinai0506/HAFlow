"""Unit tests for InboxDispatcher: single-task execution, priority dispatching, and auto-continuation."""

import pytest
import sqlite3
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


def test_reconcile_workflow_startup_failed_outcome_marks_failed(tmp_path: Path):
    """P2: If workflow status is 'completed' but outcome is 'startup_failed', inbox task must be marked FAILED."""
    db_file = tmp_path / "dispatcher_outcome.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    t = store.create_task(
        title="Failing Task", instruction="...", project_id="p1", workspace_id="w1", auto_confirm=True
    )
    store.update_task_status(t["id"], "CLAIMED")
    store.update_task_status(t["id"], "RUNNING", workflow_id="wf_bad_1")

    # Workflow lifecycle is completed, but outcome is startup_failed!
    save_workflow(
        {"workflow_id": "wf_bad_1", "title": "Bad Startup", "status": "completed", "outcome": "startup_failed"},
        db_path=db_file,
    )
    reconciled = dispatcher.reconcile_running_tasks()
    assert len(reconciled) == 1
    assert reconciled[0]["id"] == t["id"]
    assert reconciled[0]["status"] == "FAILED"
    assert "startup_failed" in reconciled[0]["error"]
    assert store.get_task(t["id"])["status"] == "FAILED"


def test_launcher_fencing_token_mismatch_halts_orphan_workflow(tmp_path: Path):
    """P1: When launcher fencing token mismatch occurs, it halts the orphan workflow and refuses to mark RUNNING."""
    db_file = tmp_path / "dispatcher_fencing.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        t = store.create_task(
            title="Stolen Task", instruction="...", project_id="p1", workspace_id="w1", auto_confirm=True
        )
        claimed = dispatcher.claim_next_task("w1")
        assert claimed is not None
        assert claimed["status"] == "CLAIMED"
        original_token = claimed["worker_id"]

        def runner_with_stolen_token(task_dict):
            # Simulate another worker or admin taking over the task while this runner is executing
            conn = sqlite3.connect(str(db_file))
            conn.execute(
                "UPDATE user_inbox_tasks SET worker_id = 'different_worker_fencing_token' WHERE id = ?",
                (task_dict["id"],),
            )
            conn.commit()
            conn.close()
            # Register a dummy workflow with full identity
            save_workflow(
                {
                    "workflow_id": "wf_orphan_99",
                    "title": "Orphan WF",
                    "status": "running",
                    "project_id": "p1",
                    "workspace_id": "w1",
                    "config": {"key": "value"},
                },
                db_path=db_file,
            )
            return "wf_orphan_99"

        # Execute launch with stolen token
        res = dispatcher.launch_task(claimed, runner_fn=runner_with_stolen_token, async_launch=False)
        assert res is None  # Launcher must reject and return None

        # Verify task is NOT RUNNING with the old workflow
        task_in_db = store.get_task(t["id"])
        assert task_in_db["workflow_id"] != "wf_orphan_99"
        assert task_in_db["status"] == "CLAIMED"
        assert task_in_db["worker_id"] == "different_worker_fencing_token"

        # Verify orphan workflow was safely halted without erasing identity (P1)
        from herdr.state_db import get_workflow
        wf = get_workflow("wf_orphan_99", db_path=db_file)
        assert wf["status"] in ("completed", "cancelled")
        assert wf.get("outcome") == "abandoned"
        assert wf.get("title") == "Orphan WF"
        assert wf.get("project_id") == "p1"
        assert wf.get("workspace_id") == "w1"
        assert wf.get("config") == {"key": "value"}


def test_launcher_heartbeat_keeps_lease_alive_during_long_launch(tmp_path: Path):
    """P1: Verify that a long-running launcher heartbeat extends lease and prevents stale recovery."""
    db_file = tmp_path / "dispatcher_long_launch.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        t = store.create_task(
            title="Long Launch Task", instruction="...", project_id="p1", workspace_id="w1", auto_confirm=True
        )
        claimed = dispatcher.claim_next_task("w1", lease_duration_seconds=180.0)
        assert claimed is not None
        original_lease = claimed["lease_expires_at"]

        # 1. Simulate active launcher heartbeat extending lease
        conn = sqlite3.connect(str(db_file))
        now = time.time()
        # Simulate that 200 seconds elapsed from initial claim, but heartbeat kept it fresh
        future_now = now + 200.0
        conn.execute(
            """
            UPDATE user_inbox_tasks
               SET heartbeat_at = ?, lease_expires_at = ?
             WHERE id = ? AND worker_id = ?
            """,
            (future_now, future_now + 180.0, t["id"], claimed["worker_id"]),
        )
        conn.commit()
        conn.close()

        # 2. Run stale task recovery at future_now + 50s (250s after initial claim)
        # Since heartbeat extended lease to future_now + 180s (380s total), task is NOT stale!
        with patch("time.time", return_value=future_now + 50.0):
            recovered = dispatcher.recover_stale_tasks()
            assert len(recovered) == 0  # Not recovered!
            assert store.get_task(t["id"])["status"] == "CLAIMED"

        # 3. If worker actually dies (no further heartbeat) and clock passes extended lease
        with patch("time.time", return_value=future_now + 200.0):
            recovered2 = dispatcher.recover_stale_tasks()
            assert len(recovered2) == 1
            assert recovered2[0]["id"] == t["id"]
            # Returned to PENDING queue because worker died
            assert store.get_task(t["id"])["status"] == "PENDING"


def test_crash_recovery_preserves_running_workflow_without_duplicate(tmp_path: Path):
    """P1-02: If process crashes after launching workflow, recover_stale_tasks connects to existing workflow."""
    db_file = tmp_path / "dispatcher_crash_recovery.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        t = store.create_task(
            title="Crash Simulation Task", instruction="...", project_id="p1", workspace_id="w1", auto_confirm=True
        )
        claimed = dispatcher.claim_next_task("w1", lease_duration_seconds=10.0)
        assert claimed is not None

        # Pre-assigned workflow_id in DB
        task_before_crash = store.get_task(t["id"])
        pre_wid = f"wf_pre_{t['id'][:6]}"

        conn = sqlite3.connect(str(db_file))
        conn.execute("UPDATE user_inbox_tasks SET workflow_id = ? WHERE id = ?", (pre_wid, t["id"]))
        conn.commit()
        conn.close()

        # The workflow was indeed registered in SQLite by launcher before crash
        save_workflow(
            {"workflow_id": pre_wid, "title": "Crashed Worker WF", "status": "running"},
            db_path=db_file,
        )

        # Worker process crashed, lease expires
        future_time = time.time() + 20.0
        with patch("time.time", return_value=future_time):
            recovered = dispatcher.recover_stale_tasks()
            assert len(recovered) == 1
            rec_task = store.get_task(t["id"])
            # MUST NOT be reset to PENDING! Must be recovered to RUNNING with existing workflow!
            assert rec_task["status"] == "RUNNING"
            assert rec_task["workflow_id"] == pre_wid


def test_waiting_task_holds_workspace_mutex_and_blocks_subsequent_dispatch(tmp_path: Path):
    """P1-02: When a task enters WAITING (e.g. human approval), it holds the workspace mutex.
    Subsequent tasks in the same workspace wait in queue; tasks in other workspaces execute in parallel.
    When the WAITING task completes, the workspace is released and the queued task executes.
    """
    db_file = tmp_path / "dispatcher_waiting_mutex.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        t1 = store.create_task(
            title="Task 1 Human Gate", instruction="...", project_id="p1", workspace_id="w1", auto_confirm=True
        )
        t2 = store.create_task(
            title="Task 2 Queued in w1", instruction="...", project_id="p1", workspace_id="w1", auto_confirm=True
        )
        t3 = store.create_task(
            title="Task 3 Independent in w2", instruction="...", project_id="p2", workspace_id="w2", auto_confirm=True
        )

        # Claim and run Task 1 in w1
        c1 = dispatcher.claim_next_task("w1")
        assert c1["id"] == t1["id"]
        store.update_task_status(t1["id"], "RUNNING", workflow_id="wf_gate_1")
        save_workflow({"workflow_id": "wf_gate_1", "status": "paused"}, db_path=db_file)

        # Reconcile Task 1 -> enters WAITING
        dispatcher.reconcile_running_tasks()
        assert store.get_task(t1["id"])["status"] == "WAITING"

        # Workspace w1 is BUSY holding the paused Task 1!
        is_busy_w1, reason_w1 = dispatcher.is_workspace_busy("w1")
        assert is_busy_w1 is True
        assert "WAITING" in reason_w1

        # Task 2 in w1 cannot be claimed yet; must wait in queue
        c2 = dispatcher.claim_next_task("w1")
        assert c2 is None
        assert store.get_task(t2["id"])["status"] == "PENDING"

        # Independent workspace w2 is FREE and can run Task 3 concurrently
        is_busy_w2, _ = dispatcher.is_workspace_busy("w2")
        assert is_busy_w2 is False
        c3 = dispatcher.claim_next_task("w2")
        assert c3 is not None
        assert c3["id"] == t3["id"]

        # Human signs off on Task 1 -> workflow completes
        save_workflow({"workflow_id": "wf_gate_1", "status": "completed", "outcome": "success"}, db_path=db_file)
        dispatcher.reconcile_running_tasks()
        assert store.get_task(t1["id"])["status"] == "COMPLETED"

        # Workspace w1 is now FREE!
        is_busy_w1_after, _ = dispatcher.is_workspace_busy("w1")
        assert is_busy_w1_after is False

        # Task 2 can now be claimed and executed!
        c2_after = dispatcher.claim_next_task("w1")
        assert c2_after is not None
        assert c2_after["id"] == t2["id"]


def test_lost_workflow_handled_after_grace_period(tmp_path: Path):
    """P2-05: Missing workflow in RUNNING state fails with informative error after 60s grace period."""
    db_file = tmp_path / "dispatcher_lost_workflow.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        t = store.create_task(title="Lost WF Task", instruction=".", project_id="p1", workspace_id="w1", auto_confirm=True)
        store.update_task_status(t["id"], "CLAIMED")
        store.update_task_status(t["id"], "RUNNING", workflow_id="wf_ghost_404")

        # Initial check at current time: within 60s grace period, task remains RUNNING
        now = time.time()
        with patch("time.time", return_value=now + 10.0):
            reconciled = dispatcher.reconcile_running_tasks()
            assert len(reconciled) == 0
            assert store.get_task(t["id"])["status"] == "RUNNING"

        # Check after 70s (> 60s): grace period expired, marked FAILED and frees workspace
        with patch("time.time", return_value=now + 75.0):
            reconciled = dispatcher.reconcile_running_tasks()
            assert len(reconciled) == 1
            assert reconciled[0]["id"] == t["id"]
            assert reconciled[0]["status"] == "FAILED"
            assert "lost workflow" in reconciled[0]["error"]
            assert store.get_task(t["id"])["status"] == "FAILED"


def test_default_git_template_retry_does_not_pollute_context_binding(tmp_path: Path):
    """P1-01: Retrying a task with default Git template software-development-v1 must not pass context."""
    db_file = tmp_path / "dispatcher_retry_git.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    p_root = tmp_path / "proj_root"
    p_root.mkdir()
    mock_projects = {
        "projects": {
            str(p_root): {
                "project_id": "proj-git-1",
                "project_name": "Proj Git",
                "project_root": str(p_root),
                "workspace_id": "ws-git-1",
            }
        }
    }

    t = store.create_task(
        title="Git Task",
        instruction="Do work",
        project_id="proj-git-1",
        workspace_id="ws-git-1",
        template_name="software-development-v1",
        auto_confirm=True,
    )
    store.update_task_status(t["id"], "CLAIMED")
    store.update_task_status(t["id"], "RUNNING", workflow_id="wf_first_fail")
    store.update_task_status(t["id"], "FAILED", error="Crash on attempt 1")

    # Retry task
    retried = store.retry_task(t["id"])
    assert retried["status"] == "PENDING"
    assert len(retried.get("execution_history", [])) == 1
    assert "execution_history" not in retried.get("context", {})

    # Launch task with Dispatcher default runner calling real workflow_api.run_workflow
    captured_cmd = []
    class MockProcess:
        returncode = 0
        stdout = "WORKFLOW_ID=wf_second_success\n"
        stderr = ""

    def mock_common_run(cmd, timeout=600):
        captured_cmd.extend(cmd)
        return MockProcess()

    with patch("herdr.inbox.dispatcher.load_projects", return_value=mock_projects), \
         patch("console.common.run", side_effect=mock_common_run):
        claimed = dispatcher.claim_next_task("ws-git-1")
        assert claimed is not None
        launched = dispatcher.launch_task(claimed)
        assert launched["status"] == "RUNNING"
        assert launched["workflow_id"] == "wf_second_success"
        # Verify real CLI command constructed by workflow_api:
        # 1. Must NOT contain --context
        assert "--context" not in captured_cmd
        # 2. Must contain git template
        assert "--template" in captured_cmd
        assert "software-development-v1" in captured_cmd
        # 3. Execution history retained in inbox task
        assert len(launched.get("execution_history", [])) == 1
        assert launched["execution_history"][0]["error"] == "Crash on attempt 1"


def test_factory_and_dispatcher_gate_alignment_on_active_workflows(tmp_path: Path):
    """P1-02: Factory and Dispatcher gates are strictly aligned.
    When a workflow is paused/active, Factory rejects concurrent creation and Dispatcher blocks claiming.
    When the workflow completes, both gates open cleanly.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader
    import pytest
    factory_path = Path("bin/herdr-factory").resolve()
    spec = importlib.util.spec_from_loader("herdr_factory", SourceFileLoader("herdr_factory", str(factory_path)))
    factory_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(factory_mod)

    db_file = tmp_path / "factory_waiting.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    p_root = tmp_path / "p_root"
    p_root.mkdir()
    project_rec = {
        "project_id": "proj-fac-1",
        "project_name": "Factory Proj",
        "project_root": str(p_root),
        "workspace_id": "ws-fac-1",
        "coordinator_pane_id": "pane-fac-1",
    }
    mock_projects = {
        "projects": {
            str(p_root): project_rec
        }
    }

    # Step 1: wf-1 is registered and paused (e.g. human intervention / verdict)
    save_workflow({
        "workflow_id": "wf-fac-1",
        "project_id": "proj-fac-1",
        "workspace_id": "ws-fac-1",
        "title": "Task 1",
        "status": "paused",
    }, db_path=db_file)

    # In Inbox: Task 1 is WAITING
    t1 = store.create_task(
        title="Task 1",
        instruction="Do 1",
        project_id="proj-fac-1",
        workspace_id="ws-fac-1",
        auto_confirm=True,
    )
    store.update_task_status(t1["id"], "CLAIMED")
    store.update_task_status(t1["id"], "RUNNING", workflow_id="wf-fac-1")
    store.update_task_status(t1["id"], "WAITING")

    # Task 2 is PENDING
    t2 = store.create_task(
        title="Task 2",
        instruction="Do 2",
        project_id="proj-fac-1",
        workspace_id="ws-fac-1",
        auto_confirm=True,
    )

    with patch("herdr.inbox.dispatcher.load_projects", return_value=mock_projects), \
         patch("herdr.projects._get_store") as mock_proj_store, \
         patch.object(factory_mod, "active_workflows_for_project") as mock_active_wf:

        # Mock projects workflow store list_workflows
        mock_proj_store.return_value.list_workflows.return_value = [
            {"workflow_id": "wf-fac-1", "project_id": "proj-fac-1", "status": "paused"}
        ]
        mock_active_wf.return_value = [
            {"workflow_id": "wf-fac-1", "project_id": "proj-fac-1", "status": "paused"}
        ]

        # 1. Factory blocks concurrent creation because wf-fac-1 is active (paused)
        with pytest.raises(SystemExit) as exc:
            factory_mod._reject_if_active_workflow(project_rec)
        assert exc.value.code == 2

        # 2. Dispatcher gate agrees: workspace is busy, so Dispatcher will NOT claim Task 2
        is_busy, _ = dispatcher.is_workspace_busy("ws-fac-1")
        assert is_busy is True

        claimed = dispatcher.claim_next_task("ws-fac-1")
        assert claimed is None
        assert store.get_task(t2["id"])["status"] == "PENDING"

        # 3. Now wf-fac-1 finishes
        mock_proj_store.return_value.list_workflows.return_value = [
            {"workflow_id": "wf-fac-1", "project_id": "proj-fac-1", "status": "completed"}
        ]
        mock_active_wf.return_value = []
        save_workflow({
            "workflow_id": "wf-fac-1",
            "project_id": "proj-fac-1",
            "workspace_id": "ws-fac-1",
            "status": "completed",
            "outcome": "success",
        }, db_path=db_file)
        dispatcher.reconcile_running_tasks()
        assert store.get_task(t1["id"])["status"] == "COMPLETED"

        # 4. Both gates open: Factory allows creation and Dispatcher claims Task 2
        factory_mod._reject_if_active_workflow(project_rec)  # should NOT raise

        is_busy_after, _ = dispatcher.is_workspace_busy("ws-fac-1")
        assert is_busy_after is False

        claimed_after = dispatcher.claim_next_task("ws-fac-1")
        assert claimed_after is not None
        assert claimed_after["id"] == t2["id"]


def test_workflow_resume_reacquires_workspace_and_blocks_if_occupied(tmp_path: Path):
    """P1-02: Resuming a paused workflow requires workspace; blocks if occupied by another workflow."""
    from herdr.kernel import resume_workflow
    from herdr.state_store import SQLiteStateStore

    db_file = tmp_path / "resume_workspace.db"
    store = InboxStore(db_file)
    s_store = SQLiteStateStore(db_file)

    p_root = tmp_path / "p_resume_root"
    p_root.mkdir()
    project_rec = {
        "project_id": "proj-res-1",
        "project_name": "Resume Proj",
        "project_root": str(p_root),
        "workspace_id": "ws-res-1",
        "coordinator_pane_id": "pane-res-1",
    }

    # Save wf-1 as paused and wf-2 as running in the same project
    save_workflow({
        "workflow_id": "wf-res-1",
        "project_id": "proj-res-1",
        "workspace_id": "ws-res-1",
        "title": "Paused Task",
        "status": "paused",
    }, db_path=db_file)

    save_workflow({
        "workflow_id": "wf-res-2",
        "project_id": "proj-res-1",
        "workspace_id": "ws-res-1",
        "title": "Running Task",
        "status": "running",
    }, db_path=db_file)

    with patch("herdr.projects._get_store", return_value=s_store):
        # 1. Attempt to resume wf-res-1 while wf-res-2 is running -> MUST raise RuntimeError
        with pytest.raises(RuntimeError) as exc_info:
            resume_workflow("wf-res-1", store=s_store)
        assert "occupied by active workflow 'wf-res-2'" in str(exc_info.value)

        # 2. Complete wf-res-2
        save_workflow({
            "workflow_id": "wf-res-2",
            "project_id": "proj-res-1",
            "workspace_id": "ws-res-1",
            "status": "completed",
        }, db_path=db_file)

        # 3. Now resume wf-res-1 -> succeeds!
        res = resume_workflow("wf-res-1", store=s_store)
        assert res["ok"] is True
        assert res["status"] == "running"


def test_safely_stop_fenced_workflow_stops_tasks_and_preserves_identity(tmp_path: Path):
    """P1: Verify that stopping a fenced workflow halts active tasks and preserves all identity metadata."""
    from herdr.state_db import list_tasks, save_task

    db_file = tmp_path / "stop_fenced.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_fenced_audit_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "title": "Production Deployment",
            "status": "running",
            "project_id": "proj-alpha",
            "workspace_id": "ws-alpha",
            "config": {"stages": ["build", "deploy"]},
        },
        db_path=db_file,
    )

    save_task(
        {
            "task_id": "task_alpha_1",
            "workflow_id": wf_id,
            "status": "working",
        },
        db_path=db_file,
    )

    # Invoke safe stopping
    dispatcher._safely_stop_fenced_workflow(wf_id, reason="launcher lost ownership")

    # 1. Execution must be safely stopped (task no longer working)
    tasks = list_tasks(workflow_id=wf_id, db_path=db_file)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "failed"

    # 2. Workflow identity must be 100% preserved
    from herdr.state_db import get_workflow
    wf = get_workflow(wf_id, db_path=db_file)
    assert wf is not None
    assert wf["status"] == "completed"
    assert wf.get("outcome") == "abandoned"
    assert wf.get("title") == "Production Deployment"
    assert wf.get("project_id") == "proj-alpha"
    assert wf.get("workspace_id") == "ws-alpha"
    assert wf.get("config") == {"stages": ["build", "deploy"]}

    # 3. Workspace is no longer busy once safely stopped
    busy, reason = dispatcher.is_workspace_busy("ws-alpha")
    assert busy is False


def test_launch_task_reconciles_registered_workflow_on_startup_exception(tmp_path: Path):
    """P2: If launcher raises after workflow registration, recover tracking instead of repeating work."""
    db_file = tmp_path / "launch_reconcile_p2.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    t = store.create_task(
        title="Async Launch Task",
        instruction="Do work",
        project_id="proj-p2",
        workspace_id="ws-p2",
        auto_confirm=True,
    )
    claimed = dispatcher.claim_next_task("ws-p2")
    assert claimed is not None

    def runner_register_then_timeout(task_dict):
        # Register workflow in DB
        wid = task_dict.get("workflow_id")
        save_workflow(
            {
                "workflow_id": wid,
                "title": "Registered Workflow",
                "status": "running",
                "project_id": "proj-p2",
                "workspace_id": "ws-p2",
            },
            db_path=db_file,
        )
        # Simulate process timeout or connection drop after registration
        raise TimeoutError("CLI startup timed out waiting for process ACK")

    # Launch task: should catch TimeoutError, detect registered workflow, and recover tracking as RUNNING
    launched = dispatcher.launch_task(claimed, runner_fn=runner_register_then_timeout, async_launch=False)
    assert launched is not None
    assert launched["status"] == "RUNNING"
    assigned_wf = launched["workflow_id"]
    assert assigned_wf is not None

    # Verify task in DB is RUNNING
    in_db = store.get_task(t["id"])
    assert in_db["status"] == "RUNNING"
    assert in_db["workflow_id"] == assigned_wf

    # Simulate underlying workflow completing
    save_workflow(
        {
            "workflow_id": assigned_wf,
            "title": "Registered Workflow",
            "status": "completed",
            "project_id": "proj-p2",
            "workspace_id": "ws-p2",
            "outcome": "delivered",
        },
        db_path=db_file,
    )

    # Reconcile: task should transition to COMPLETED
    reconciled = dispatcher.reconcile_running_tasks()
    assert len(reconciled) == 1
    assert reconciled[0]["id"] == t["id"]
    assert reconciled[0]["status"] == "COMPLETED"

    # Crucial check: subsequent dispatch must NOT re-claim or repeat the completed task!
    next_claim = dispatcher.claim_next_task("ws-p2")
    assert next_claim is None


def test_launch_task_requeues_when_workflow_never_registered(tmp_path: Path):
    """P2: If launcher raises before workflow registration, safely requeues to PENDING for retry."""
    db_file = tmp_path / "launch_never_reg.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    t = store.create_task(
        title="Failing Launch Task",
        instruction="Do work",
        project_id="proj-fail",
        workspace_id="ws-fail",
        auto_confirm=True,
    )
    claimed = dispatcher.claim_next_task("ws-fail")
    assert claimed is not None

    def runner_crash_immediately(task_dict):
        raise RuntimeError("Command not found: herdr-factory")

    launched = dispatcher.launch_task(claimed, runner_fn=runner_crash_immediately, async_launch=False)
    assert launched is not None
    assert launched["status"] == "PENDING"
    assert "Command not found" in (launched.get("error") or "")


def test_launcher_ownership_cas_check_aborts_when_ownership_lost(tmp_path: Path):
    """P1 (Thread 24): Launcher aborts before calling runner if task ownership or lease was lost."""
    db_file = tmp_path / "fencing_cas.db"
    store = InboxStore(db_file)
    dispatcher_a = InboxDispatcher(store=store, worker_id="worker-A", db_path=db_file)
    dispatcher_b = InboxDispatcher(store=store, worker_id="worker-B", db_path=db_file)

    t = store.create_task(
        title="Fenced Runner Task",
        instruction="Do something",
        project_id="proj-cas",
        workspace_id="ws-cas",
        auto_confirm=True,
    )
    claimed_by_a = dispatcher_a.claim_next_task("ws-cas")
    assert claimed_by_a is not None
    assert claimed_by_a["worker_id"].startswith("worker-A")

    # Simulate: while A is delayed, its lease expires, and worker B steals/recovers the task
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "UPDATE user_inbox_tasks SET worker_id = 'worker-B', lease_expires_at = ? WHERE id = ?",
        (time.time() + 300, claimed_by_a["id"]),
    )
    conn.commit()
    conn.close()

    runner_called = False

    def test_runner(task_dict):
        nonlocal runner_called
        runner_called = True
        return "wf_from_a"

    # Now A runs launch_task: ownership CAS must fail and abort WITHOUT invoking test_runner!
    result = dispatcher_a.launch_task(claimed_by_a, runner_fn=test_runner, async_launch=False)
    assert result is None
    assert runner_called is False


def test_cancel_waiting_task_stops_workflow_and_releases_workspace(tmp_path: Path):
    """P2 (Thread 25): Cancelling a WAITING task safely stops the underlying workflow and frees workspace."""
    from herdr.state_db import save_workflow, get_workflow

    db_file = tmp_path / "cancel_waiting.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_to_cancel_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj-w",
            "workspace_id": "ws-w",
            "title": "Active WF",
            "status": "paused",
            "config": {"workspace_id": "ws-w", "project_id": "proj-w"},
        },
        db_path=db_file,
    )

    t = store.create_task(
        title="Waiting Task",
        instruction="Wait for input",
        project_id="proj-w",
        workspace_id="ws-w",
        auto_confirm=True,
    )
    # Transition to WAITING associated with wf_id
    store.update_task_status(t["id"], new_status="CLAIMED")
    store.update_task_status(t["id"], new_status="RUNNING", workflow_id=wf_id)
    store.update_task_status(t["id"], new_status="WAITING")

    # Workspace is busy initially
    busy, reason = dispatcher.is_workspace_busy("ws-w")
    assert busy is True

    # Next task in queue is blocked
    t2 = store.create_task(
        title="Queued Task 2",
        instruction="Should run after cancel",
        project_id="proj-w",
        workspace_id="ws-w",
        auto_confirm=True,
    )
    assert dispatcher.claim_next_task("ws-w") is None

    # Cancel the waiting task
    cancelled = store.cancel_task(t["id"], reason="User cancelled waiting task")
    assert cancelled["status"] == "CANCELLED"

    # Underlying workflow must now be stopped (completed/abandoned)
    wf_after = get_workflow(wf_id, db_path=db_file)
    assert wf_after is not None
    assert wf_after["status"] == "completed"

    # Workspace is now free!
    busy_after, _ = dispatcher.is_workspace_busy("ws-w")
    assert busy_after is False

    # Next task can be claimed immediately!
    claimed_2 = dispatcher.claim_next_task("ws-w")
    assert claimed_2 is not None
    assert claimed_2["id"] == t2["id"]


def test_reconcile_active_tasks_paginates_beyond_500_tasks(tmp_path: Path):
    """P2 (Thread 26): Reconciliation paginates beyond the first 500 tasks, avoiding starvation."""
    from herdr.state_db import save_workflow

    db_file = tmp_path / "paginate_reconcile.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    # 1. Create task-oldest with a completed workflow
    wf_oldest = "wf_oldest"
    save_workflow(
        {
            "workflow_id": wf_oldest,
            "project_id": "p-old",
            "workspace_id": "ws-old",
            "title": "Oldest Completed",
            "status": "completed",
            "config": {"workspace_id": "ws-old", "project_id": "p-old"},
        },
        db_path=db_file,
    )
    t_old = store.create_task(
        title="Oldest Task",
        instruction="Do oldest",
        project_id="p-old",
        workspace_id="ws-old",
        auto_confirm=True,
    )
    store.update_task_status(t_old["id"], new_status="CLAIMED")
    store.update_task_status(t_old["id"], new_status="RUNNING", workflow_id=wf_oldest)

    # 2. Insert 505 newer tasks in WAITING status (to exceed 500 limit)
    conn = sqlite3.connect(str(db_file))
    now = time.time()
    for i in range(505):
        tid = f"task-newer-{i:04d}"
        conn.execute(
            """
            INSERT INTO user_inbox_tasks (
                id, user_id, title, instruction, status, priority, priority_weight,
                sort_order, created_at, updated_at
            ) VALUES (?, 'default', ?, 'Instruction', 'WAITING', 'NORMAL', 2, ?, ?, ?)
            """,
            (tid, f"Newer Task {i}", i, now + i + 1, now + i + 1),
        )
    conn.commit()
    conn.close()

    # Verify total waiting tasks is 505, running is 1
    _, waiting_total = store.list_tasks(status="WAITING")
    assert waiting_total == 505

    # 3. Reconcile: must paginate and reach the oldest running task!
    reconciled = dispatcher.reconcile_running_tasks()
    reconciled_ids = {r["id"] for r in reconciled}
    assert t_old["id"] in reconciled_ids

    # Oldest task is now COMPLETED
    t_old_updated = store.get_task(t_old["id"])
    assert t_old_updated["status"] == "COMPLETED"


def test_launcher_atomic_cas_renewal_aborts_when_lease_expired_after_preflight(tmp_path: Path):
    """P1-01: Atomic CAS renewal prevents TOCTOU race when lease expires between claim and runner execution."""
    db_file = tmp_path / "cas_renewal_expire.db"
    store = InboxStore(db_file)
    dispatcher_a = InboxDispatcher(store=store, worker_id="worker-A", db_path=db_file)

    t = store.create_task(
        title="TOCTOU Expire Task",
        instruction="Do work",
        project_id="p-exp",
        workspace_id="ws-exp",
        auto_confirm=True,
    )
    claimed_by_a = dispatcher_a.claim_next_task("ws-exp")
    assert claimed_by_a is not None
    assert claimed_by_a["worker_id"].startswith("worker-A")

    # Simulate: Worker A started launch_task and pre-assigned workflow_id,
    # but then process was paused/delayed and its lease expired in the database
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        UPDATE user_inbox_tasks
           SET workflow_id = 'wf_pre_assigned_1',
               lease_expires_at = ?
         WHERE id = ?
        """,
        (time.time() - 10.0, claimed_by_a["id"]),
    )
    conn.commit()
    conn.close()

    runner_executed = False

    def test_runner(task_dict):
        nonlocal runner_executed
        runner_executed = True
        return "wf_pre_assigned_1"

    # Worker A resumes and attempts launch_task: atomic CAS renewal fails because lease_expires_at < now!
    res = dispatcher_a.launch_task(claimed_by_a, runner_fn=test_runner, async_launch=False)
    assert res is None
    assert runner_executed is False, "Runner MUST NOT execute when atomic CAS renewal fails due to expired lease"


def test_launcher_heartbeat_loss_aborts_and_halts_workflow(tmp_path: Path):
    """P1-01: Heartbeat update rowcount==0 immediately marks lost ownership, halts orphan workflow, and preserves DB task."""
    from herdr.state_db import save_workflow, get_workflow

    db_file = tmp_path / "hb_loss.db"
    store = InboxStore(db_file)
    dispatcher_a = InboxDispatcher(store=store, worker_id="worker-A", db_path=db_file)

    t = store.create_task(
        title="Heartbeat Loss Task",
        instruction="Do something long",
        project_id="p-hb",
        workspace_id="ws-hb",
        auto_confirm=True,
    )
    claimed_by_a = dispatcher_a.claim_next_task("ws-hb")
    assert claimed_by_a is not None

    wf_id = "wf_hb_stolen_1"

    def slow_runner(task_dict):
        # Simulate: while runner is preparing, Worker B steals the task in the database
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "UPDATE user_inbox_tasks SET worker_id = 'worker-B', lease_expires_at = ? WHERE id = ?",
            (time.time() + 600.0, claimed_by_a["id"]),
        )
        conn.commit()
        conn.close()

        # Save workflow to DB so we can verify if it gets halted upon fencing mismatch
        save_workflow(
            {
                "workflow_id": wf_id,
                "project_id": "p-hb",
                "workspace_id": "ws-hb",
                "title": "Stolen WF",
                "status": "running",
            },
            db_path=db_file,
        )
        # Give heartbeat loop a moment to run and detect rowcount == 0
        time.sleep(0.05)
        return wf_id

    # Launch task with slow_runner
    res = dispatcher_a.launch_task(claimed_by_a, runner_fn=slow_runner, async_launch=False)
    assert res is None

    # Underlying orphan workflow must have been safely stopped (transitioned to completed/abandoned)
    wf_after = get_workflow(wf_id, db_path=db_file)
    assert wf_after is not None
    assert wf_after["status"] == "completed"

    # Task in DB must NOT have been overwritten or changed to PENDING/FAILED by Worker A
    t_after = store.get_task(claimed_by_a["id"])
    assert t_after["worker_id"] == "worker-B"
    assert t_after["status"] == "CLAIMED"


def test_cancel_task_fault_injection_halt_and_close_pane_both_fail(tmp_path: Path, monkeypatch):
    """P1-02: Cancelling fails and preserves original status when underlying execution cannot be confirmed stopped."""
    from herdr.state_db import save_workflow, get_workflow, save_task

    db_file = tmp_path / "cancel_fault.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_fault_cancel_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "p-fault",
            "workspace_id": "ws-fault",
            "title": "Faulty Cancel WF",
            "status": "running",
            "config": {"workspace_id": "ws-fault", "project_id": "p-fault"},
        },
        db_path=db_file,
    )
    # Task has an active process running in a pane
    save_task(
        {
            "task_id": "task_active_1",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%99",
        },
        db_path=db_file,
    )

    t = store.create_task(
        title="Fault Cancel Task",
        instruction="Running task that resists stopping",
        project_id="p-fault",
        workspace_id="ws-fault",
        auto_confirm=True,
    )
    store.update_task_status(t["id"], new_status="CLAIMED")
    store.update_task_status(t["id"], new_status="RUNNING", workflow_id=wf_id)

    monkeypatch.setattr("herdr.workflow_shutdown._stop_runtime", lambda *a, **k: False)

    # Attempting to cancel must RAISE RuntimeError and refuse to mark CANCELLED
    with pytest.raises(RuntimeError, match="failed to stop underlying workflow"):
        store.cancel_task(t["id"], reason="Kill this task")

    # Invariants verification:
    # 1. Task in DB remains RUNNING (NOT CANCELLED!)
    t_db = store.get_task(t["id"])
    assert t_db["status"] == "RUNNING"

    # 2. Workflow in DB is in closing/non-terminal state (NOT completed!)
    wf_db = get_workflow(wf_id, db_path=db_file)
    assert wf_db["status"] in ("closing", "running")
    assert wf_db["status"] not in ("completed", "abandoned")

    # 3. Workspace remains busy and does not leak occupancy
    busy, reason = dispatcher.is_workspace_busy("ws-fault")
    assert busy is True
    assert "RUNNING" in (reason or "")

    monkeypatch.setattr("herdr.workflow_shutdown._stop_runtime", lambda *a, **k: True)

    cancelled = store.cancel_task(t["id"], reason="Kill this task retry")
    assert cancelled["status"] == "CANCELLED"

    # Workflow is now completed
    wf_db_after = get_workflow(wf_id, db_path=db_file)
    assert wf_db_after["status"] == "completed"

    # Workspace is now free
    busy_after, _ = dispatcher.is_workspace_busy("ws-fault")
    assert busy_after is False


def test_safely_stop_workflow_halt_task_fails_and_close_pane_fails_returns_stop_failed(tmp_path: Path, monkeypatch):
    """P1-01: When both halt_task and close_pane fail, workflow and task remain active; workspace busy."""
    from herdr.state_db import save_workflow, get_workflow, save_task, get_task

    db_file = tmp_path / "stop_failed_both.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_stop_failed_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj-stop",
            "workspace_id": "ws-stop",
            "title": "Stop Failed WF",
            "status": "running",
            "config": {"workspace_id": "ws-stop", "project_id": "proj-stop"},
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_active_p1",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%42",
        },
        db_path=db_file,
    )

    monkeypatch.setattr("herdr.workflow_shutdown._stop_runtime", lambda *a, **k: False)

    res = dispatcher._safely_stop_fenced_workflow(wf_id, reason="testing stop failure")
    assert res == "STOP_FAILED"

    # Task status MUST NOT transition to failed (agent process still alive!)
    t = get_task("task_active_p1", db_path=db_file)
    assert t["status"] == "working"

    # Workflow MUST NOT transition to completed
    wf = get_workflow(wf_id, db_path=db_file)
    assert wf["status"] in ("closing", "running")
    assert wf["status"] not in ("completed", "abandoned")

    # Workspace remains busy
    busy, reason = dispatcher.is_workspace_busy("ws-stop")
    assert busy is True


def test_safely_stop_workflow_halt_task_fails_but_close_pane_succeeds_returns_stopped(tmp_path: Path, monkeypatch):
    """P1-01: When halt_task fails but close_pane terminates the pane, workflow stops and workspace is freed."""
    from herdr.state_db import save_workflow, get_workflow, save_task, get_task

    db_file = tmp_path / "stop_succeeds_close_pane.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_stop_pane_killed_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj-pane-kill",
            "workspace_id": "ws-pane-kill",
            "title": "Pane Killed WF",
            "status": "running",
            "config": {"workspace_id": "ws-pane-kill", "project_id": "proj-pane-kill"},
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_active_p2",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%43",
        },
        db_path=db_file,
    )

    monkeypatch.setattr("herdr.workflow_shutdown._stop_runtime", lambda *a, **k: True)

    res = dispatcher._safely_stop_fenced_workflow(wf_id, reason="pane kill test")
    assert res == "STOPPED"

    # Task status transitioned to failed
    t = get_task("task_active_p2", db_path=db_file)
    assert t["status"] == "failed"

    # Workflow transitioned to completed / abandoned
    wf = get_workflow(wf_id, db_path=db_file)
    assert wf["status"] == "completed"
    assert wf.get("outcome") == "abandoned"

    # Workspace is now freed
    busy, _ = dispatcher.is_workspace_busy("ws-pane-kill")
    assert busy is False


def test_launcher_interleaved_with_recovering_dispatcher_does_not_kill_workflow(tmp_path: Path):
    """P1-03: When another Dispatcher concurrently recovers the workflow to RUNNING, original Launcher does not kill it."""
    from herdr.state_db import save_workflow, get_workflow

    db_file = tmp_path / "interleaved_recovery.db"
    store = InboxStore(db_file)
    dispatcher_a = InboxDispatcher(store=store, worker_id="worker-A", db_path=db_file)
    dispatcher_b = InboxDispatcher(store=store, worker_id="worker-B", db_path=db_file)

    t = store.create_task(
        title="Interleaved Recovery Task",
        instruction="Long startup task",
        project_id="proj-inter",
        workspace_id="ws-inter",
        auto_confirm=True,
    )
    claimed_by_a = dispatcher_a.claim_next_task("ws-inter")
    assert claimed_by_a is not None
    tid = claimed_by_a["id"]
    wf_id = claimed_by_a.get("workflow_id") or "wf_inter_42"

    def slow_runner(task_dict):
        # 1. Register workflow in DB as running
        save_workflow(
            {
                "workflow_id": wf_id,
                "project_id": "proj-inter",
                "workspace_id": "ws-inter",
                "title": "Recovered Workflow",
                "status": "running",
                "config": {"workspace_id": "ws-inter", "project_id": "proj-inter"},
            },
            db_path=db_file,
        )
        # 2. Simulate Dispatcher B discovering the running workflow and recovering the task to RUNNING
        conn = sqlite3.connect(str(db_file))
        now = time.time()
        conn.execute(
            """
            UPDATE user_inbox_tasks
               SET status = 'RUNNING',
                   workflow_id = ?,
                   lease_expires_at = ?,
                   heartbeat_at = ?,
                   updated_at = ?
             WHERE id = ? AND status = 'CLAIMED'
            """,
            (wf_id, now + 180.0, now, now, tid),
        )
        conn.commit()
        conn.close()
        return wf_id

    # Dispatcher A runs launch_task: its UPDATE status='RUNNING' WHERE status='CLAIMED' affects 0 rows!
    res = dispatcher_a.launch_task(claimed_by_a, runner_fn=slow_runner, async_launch=False)

    # Dispatcher A must NOT have killed the recovered workflow!
    wf_after = get_workflow(wf_id, db_path=db_file)
    assert wf_after is not None
    assert wf_after["status"] == "running", "Concurrently recovered workflow MUST NOT be killed by original launcher"

    # Task must still be RUNNING
    assert res is not None
    assert res["status"] == "RUNNING"
    assert res["workflow_id"] == wf_id


def test_semaphore_released_on_fencing_cas_failures_allowing_subsequent_launches(tmp_path: Path):
    """P2-01: Verify Semaphore is released on all early exits (CAS failure), preventing thread pool starvation."""
    from herdr.inbox.dispatcher import _LAUNCH_SEMAPHORE

    db_file = tmp_path / "sem_release.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, worker_id="worker-sem", db_path=db_file)

    # Create 3 tasks in 3 distinct workspaces
    t1 = store.create_task(title="Task 1", instruction="Do 1", project_id="p1", workspace_id="ws-s1", auto_confirm=True)
    t2 = store.create_task(title="Task 2", instruction="Do 2", project_id="p2", workspace_id="ws-s2", auto_confirm=True)
    t3 = store.create_task(title="Task 3", instruction="Do 3", project_id="p3", workspace_id="ws-s3", auto_confirm=True)

    # Claim task 1 and task 2
    c1 = dispatcher.claim_next_task("ws-s1")
    c2 = dispatcher.claim_next_task("ws-s2")
    c3 = dispatcher.claim_next_task("ws-s3")
    assert c1 and c2 and c3

    # Force CAS renewal failure for c1 and c2 by setting lease_expires_at in the past
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "UPDATE user_inbox_tasks SET lease_expires_at = ? WHERE id IN (?, ?)",
        (time.time() - 100.0, c1["id"], c2["id"]),
    )
    conn.commit()
    conn.close()

    # Pre-acquire one permit to test that semaphore pool has exactly 2 capacity
    # Launch c1 with semaphore release requested (simulating async launch dispatch)
    # Both c1 and c2 fail CAS renewal
    # If semaphore is leaked, 2 permits are permanently lost, pool becomes empty (0 permits)
    _LAUNCH_SEMAPHORE.acquire()  # Permit 1 acquired
    _LAUNCH_SEMAPHORE.release()  # Release to ensure baseline is clean

    # Acquire permits as dispatch_all_workspaces does:
    assert _LAUNCH_SEMAPHORE.acquire(blocking=False)
    res1 = dispatcher.launch_task(c1, runner_fn=lambda t: "wf1", async_launch=False, release_sem_on_finish=True)
    assert res1 is None

    assert _LAUNCH_SEMAPHORE.acquire(blocking=False)
    res2 = dispatcher.launch_task(c2, runner_fn=lambda t: "wf2", async_launch=False, release_sem_on_finish=True)
    assert res2 is None

    # Now launch task 3: if c1 and c2 leaked permits, acquire would fail!
    assert _LAUNCH_SEMAPHORE.acquire(blocking=False), "Semaphore MUST have available permits; leak detected!"
    res3 = dispatcher.launch_task(c3, runner_fn=lambda t: "wf3", async_launch=False, release_sem_on_finish=True)
    assert res3 is not None
    assert res3["status"] == "RUNNING"


def test_safely_stop_workflow_halt_task_fails_and_ownership_unproven_refuses_to_close_pane(tmp_path: Path, monkeypatch):
    """Codex P1 / Invariant 11: When halt_task fails and runtime ownership is unproven, refuse to kill pane and preserve occupancy."""
    from herdr.state_db import save_workflow, get_workflow, save_task, get_task

    db_file = tmp_path / "stop_unproven_pane.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_unproven_pane_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj-unproven",
            "workspace_id": "ws-unproven",
            "title": "Unproven Pane WF",
            "status": "running",
            "config": {"workspace_id": "ws-unproven", "project_id": "proj-unproven"},
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_unproven_1",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%77",
        },
        db_path=db_file,
    )

    monkeypatch.setattr("herdr.workflow_shutdown._pane", lambda _: {"terminal_id": "foreign"})
    monkeypatch.setattr("herdr.task_resources.owned_live_pane", lambda _: (False, "session_mismatch"))

    close_pane_called = False

    def mock_close_pane(pid):
        nonlocal close_pane_called
        close_pane_called = True
        return True
    monkeypatch.setattr("herdr.workflow_shutdown.close_pane", mock_close_pane)

    res = dispatcher._safely_stop_fenced_workflow(wf_id, reason="testing unproven ownership")
    assert res == "STOP_FAILED", "Must return STOP_FAILED when runtime ownership is unproven"
    assert close_pane_called is False, "Must NOT call close_pane when pane runtime ownership is not proven (Invariant 11)"

    # Task status remains working
    t = get_task("task_unproven_1", db_path=db_file)
    assert t["status"] == "working"

    # Workflow remains closing / non-terminal
    wf = get_workflow(wf_id, db_path=db_file)
    assert wf["status"] in ("closing", "running")
    assert wf["status"] not in ("completed", "abandoned")

    # Workspace remains busy
    busy, reason = dispatcher.is_workspace_busy("ws-unproven")
    assert busy is True


def test_safely_stop_workflow_pane_missing_without_process_evidence_preserves_occupancy(tmp_path: Path, monkeypatch):
    """A vanished pane does not establish process exit without a process receipt."""
    from herdr.state_db import save_workflow, get_workflow, save_task, get_task

    db_file = tmp_path / "stop_missing_pane.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_missing_pane_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj-missing",
            "workspace_id": "ws-missing",
            "title": "Missing Pane WF",
            "status": "running",
            "config": {"workspace_id": "ws-missing", "project_id": "proj-missing"},
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_missing_1",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%88",
        },
        db_path=db_file,
    )

    # A missing pane without process evidence is an unknown stop.
    monkeypatch.setattr("herdr.workflow_shutdown._pane", lambda _: None)
    assert dispatcher._safely_stop_fenced_workflow(wf_id) == "STOP_FAILED"
    assert get_workflow(wf_id, db_path=db_file)["status"] == "closing"
    assert dispatcher.is_workspace_busy("ws-missing")[0]

def test_full_execution_fencing_lifecycle(tmp_path: Path):
    """Comprehensive test of full execution fencing lifecycle:
    领取任务 -> 获得执行租约 -> 原子校验执行权 -> 注册 Workflow -> 持续续租 -> 处理恢复与并发竞争 -> 确认执行终态 -> 释放工作区
    """
    from herdr.state_db import save_workflow, get_workflow, transition_workflow

    db_file = tmp_path / "lifecycle.db"
    store = InboxStore(db_file)
    dispatcher_1 = InboxDispatcher(store=store, worker_id="worker-node-1", db_path=db_file)
    dispatcher_2 = InboxDispatcher(store=store, worker_id="worker-node-2", db_path=db_file)

    # 1. User submits 2 tasks to same workspace (Task 1 and Task 2)
    t1 = store.create_task(
        title="Lifecycle Task 1",
        instruction="First task in queue",
        project_id="proj-life",
        workspace_id="ws-life",
        auto_confirm=True,
    )
    t2 = store.create_task(
        title="Lifecycle Task 2",
        instruction="Second task queued behind Task 1",
        project_id="proj-life",
        workspace_id="ws-life",
        auto_confirm=True,
    )

    # Step 1: 领取任务 + 获得执行租约
    claimed = dispatcher_1.claim_next_task("ws-life", lease_duration_seconds=180.0)
    assert claimed is not None
    assert claimed["id"] == t1["id"]
    assert claimed["status"] == "CLAIMED"
    assert claimed["worker_id"].startswith("worker-node-1")
    assert claimed["lease_expires_at"] > time.time()

    # Workspace is immediately busy; Task 2 cannot jump ahead
    assert dispatcher_2.claim_next_task("ws-life") is None

    wf_life_id = "wf_life_exec_1"

    # Step 2: 原子校验执行权 + 注册 Workflow
    def lifecycle_runner(task_dict):
        # Register workflow in state_db
        save_workflow(
            {
                "workflow_id": wf_life_id,
                "project_id": "proj-life",
                "workspace_id": "ws-life",
                "title": "Lifecycle Execution Workflow",
                "status": "running",
                "config": {"workspace_id": "ws-life", "project_id": "proj-life"},
            },
            db_path=db_file,
        )
        return wf_life_id

    # Step 3: Launch task
    launched = dispatcher_1.launch_task(claimed, runner_fn=lifecycle_runner, async_launch=False)
    assert launched is not None
    assert launched["status"] == "RUNNING"
    assert launched["workflow_id"] == wf_life_id

    # Step 4: 持续续租 (reconcile extends heartbeat and lease for RUNNING tasks)
    dispatcher_1.reconcile_running_tasks()
    t1_running = store.get_task(t1["id"])
    assert t1_running["status"] == "RUNNING"
    assert t1_running["lease_expires_at"] > time.time()

    # Step 5: 并发竞争与恢复处理 (simulate node 1 pause/delay, node 2 reconcile recovers and keeps workflow running)
    dispatcher_2.reconcile_running_tasks()
    t1_chk = store.get_task(t1["id"])
    assert t1_chk["status"] == "RUNNING"
    assert t1_chk["workflow_id"] == wf_life_id

    # Step 6: 确认执行终态 (Workflow engine completes the workflow)
    transition_workflow(
        workflow_id=wf_life_id,
        to_status="completed",
        reason="all stages successfully finished",
        db_path=db_file,
    )

    # Step 7: Reconcile confirms terminal state and marks task COMPLETED
    reconciled = dispatcher_1.reconcile_running_tasks()
    assert len(reconciled) >= 1
    t1_done = store.get_task(t1["id"])
    assert t1_done["status"] == "COMPLETED"

    # Step 8: 释放工作区 + 自动接续下一个任务
    busy, reason = dispatcher_1.is_workspace_busy("ws-life")
    assert busy is False

    # Dispatch sweep automatically claims and launches Task 2!
    sweep_res = dispatcher_1.dispatch_sweep(runner_fn=lambda t: "wf_life_exec_2", async_launch=False)
    assert len(sweep_res["launched"]) == 1
    assert sweep_res["launched"][0]["id"] == t2["id"]
    assert sweep_res["launched"][0]["status"] == "RUNNING"


def test_lease_expired_launcher_resumes_refuses_to_launch_workflow(tmp_path: Path):
    """Review Round 11 / P1-01: Launcher A lease expires, resumes, cannot launch workflow without valid execution fencing."""
    db_file = tmp_path / "lease_expired_launch.db"
    store = InboxStore(db_file)
    disp_a = InboxDispatcher(store=store, db_path=db_file, worker_id="worker_node_a")

    with patch("herdr.inbox.dispatcher.load_projects", return_value={"projects": {}}):
        t = store.create_task(
            title="Expire Test Task",
            instruction="Task to test fencing on pause/resume",
            project_id="p-exp",
            workspace_id="ws-exp",
            auto_confirm=True,
        )
        claimed_a = disp_a.claim_next_task("ws-exp")
        assert claimed_a is not None
        assert claimed_a["status"] == "CLAIMED"
        assert claimed_a["worker_id"].startswith("worker_node_a")

        # Simulate Launcher A being paused while its lease expires in the database
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "UPDATE user_inbox_tasks SET lease_expires_at = ? WHERE id = ?",
            (time.time() - 50.0, t["id"]),
        )
        conn.commit()
        conn.close()

        runner_called = False

        def spy_runner(task_dict):
            nonlocal runner_called
            runner_called = True
            return "wf_should_never_launch"

        # Launcher A resumes and tries to launch
        res = disp_a.launch_task(claimed_a, runner_fn=spy_runner, async_launch=False)

        # 1. Launcher A must reject launch and return None
        assert res is None, "Launcher A must abort launch because lease expired"
        # 2. Invariant 1: Runner was NEVER invoked (no side-effect / workflow creation permitted)
        assert runner_called is False, "Runner MUST NOT be invoked when lease expired"
        # 3. Task in DB remains CLAIMED (or uncorrupted)
        t_db = store.get_task(t["id"])
        assert t_db["workflow_id"] != "wf_should_never_launch"


def test_pane_reused_by_other_agent_cancel_refuses_to_interrupt_new_agent(tmp_path: Path, monkeypatch):
    """Review Round 11 / P1-02: Cancelling old task when pane is reused by another agent MUST NOT send interrupt or close pane."""
    from herdr.state_db import save_workflow, get_workflow, save_task, get_task

    db_file = tmp_path / "pane_reused_cancel.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    wf_id = "wf_old_agent_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj-reused",
            "workspace_id": "ws-reused",
            "title": "Old Workflow",
            "status": "running",
            "config": {"workspace_id": "ws-reused", "project_id": "proj-reused"},
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_old_1",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%88",
        },
        db_path=db_file,
    )

    t = store.create_task(
        title="Old Task with Reused Pane",
        instruction="Do work",
        project_id="proj-reused",
        workspace_id="ws-reused",
        auto_confirm=True,
    )
    store.update_task_status(t["id"], new_status="CLAIMED")
    store.update_task_status(t["id"], new_status="RUNNING", workflow_id=wf_id)

    # Injected probe: Pane %88 is now active with another agent session!
    halt_called = False
    close_called = False

    monkeypatch.setattr("herdr.workflow_shutdown._pane", lambda _: {"terminal_id": "foreign"})
    monkeypatch.setattr("herdr.task_resources.owned_live_pane", lambda _: (False, "session_mismatch"))
    def unexpected_adapter(agent):
        nonlocal halt_called
        halt_called = True
        pytest.fail("refused runtime must never resolve a signaling adapter")

    monkeypatch.setattr("herdr.workflow_shutdown.get_agent_adapter", unexpected_adapter)

    def spy_close_pane(pid):
        nonlocal close_called
        close_called = True
        return True
    monkeypatch.setattr("herdr.workflow_shutdown.close_pane", spy_close_pane)

    # Cancellation must fail because execution cannot be safely confirmed stopped
    with pytest.raises(RuntimeError, match="failed to stop underlying workflow"):
        store.cancel_task(t["id"], reason="User cancels old task")

    # Invariant 2: halt_task and close_pane MUST NOT be called on reused pane!
    assert halt_called is False, "halt_task MUST NOT be called on unproven/reused pane!"
    assert close_called is False, "close_pane MUST NOT be called on unproven/reused pane!"

    # Old task remains working and workflow remains closing / non-terminal
    t_db = store.get_task(t["id"])
    assert t_db["status"] == "RUNNING"
    task_wf = get_task("task_old_1", db_path=db_file)
    assert task_wf["status"] == "working"
    wf_db = get_workflow(wf_id, db_path=db_file)
    assert wf_db["status"] in ("closing", "running")
    assert wf_db["status"] not in ("completed", "abandoned")


def test_pending_task_concurrent_claim_and_cancel_consistent_final_state(tmp_path: Path):
    """Review Round 11 / P2-01: Concurrent claim and cancel on PENDING task reaches consistent final state."""
    db_file = tmp_path / "concurrent_claim_cancel.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file, worker_id="worker_race")

    t = store.create_task(
        title="Concurrent Task",
        instruction="Instruction",
        project_id="p-race",
        workspace_id="ws-race",
        auto_confirm=True,
    )
    assert t["status"] == "PENDING"

    import threading

    def do_cancel():
        try:
            store.cancel_task(t["id"], reason="Concurrent cancellation")
        except Exception:
            pass

    def do_claim_and_launch():
        try:
            claimed = dispatcher.claim_next_task("ws-race")
            if claimed:
                dispatcher.launch_task(claimed, runner_fn=lambda t_dict: "wf_concurrent_1", async_launch=False)
        except Exception:
            pass

    th1 = threading.Thread(target=do_cancel)
    th2 = threading.Thread(target=do_claim_and_launch)

    th1.start()
    th2.start()
    th1.join(timeout=5.0)
    th2.join(timeout=5.0)

    final_task = store.get_task(t["id"])
    # Final state must be CANCELLED (or cleanly completed)
    assert final_task["status"] in ("CANCELLED", "COMPLETED")
    if final_task["status"] == "CANCELLED":
        busy, reason = dispatcher.is_workspace_busy("ws-race")
        assert busy is False, f"Workspace must not be held when task is CANCELLED: {reason}"


def test_claimed_task_cancellation_both_inbox_and_workflow_reach_terminal_state(tmp_path: Path, monkeypatch):
    """Review Round 11 / P2-02: Cancelling a CLAIMED task safely halts workflow and enters CANCELLED."""
    from herdr.state_db import save_workflow, get_workflow, save_task, get_task

    db_file = tmp_path / "cancel_claimed.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    t = store.create_task(
        title="Claimed Cancel Task",
        instruction="Instruction",
        project_id="p-claim",
        workspace_id="ws-claim",
        auto_confirm=True,
    )
    claimed = dispatcher.claim_next_task("ws-claim")
    assert claimed is not None
    assert claimed["status"] == "CLAIMED"

    wf_id = "wf_claimed_cancel_1"
    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "p-claim",
            "workspace_id": "ws-claim",
            "title": "Claimed Cancel WF",
            "status": "running",
            "config": {"workspace_id": "ws-claim", "project_id": "p-claim"},
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_sub_1",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%44",
        },
        db_path=db_file,
    )

    # Attach workflow_id to the claimed task in store
    store.update_task_status(claimed["id"], new_status="CLAIMED", workflow_id=wf_id)

    monkeypatch.setattr("herdr.workflow_shutdown._stop_runtime", lambda *a, **k: True)



    # Cancel the CLAIMED task
    cancelled = store.cancel_task(claimed["id"], reason="User cancels CLAIMED task")

    # 1. Inbox task successfully transitions CLAIMED -> CANCELLED without state machine error
    assert cancelled["status"] == "CANCELLED"
    t_after = store.get_task(claimed["id"])
    assert t_after["status"] == "CANCELLED"

    # 2. Workflow is stopped and transitioned to completed / abandoned
    wf_after = get_workflow(wf_id, db_path=db_file)
    assert wf_after["status"] == "completed"
    assert wf_after.get("outcome") == "abandoned"

    # 3. Workspace is completely freed
    busy, reason = dispatcher.is_workspace_busy("ws-claim")
    assert busy is False, f"Workspace must not be busy after cancel: {reason}"


def test_lease_expired_launcher_process_pause_and_resume_rejected_at_workflow_registration_gate(tmp_path: Path, monkeypatch):
    """Review Round 12 / P1-02: Thread/Process pause-resume lease expiry test.

    Verifies that when Worker A is paused during launch, its lease expires, and Worker B takes over:
    1. Worker B successfully registers workflow wf_B.
    2. Worker A resumes and enters Workflow registration entrance (bin/herdr-factory).
    3. Workflow registration gate rejects Worker A with fencing error.
    4. Task and Workflow remain cleanly bound to Worker B without duplicate workflow creation.
    """
    import importlib.machinery, importlib.util, json
    loader = importlib.machinery.SourceFileLoader(
        "factory_fencing_test",
        str(Path(__file__).resolve().parent.parent / "bin" / "herdr-factory"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    factory = importlib.util.module_from_spec(spec)
    loader.exec_module(factory)

    db_file = tmp_path / "fenced_gate.db"
    store = InboxStore(db_file)
    disp_a = InboxDispatcher(store=store, db_path=db_file, worker_id="worker_A")

    proj_root = tmp_path / "project"
    proj_root.mkdir()
    wf_file = proj_root / "workflow.json"
    wf_file.write_text(json.dumps({"workflow_template": "software-development-v1", "nodes": []}))
    proj = {
        "project_id": "p-fence",
        "project_name": "fence-project",
        "project_root": str(proj_root),
        "workspace_id": "ws-fence",
        "coordinator_pane_id": "pane-coord-1",
        "workflow_file": str(wf_file),
    }

    t = store.create_task(
        title="Fenced Gate Task",
        instruction="Run fenced task",
        project_id="p-fence",
        workspace_id="ws-fence",
        auto_confirm=True,
    )
    claimed_a = disp_a.claim_next_task("ws-fence")
    assert claimed_a is not None
    worker_a = claimed_a["worker_id"]

    monkeypatch.setattr(factory, "run_workflow_preflight", lambda *a, **k: (["auto"], {}))
    monkeypatch.setattr(factory, "resolve_project_for_template", lambda *a, **k: proj)
    monkeypatch.setattr(factory, "active_workflows_for_project", lambda *a, **k: [])
    monkeypatch.setattr("herdr.projects.active_workflows_for_project", lambda *a, **k: [])

    # 1. Simulate Worker A being paused (e.g. process freeze / GC pause / sleep) while lease expires
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "UPDATE user_inbox_tasks SET lease_expires_at = ? WHERE id = ?",
        (time.time() - 20.0, t["id"]),
    )
    conn.commit()
    conn.close()

    # Worker A attempts to enter registration gate with expired lease: REJECTED!
    with pytest.raises(RuntimeError, match="Inbox task fencing violation: execution lease expired"):
        factory.start_workflow(
            "Run fenced task",
            workflow_id="wf_A",
            project_path=str(proj_root),
            inbox_task_id=t["id"],
            fencing_token=worker_a,
            launch_attempt_id=1,
            db_path=str(db_file),
        )

    # 2. Worker B recovers the task
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "UPDATE user_inbox_tasks SET worker_id = 'worker_B', lease_expires_at = ?, attempt_count = 2 WHERE id = ?",
        (time.time() + 180.0, t["id"]),
    )
    conn.commit()
    conn.close()

    # Worker B successfully registers workflow wf_B
    registered_wid = factory.start_workflow(
        "Run fenced task",
        workflow_id="wf_B",
        project_path=str(proj_root),
        inbox_task_id=t["id"],
        fencing_token="worker_B",
        launch_attempt_id=2,
        db_path=str(db_file),
    )
    assert registered_wid == "wf_B"

    # Task is now bound to wf_B
    t_after_b = store.get_task(t["id"])
    assert t_after_b["workflow_id"] == "wf_B"

    # 3. Worker A resumes and attempts to enter registration gate: REJECTED!
    with pytest.raises(RuntimeError, match="Inbox task fencing violation"):
        factory.start_workflow(
            "Run fenced task",
            workflow_id="wf_A",
            project_path=str(proj_root),
            inbox_task_id=t["id"],
            fencing_token=worker_a,
            launch_attempt_id=1,
            db_path=str(db_file),
        )

    # 4. Invariant: Task remains bound to wf_B; wf_A was never created
    t_final = store.get_task(t["id"])
    assert t_final["workflow_id"] == "wf_B"
    assert t_final["status"] == "CLAIMED"

    # 5. Idempotent re-entry by Worker B succeeds
    reentry = factory.start_workflow(
        "Run fenced task",
        workflow_id="wf_B",
        project_path=str(proj_root),
        inbox_task_id=t["id"],
        fencing_token="worker_B",
        launch_attempt_id=2,
        db_path=str(db_file),
    )
    assert reentry == "wf_B"


def test_preallocated_workflow_id_lease_expired_rejected_at_registration_gate(tmp_path: Path, monkeypatch):
    """Review Round 13 / P1 & P2: Real production sequence regression test.

    1. A claims task.
    2. A pre-assigns wf_A and writes it to Inbox (simulating dispatcher.py pre-write).
    3. A lease expires while paused.
    4. B takes over task, receives new Worker Token (worker_B) and attempt_count 2.
    5. A resumes with original wf_A and old token/attempt into Factory registration gate.
    6. Factory MUST reject A (must NOT bypass execution authority checks via db_wid == workflow_id).
    7. Verify wf_A is NOT in workflows table (no second workflow registered).
    8. B registers wf_B successfully and proceeds.
    9. B idempotent re-entry succeeds.
    """
    import importlib.machinery, importlib.util, json
    loader = importlib.machinery.SourceFileLoader(
        "factory_fencing_test_prod",
        str(Path(__file__).resolve().parent.parent / "bin" / "herdr-factory"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    factory = importlib.util.module_from_spec(spec)
    loader.exec_module(factory)

    db_file = tmp_path / "fenced_prod.db"
    store = InboxStore(db_file)
    disp_a = InboxDispatcher(store=store, db_path=db_file, worker_id="worker_A")

    proj_root = tmp_path / "project_prod"
    proj_root.mkdir()
    wf_file = proj_root / "workflow.json"
    wf_file.write_text(json.dumps({"workflow_template": "software-development-v1", "nodes": []}))
    proj = {
        "project_id": "p-prod",
        "project_name": "prod-project",
        "project_root": str(proj_root),
        "workspace_id": "ws-prod",
        "coordinator_pane_id": "pane-coord-prod",
        "workflow_file": str(wf_file),
    }

    # 1. A claims task
    t = store.create_task(
        title="Prod Fenced Task",
        instruction="Run prod fenced task",
        project_id="p-prod",
        workspace_id="ws-prod",
        auto_confirm=True,
    )
    claimed_a = disp_a.claim_next_task("ws-prod")
    assert claimed_a is not None
    worker_a = claimed_a["worker_id"]
    assert claimed_a["attempt_count"] == 1

    monkeypatch.setattr(factory, "run_workflow_preflight", lambda *a, **k: (["auto"], {}))
    monkeypatch.setattr(factory, "resolve_project_for_template", lambda *a, **k: proj)
    monkeypatch.setattr(factory, "active_workflows_for_project", lambda *a, **k: [])
    monkeypatch.setattr("herdr.projects.active_workflows_for_project", lambda *a, **k: [])

    # 2. A pre-assigns wf_A and writes it to Inbox (exact dispatcher pre-write behavior)
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        UPDATE user_inbox_tasks
           SET workflow_id = 'wf_A',
               lease_expires_at = ?,
               heartbeat_at = ?,
               updated_at = ?
         WHERE id = ? AND worker_id = ?
        """,
        (time.time() + 180.0, time.time(), time.time(), t["id"], worker_a),
    )
    conn.commit()
    conn.close()

    # 3. A is paused; its lease expires in database
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "UPDATE user_inbox_tasks SET lease_expires_at = ? WHERE id = ?",
        (time.time() - 30.0, t["id"]),
    )
    conn.commit()
    conn.close()

    # 4. Real recovery: disp_b recovers the stale task via recover_stale_tasks()
    disp_b = InboxDispatcher(store=store, db_path=db_file, worker_id="worker_B")
    recovered = disp_b.recover_stale_tasks()
    assert len(recovered) == 1
    assert recovered[0]["id"] == t["id"]
    assert recovered[0]["status"] == "PENDING"
    # Crucial: workflow_id was pre-allocated by A and is retained by recover_stale_tasks!
    assert recovered[0]["workflow_id"] == "wf_A"

    # 5. Worker B claims the recovered task via claim_next_task()
    claimed_b = disp_b.claim_next_task("ws-prod")
    assert claimed_b is not None
    worker_b = claimed_b["worker_id"]
    assert claimed_b["attempt_count"] == 2
    # Pre-allocated workflow_id remains intact in the claimed task record
    assert claimed_b["workflow_id"] == "wf_A"

    # 6. A resumes with original wf_A and old token/attempt into Factory registration gate:
    # CRITICAL: Factory MUST reject A, even though A originally pre-assigned wf_A!
    with pytest.raises(RuntimeError, match="Inbox task fencing violation: execution lease lost"):
        factory.start_workflow(
            "Run prod fenced task",
            workflow_id="wf_A",
            project_path=str(proj_root),
            inbox_task_id=t["id"],
            fencing_token=worker_a,
            launch_attempt_id=f"{worker_a}:1",
            db_path=str(db_file),
        )

    # 7. Confirm wf_A is NOT in workflows table
    conn = sqlite3.connect(str(db_file))
    cur = conn.execute("SELECT workflow_id FROM workflows WHERE workflow_id = 'wf_A'")
    assert cur.fetchone() is None, "wf_A MUST NOT be registered by expired Launcher A"
    conn.close()

    # 8. B enters Factory with valid execution authority (worker_b, attempt 2) and registers wf_A
    registered_wid = factory.start_workflow(
        "Run prod fenced task",
        workflow_id="wf_A",
        project_path=str(proj_root),
        inbox_task_id=t["id"],
        fencing_token=worker_b,
        launch_attempt_id=f"{worker_b}:2",
        db_path=str(db_file),
    )
    assert registered_wid == "wf_A"

    # Task is now registered and bound to wf_A
    t_after_b = store.get_task(t["id"])
    assert t_after_b["workflow_id"] == "wf_A"

    # 9. B idempotent re-entry succeeds
    reentry = factory.start_workflow(
        "Run prod fenced task",
        workflow_id="wf_A",
        project_path=str(proj_root),
        inbox_task_id=t["id"],
        fencing_token=worker_b,
        launch_attempt_id=f"{worker_b}:2",
        db_path=str(db_file),
    )
    assert reentry == "wf_A"


def test_fencing_gate_fails_closed_when_db_or_table_missing(tmp_path: Path):
    """P1 / Fail Closed: When inbox_task_id is provided but DB or table is missing, raise RuntimeError."""
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader(
        "factory_fencing_fail_closed",
        str(Path(__file__).resolve().parent.parent / "bin" / "herdr-factory"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    factory = importlib.util.module_from_spec(spec)
    loader.exec_module(factory)

    # Missing database
    with pytest.raises(RuntimeError, match="Inbox task fencing failure: database not found"):
        factory._verify_and_bind_inbox_task_fencing(
            db_path=str(tmp_path / "nonexistent.db"),
            inbox_task_id="task_fc_01",
            fencing_token="w1",
            launch_attempt_id="w1:1",
        )

    # Missing table
    empty_db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(empty_db))
    conn.execute("CREATE TABLE dummy (id TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="Inbox task fencing failure: user_inbox_tasks table not found"):
        factory._verify_and_bind_inbox_task_fencing(
            db_path=str(empty_db),
            inbox_task_id="task_fc_01",
            fencing_token="w1",
            launch_attempt_id="w1:1",
        )


def test_enforce_inbox_task_fencing_requires_fencing_token_and_attempt_id(tmp_path: Path):
    """Factory fencing parameters check: when inbox_task_id is provided, fencing_token and launch_attempt_id are required."""
    import importlib.machinery, importlib.util
    loader = importlib.machinery.SourceFileLoader(
        "factory_fencing_required_params",
        str(Path(__file__).resolve().parent.parent / "bin" / "herdr-factory"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    factory = importlib.util.module_from_spec(spec)
    loader.exec_module(factory)

    db_file = tmp_path / "fencing_req.db"
    store = InboxStore(db_file)
    t = store.create_task(
        title="Req params task",
        instruction="run",
        project_id="p-1",
        workspace_id="ws-1",
        auto_confirm=True,
    )
    disp = InboxDispatcher(store=store, db_path=db_file, worker_id="w1")
    claimed = disp.claim_next_task("ws-1")
    assert claimed is not None
    token = claimed["worker_id"]

    # 1. Missing fencing_token raises RuntimeError
    with pytest.raises(RuntimeError, match="fencing_token is required when inbox_task_id is provided"):
        factory._verify_and_bind_inbox_task_fencing(
            db_path=str(db_file),
            inbox_task_id=t["id"],
            fencing_token=None,
            launch_attempt_id=f"{token}:1",
        )

    # 2. Missing launch_attempt_id raises RuntimeError
    with pytest.raises(RuntimeError, match="launch_attempt_id is required when inbox_task_id is provided"):
        factory._verify_and_bind_inbox_task_fencing(
            db_path=str(db_file),
            inbox_task_id=t["id"],
            fencing_token=token,
            launch_attempt_id=None,
        )
