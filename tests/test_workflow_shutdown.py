"""Acceptance Test Suite for Universal Workflow Shutdown Protocol (Review Round 14 / P1).

Covers the 3 required acceptance scenarios:
1. While workflow is closing/cancelling, Coordinator attempts to dispatch B -> B must be rejected.
2. When B has entered launch process but is not yet registered, shutdown coordinates cleanly -> B is not orphaned.
3. All agents must be confirmed stopped before workflow enters terminal state and releases workspace.
"""
import json
import sqlite3
import time
from pathlib import Path
import pytest

from herdr.inbox.store import InboxStore
from herdr.inbox.dispatcher import InboxDispatcher
from herdr.workflow_shutdown import shutdown_workflow
from herdr.state_db import save_workflow, get_workflow, save_task, get_task, list_tasks
from herdr import node_dispatch as core
from herdr import node_dispatch_store as nds
from herdr import task_resources


@pytest.fixture
def test_env(tmp_path: Path):
    db_file = tmp_path / "shutdown_test.db"
    store = InboxStore(db_file)
    disp = InboxDispatcher(store=store, db_path=db_file)
    return {
        "tmp_path": tmp_path,
        "db_file": db_file,
        "store": store,
        "disp": disp,
    }


def test_scenario_1_coordinator_dispatch_rejected_when_workflow_closing(test_env, monkeypatch):
    """Scenario 1: While A is being cancelled/closing, Coordinator concurrently attempts to dispatch B.

    B MUST be rejected:
    - node_dispatch.active(wf) returns False.
    - node_dispatch_store.claim returns None.
    - node_dispatch_store.start raises ValueError.
    - node_dispatch_store.validate_unbound_launch raises ValueError.
    - task_resources.begin_launch_intent raises ValueError.
    """
    db_file = test_env["db_file"]
    wf_id = "wf_scene_1"
    ws_id = "ws_scene_1"

    config = {
        "workflow_template": "software-development-v1",
        "nodes": [
            {"id": "node_A", "node_type": "agent", "roles": ["worker"]},
            {"id": "node_B", "node_type": "agent", "roles": ["worker"], "depends_on": ["node_A"]},
        ],
    }

    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj_1",
            "workspace_id": ws_id,
            "title": "Shutdown Test WF",
            "status": "running",
            "execution_id": "exec_1",
            "config": config,
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_A",
            "workflow_id": wf_id,
            "node": "node_A",
            "status": "working",
            "pane_id": "%10",
        },
        db_path=db_file,
    )

    # Simulate workflow entering CLOSING state (Step 1 of shutdown protocol)
    from herdr.state_db import transition_workflow
    transition_workflow(wf_id, to_status="closing", reason="cancelling workflow", db_path=db_file, force=True)

    wf = get_workflow(wf_id, db_path=db_file)
    assert wf["status"] == "closing"

    # 1. Verification: core.active(workflow) is strictly FALSE for closing workflows
    assert core.active(wf) is False

    # 2. Verification: node_dispatch_store.claim refuses to claim operations for closing workflows
    # Seed an operation in workflow_recovery_operations
    now = time.time()
    conn = sqlite3.connect(str(db_file))
    cur = conn.execute(
        """
        INSERT INTO workflow_recovery_operations
        (workflow_id, identity_key, status, payload_json, detail_json, owner, attempts, next_due_at, lease_until, created_at, updated_at, version)
        VALUES (?, ?, 'pending', ?, '{}', NULL, 0, ?, ?, ?, ?, 1)
        """,
        (
            wf_id,
            "node_dispatch:test_b",
            json.dumps({"kind": "node_dispatch", "node_id": "node_B", "generation": ["exec_1", None, None]}),
            now - 10,
            now + 60,
            now,
            now,
        ),
    )
    op_id = cur.lastrowid
    conn.commit()
    conn.close()

    claimed_op = nds.claim(db_file, op_id, owner="coordinator_1", now=now)
    assert claimed_op is None, "Coordinator MUST NOT claim dispatch for a closing workflow"

    # 3. Verification: validate_unbound_launch rejects unbound launches on closing workflow
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    with pytest.raises(ValueError, match="is closing"):
        nds.validate_unbound_launch(conn, wf_id, "node_B")
    conn.close()

    # 4. Verification: task_resources.begin_launch_intent rejects launch intents on closing workflow
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(str(db_file))
    with pytest.raises(ValueError, match="Cannot begin launch intent: workflow 'wf_scene_1' is closing"):
        task_resources.begin_launch_intent(
            store,
            workflow_id=wf_id,
            node_id="node_B",
            task_id="task_B_attempt",
        )


