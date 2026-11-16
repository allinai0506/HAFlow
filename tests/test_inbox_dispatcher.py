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
