"""End-to-End Acceptance Pipeline for User Task Inbox V1.

Verifies the ultimate acceptance criteria:
1. Continuous submission of 10 tasks executed one-by-one with automatic continuation,
   strict priority + FIFO ordering, zero task loss, zero duplicate execution,
   zero wrong assignment, and zero manual intervention.
2. Draft and unconfirmed scope isolation (drafts are never dispatched).
3. Multi-workspace parallel execution without cross-workspace blocking.
4. Worker crash recovery, error capture, workspace unlock, and retry.
5. Idempotent submission preventing duplicate tasks.
"""

import pytest
from pathlib import Path
from unittest.mock import patch

from herdr.inbox.dispatcher import InboxDispatcher
from herdr.inbox.store import InboxStore
from herdr.state.workflow_task_store import save_workflow, get_workflow


def test_ten_tasks_continuous_auto_dispatch(tmp_path: Path):
    """Ultimate acceptance test:

    Submit 10 tasks with mixed priorities into a single workspace.
    Verify they execute strictly in priority order (URGENT > HIGH > NORMAL > LOW)
    with FIFO tie-breaking, auto-continue upon completion, maintain single-task mutex,
    and achieve 100% completion with zero manual intervention, zero duplication,
    and zero task loss.
    """
    db_file = tmp_path / "e2e_10_tasks.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    mock_projects = {
        "projects": {
            "proj_main": {
                "project_id": "proj_main",
                "workspace_id": "ws_core",
                "root": str(tmp_path / "main_repo"),
            }
        }
    }

    with patch("herdr.inbox.dispatcher.load_projects", return_value=mock_projects):
        # 1. Sequentially submit 10 tasks with diverse priorities
        task_specs = [
            ("Task 1 (LOW)", "LOW"),
            ("Task 2 (NORMAL)", "NORMAL"),
            ("Task 3 (URGENT)", "URGENT"),
            ("Task 4 (HIGH)", "HIGH"),
            ("Task 5 (NORMAL)", "NORMAL"),
            ("Task 6 (URGENT)", "URGENT"),
            ("Task 7 (HIGH)", "HIGH"),
            ("Task 8 (LOW)", "LOW"),
            ("Task 9 (NORMAL)", "NORMAL"),
            ("Task 10 (HIGH)", "HIGH"),
        ]

        created_tasks = []
        for title, priority in task_specs:
            t = store.create_task(
                title=title,
                instruction=f"Instruction for {title}",
                project_id="proj_main",
                workspace_id="ws_core",
                priority=priority,
                auto_confirm=True,
            )
            created_tasks.append(t)

        assert len(created_tasks) == 10
        # All 10 initially PENDING
        for t in created_tasks:
            fetched = store.get_task(t["id"])
            assert fetched["status"] == "PENDING"
            assert fetched["workspace_id"] == "ws_core"

        # Expected execution order:
        # URGENT: Task 3, Task 6
        # HIGH:   Task 4, Task 7, Task 10
        # NORMAL: Task 2, Task 5, Task 9
        # LOW:    Task 1, Task 8
        expected_execution_order = [
            created_tasks[2]["id"],  # Task 3 (URGENT)
            created_tasks[5]["id"],  # Task 6 (URGENT)
            created_tasks[3]["id"],  # Task 4 (HIGH)
            created_tasks[6]["id"],  # Task 7 (HIGH)
            created_tasks[9]["id"],  # Task 10 (HIGH)
            created_tasks[1]["id"],  # Task 2 (NORMAL)
            created_tasks[4]["id"],  # Task 5 (NORMAL)
            created_tasks[8]["id"],  # Task 9 (NORMAL)
            created_tasks[0]["id"],  # Task 1 (LOW)
            created_tasks[7]["id"],  # Task 8 (LOW)
        ]

        actual_executed_order = []
        workflow_ids_seen = set()

        def mock_runner(task):
            wf_id = f"wf_run_{task['id']}"
            assert wf_id not in workflow_ids_seen, f"Duplicate workflow launch detected for task {task['id']}"
            workflow_ids_seen.add(wf_id)
            save_workflow(
                {"workflow_id": wf_id, "title": task["title"], "status": "running"},
                db_path=db_file,
            )
            return wf_id

        # 2. Pipeline execution loop simulating the automated Controller
        # First sweep launches the initial task (highest priority: Task 3 URGENT)
        sweep = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep["launched"]) == 1
        current_task = sweep["launched"][0]
        actual_executed_order.append(current_task["id"])

        for step in range(1, 10):
            # Verify single-task mutex: workspace is busy with current task
            is_busy, reason = dispatcher.is_workspace_busy("ws_core")
            assert is_busy is True, f"Step {step}: workspace must be busy while current task runs"

            # Intermediate sweep while task is RUNNING: launches NOTHING
            idle_sweep = dispatcher.dispatch_sweep(runner_fn=mock_runner)
            assert len(idle_sweep["launched"]) == 0
            assert any(b["workspace_id"] == "ws_core" for b in idle_sweep["busy_workspaces"])

            # Simulate Agent workflow completion in engine
            save_workflow(
                {"workflow_id": current_task["workflow_id"], "title": current_task["title"], "status": "completed"},
                db_path=db_file,
            )

            # Auto-continuation sweep: in ONE automated cycle, it reconciles the completed task,
            # releases workspace lock, and automatically launches the next eligible task!
            sweep = dispatcher.dispatch_sweep(runner_fn=mock_runner)
            assert len(sweep["reconciled"]) == 1
            assert sweep["reconciled"][0]["id"] == current_task["id"]
            assert store.get_task(current_task["id"])["status"] == "COMPLETED"

            assert len(sweep["launched"]) == 1
            current_task = sweep["launched"][0]
            actual_executed_order.append(current_task["id"])

        # 3. Complete the final (10th) task
        save_workflow(
            {"workflow_id": current_task["workflow_id"], "title": current_task["title"], "status": "completed"},
            db_path=db_file,
        )
        final_sweep = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(final_sweep["reconciled"]) == 1
        assert final_sweep["reconciled"][0]["id"] == current_task["id"]
        assert len(final_sweep["launched"]) == 0
        assert store.get_task(current_task["id"])["status"] == "COMPLETED"

        # 4. Final verification of the entire 10-task pipeline
        assert actual_executed_order == expected_execution_order
        assert len(actual_executed_order) == 10
        assert len(workflow_ids_seen) == 10

        # Verify all tasks are completed in the store
        stats = store.get_inbox_stats()
        assert stats["total"] == 10
        assert stats["counts"]["COMPLETED"] == 10
        assert stats["counts"].get("PENDING", 0) == 0
        assert stats["counts"].get("RUNNING", 0) == 0
        assert stats["counts"].get("FAILED", 0) == 0

        # Workspace is completely free
        is_busy, _ = dispatcher.is_workspace_busy("ws_core")
        assert is_busy is False