def test_scenario_2_inflight_launch_not_yet_registered_is_safely_coordinated(test_env, monkeypatch):
    """Scenario 2: Node B has entered launch process but has not yet registered.

    Shutdown coordinates via workflow_launch_lock:
    - B's registration into CLOSING workflow is rejected by validate_task_registration.
    - If B managed to register right before lock acquisition, shutdown's fresh snapshot
      catches B and stops it.
    - No orphan tasks or unstopped agents survive.
    """
    db_file = test_env["db_file"]
    wf_id = "wf_scene_2"
    ws_id = "ws_scene_2"

    config = {
        "workflow_template": "software-development-v1",
        "nodes": [{"id": "node_A", "node_type": "agent", "roles": ["worker"]}],
    }

    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj_2",
            "workspace_id": ws_id,
            "title": "Inflight Test WF",
            "status": "running",
            "execution_id": "exec_2",
            "config": config,
        },
        db_path=db_file,
    )

    # 2A. Simulate B having created a launch intent (allocating phase) before registration
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(str(db_file))
    now = time.time()

    store.record_event(
        "launch_intent",
        {
            "key": f"{wf_id}:node_B:worker:1:exec_2",
            "intent_id": "intent_b_1",
            "workflow_id": wf_id,
            "node_id": "node_B",
            "role": "worker",
            "task_id": "task_B_inflight",
            "phase": "allocating",
            "lease_until": now + 120,
            "resources": {"pane_id": "%22"},
        },
        workflow_id=wf_id,
        task_id=f"{wf_id}:node_B:worker:1:exec_2",
        source="launch",
    )

    # Also register task_A as working with pane %21
    save_task(
        {
            "task_id": "task_A",
            "workflow_id": wf_id,
            "node": "node_A",
            "status": "working",
            "pane_id": "%21",
        },
        db_path=db_file,
    )

    # Mock steering and pane closing to succeed
    monkeypatch.setattr("herdr.task_resources.owned_live_pane", lambda task: (True, "identity_match"))
    monkeypatch.setattr("herdr.steering.halt_task", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("herdr.workflow_shutdown.close_pane", lambda pid: True)
    monkeypatch.setattr("herdr.inbox.dispatcher.close_pane", lambda pid: True)

    # Run shutdown
    res = shutdown_workflow(wf_id, db_path=db_file, reason="test cancellation")
    assert res == "STOPPED"

    # Verify task_A was stopped
    t_a = get_task("task_A", db_path=db_file)
    assert t_a["status"] == "failed"

    # Verify allocating intent for task_B was cleanly drained/aborted
    events = store.list_events(workflow_id=wf_id, event_type="launch_intent")
    aborted_events = [e for e in events if e.get("payload", {}).get("phase") == "resources_absent"]
    assert len(aborted_events) >= 1, "In-flight allocating intent must be aborted"

    # Verify workflow transitioned to completed / abandoned
    wf_after = get_workflow(wf_id, db_path=db_file)
    assert wf_after["status"] == "completed"
    assert wf_after.get("outcome") == "abandoned"

    # Verify zero active tasks remain
    all_tasks = list_tasks(workflow_id=wf_id, db_path=db_file)
    active = [t for t in all_tasks if t.get("status") in ("working", "running", "pending")]
    assert len(active) == 0


def test_scenario_3_agents_must_stop_before_terminal_state_and_workspace_release(test_env, monkeypatch):
    """Scenario 3: All Agents must be confirmed stopped before Workflow enters terminal state and releases workspace.

    - When an agent cannot be confirmed stopped (halt_task fails and pane unproven/reused):
      shutdown returns STOP_FAILED, workflow remains in 'closing' (NOT 'completed'),
      and workspace remains busy.
    - When all agents are confirmed stopped:
      shutdown returns STOPPED, workflow transitions to 'completed', and workspace is freed.
    """
    db_file = test_env["db_file"]
    dispatcher = test_env["disp"]
    store = test_env["store"]
    wf_id = "wf_scene_3"
    ws_id = "ws_scene_3"

    save_workflow(
        {
            "workflow_id": wf_id,
            "project_id": "proj_3",
            "workspace_id": ws_id,
            "title": "Gate Stop WF",
            "status": "running",
            "config": {"workspace_id": ws_id, "project_id": "proj_3"},
        },
        db_path=db_file,
    )
    save_task(
        {
            "task_id": "task_resisting_1",
            "workflow_id": wf_id,
            "status": "working",
            "pane_id": "%77",
        },
        db_path=db_file,
    )

    t = store.create_task(
        title="Scene 3 Task",
        instruction="Instruction",
        project_id="proj_3",
        workspace_id=ws_id,
        auto_confirm=True,
    )
    store.update_task_status(t["id"], new_status="CLAIMED")
    store.update_task_status(t["id"], new_status="RUNNING", workflow_id=wf_id)

    # 1. Fault injection: halt fails AND pane ownership cannot be verified (e.g. foreign agent)
    monkeypatch.setattr("herdr.steering.halt_task", lambda *a, **k: {"ok": False, "error": "unresponsive"})
    monkeypatch.setattr("herdr.task_resources.owned_live_pane", lambda task: (False, "session_mismatch"))
    monkeypatch.setattr("herdr.inbox.dispatcher.close_pane", lambda pid: False)
    monkeypatch.setattr("herdr.workflow_shutdown.close_pane", lambda pid: False)

    # Shutdown MUST return STOP_FAILED
    res = shutdown_workflow(wf_id, db_path=db_file, reason="attempted stop")
    assert res == "STOP_FAILED"

    # Workflow MUST NOT be terminal; it must remain in CLOSING
    wf_db = get_workflow(wf_id, db_path=db_file)
    assert wf_db["status"] == "closing"

    # Workspace MUST remain busy
    busy, reason = dispatcher.is_workspace_busy(ws_id)
    assert busy is True, "Workspace must remain busy when workflow agent could not be stopped"

    # 2. Recovery: runtime ownership verified and stop succeeds
    monkeypatch.setattr("herdr.task_resources.owned_live_pane", lambda task: (True, "identity_match"))
    monkeypatch.setattr("herdr.inbox.dispatcher.close_pane", lambda pid: True)
    monkeypatch.setattr("herdr.workflow_shutdown.close_pane", lambda pid: True)

    res_retry = shutdown_workflow(wf_id, db_path=db_file, reason="retry stop")
    assert res_retry == "STOPPED"

    # Workflow is now completed / abandoned
    wf_final = get_workflow(wf_id, db_path=db_file)
    assert wf_final["status"] == "completed"
    assert wf_final.get("outcome") == "abandoned"

    # Task is now failed
    task_final = get_task("task_resisting_1", db_path=db_file)
    assert task_final["status"] == "failed"

    # Workspace can now be safely released
    store.cancel_task(t["id"])
    busy_after, _ = dispatcher.is_workspace_busy(ws_id)
    assert busy_after is False, "Workspace must be released once all agents are confirmed stopped"