def test_draft_never_dispatched_until_confirmed(tmp_path: Path):
    """Verify that unconfirmed tasks remain DRAFT and are NEVER claimed or executed."""
    db_file = tmp_path / "draft_isolation.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    mock_projects = {
        "projects": {
            "proj_main": {
                "project_id": "proj_main",
                "workspace_id": "ws_core",
                "root": str(tmp_path / "main_repo"),
            }
        }
    }

    with patch("herdr.inbox.dispatcher.load_projects", return_value=mock_projects):
        # 1. Create a draft task without confirmation
        draft_task = store.create_task(
            title="Draft Task",
            instruction="Some unconfirmed idea",
            project_id="proj_main",
            workspace_id="ws_core",
            priority="HIGH",
            auto_confirm=False,
        )
        assert draft_task["status"] == "DRAFT"

        # 2. Dispatcher sweep should NOT touch the draft
        sweep1 = dispatcher.dispatch_sweep()
        assert len(sweep1["launched"]) == 0
        assert store.get_task(draft_task["id"])["status"] == "DRAFT"

        # 3. Confirm scope
        confirmed = store.confirm_task_scope(draft_task["id"], "proj_main", "ws_core")
        assert confirmed["status"] == "PENDING"

        # 4. Next sweep claims and launches it
        def mock_runner(t):
            wf_id = f"wf_{t['id']}"
            save_workflow({"workflow_id": wf_id, "title": t["title"], "status": "running"}, db_path=db_file)
            return wf_id

        sweep2 = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep2["launched"]) == 1
        assert sweep2["launched"][0]["id"] == draft_task["id"]
        assert sweep2["launched"][0]["status"] == "RUNNING"


def test_multi_workspace_parallel_execution(tmp_path: Path):
    """Verify different workspaces execute tasks independently in parallel."""
    db_file = tmp_path / "multi_ws.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    mock_projects = {
        "projects": {
            "proj_a": {"project_id": "proj_a", "workspace_id": "ws_alpha", "root": "/a"},
            "proj_b": {"project_id": "proj_b", "workspace_id": "ws_beta", "root": "/b"},
        }
    }

    with patch("herdr.inbox.dispatcher.load_projects", return_value=mock_projects):
        # Submit 2 tasks to ws_alpha, 2 tasks to ws_beta
        t_a1 = store.create_task(title="A1", instruction="ins", project_id="proj_a", workspace_id="ws_alpha", auto_confirm=True)
        t_a2 = store.create_task(title="A2", instruction="ins", project_id="proj_a", workspace_id="ws_alpha", auto_confirm=True)
        t_b1 = store.create_task(title="B1", instruction="ins", project_id="proj_b", workspace_id="ws_beta", auto_confirm=True)
        t_b2 = store.create_task(title="B2", instruction="ins", project_id="proj_b", workspace_id="ws_beta", auto_confirm=True)

        def mock_runner(t):
            wf_id = f"wf_{t['id']}"
            save_workflow({"workflow_id": wf_id, "title": t["title"], "status": "running"}, db_path=db_file)
            return wf_id

        # First sweep: should launch A1 on ws_alpha AND B1 on ws_beta concurrently!
        sweep = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep["launched"]) == 2
        launched_ids = {t["id"] for t in sweep["launched"]}
        assert launched_ids == {t_a1["id"], t_b1["id"]}

        # Both workspaces are busy, but each with only 1 task
        busy_a, _ = dispatcher.is_workspace_busy("ws_alpha")
        busy_b, _ = dispatcher.is_workspace_busy("ws_beta")
        assert busy_a is True
        assert busy_b is True

        # Complete A1 only
        save_workflow({"workflow_id": f"wf_{t_a1['id']}", "title": "A1", "status": "completed"}, db_path=db_file)

        # Next sweep: ws_alpha reconciles A1 and starts A2! ws_beta is still busy with B1!
        sweep2 = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep2["reconciled"]) == 1
        assert sweep2["reconciled"][0]["id"] == t_a1["id"]
        assert len(sweep2["launched"]) == 1
        assert sweep2["launched"][0]["id"] == t_a2["id"]


def test_worker_crash_and_retry_recovery(tmp_path: Path):
    """Verify that when a workflow fails or crashes, the task marks FAILED,

    releases the workspace lock, allows other tasks to proceed, and can be retried.
    """
    db_file = tmp_path / "crash_recovery.db"
    store = InboxStore(db_file)
    dispatcher = InboxDispatcher(store=store, db_path=db_file)

    mock_projects = {
        "projects": {
            "proj_main": {"project_id": "proj_main", "workspace_id": "ws_core", "root": "/main"}
        }
    }

    with patch("herdr.inbox.dispatcher.load_projects", return_value=mock_projects):
        t1 = store.create_task(title="Flaky Task", instruction="ins", project_id="proj_main", workspace_id="ws_core", auto_confirm=True)
        t2 = store.create_task(title="Subsequent Task", instruction="ins", project_id="proj_main", workspace_id="ws_core", auto_confirm=True)

        def mock_runner(t):
            wf_id = f"wf_{t['id']}"
            save_workflow({"workflow_id": wf_id, "title": t["title"], "status": "running"}, db_path=db_file)
            return wf_id

        # Launch T1
        dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert store.get_task(t1["id"])["status"] == "RUNNING"

        # Simulate Worker crash: workflow ends in "failed"
        save_workflow(
            {"workflow_id": f"wf_{t1['id']}", "title": "Flaky Task", "status": "failed", "error": "Agent syntax error"},
            db_path=db_file,
        )

        # Dispatcher sweep reconciles failure
        sweep = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep["reconciled"]) == 1
        assert sweep["reconciled"][0]["id"] == t1["id"]

        failed_t1 = store.get_task(t1["id"])
        assert failed_t1["status"] == "FAILED"
        assert "ended with status 'failed'" in failed_t1["error"]

        # Workspace is immediately unlocked and T2 is launched!
        assert len(sweep["launched"]) == 1
        assert sweep["launched"][0]["id"] == t2["id"]
        assert store.get_task(t2["id"])["status"] == "RUNNING"

        # Complete T2
        save_workflow({"workflow_id": f"wf_{t2['id']}", "title": "Subsequent Task", "status": "completed"}, db_path=db_file)
        dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert store.get_task(t2["id"])["status"] == "COMPLETED"

        # Now User retries T1
        retried = store.retry_task(t1["id"])
        assert retried["status"] == "PENDING"
        assert retried["error"] is None

        # Next sweep launches retried T1
        sweep_retry = dispatcher.dispatch_sweep(runner_fn=mock_runner)
        assert len(sweep_retry["launched"]) == 1
        assert sweep_retry["launched"][0]["id"] == t1["id"]
        assert store.get_task(t1["id"])["status"] == "RUNNING"


def test_idempotent_task_submission(tmp_path: Path):
    """Verify that submitting identical idempotency keys returns existing task."""
    db_file = tmp_path / "idempotent.db"
    store = InboxStore(db_file)

    t1 = store.create_task(
        title="Unique task",
        instruction="Do something once",
        project_id="proj_1",
        workspace_id="ws_1",
        idempotency_key="unique_external_key_123",
        auto_confirm=True,
    )

    t2 = store.create_task(
        title="Unique task duplicate",
        instruction="Do something once again",
        project_id="proj_1",
        workspace_id="ws_1",
        idempotency_key="unique_external_key_123",
        auto_confirm=True,
    )

    assert t1["id"] == t2["id"]
    stats = store.get_inbox_stats()
    assert stats["total"] == 1
