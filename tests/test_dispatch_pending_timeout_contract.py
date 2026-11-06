"""Anti-regression contract tests for dispatch timeout, pending stall, and false resolution.

Verifies:
1. node_dispatch.result() NEVER resolves an operation if registered tasks are still 'pending'.
2. direct_dispatch timeout on launch records attention and avoids marking operation resolved.
3. Sentinel detects and alerts on stranded tasks stuck in 'pending' status past SLA.
"""

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from herdr import node_dispatch as core
from herdr import node_dispatch_store as nd
from herdr import recovery_store
from herdr.state_store import get_state_store, reset_state_store
from herdr.transitions import ACTIVE_TASK_STATUSES, COMPLETED_TASK_STATUSES

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    (tmp_path / ".herdr-controller").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    reset_state_store()
    store = get_state_store(db_path)
    yield store, db_path
    reset_state_store()


def test_pending_task_cannot_resolve_node_dispatch_operation(test_db):
    """A task that is only in 'pending' status has not completed dispatch;

    result() must not return 'resolved'.
    """
    store, db_path = test_db
    workflow = {
        "workflow_id": "wf-test",
        "execution_id": "exec-1",
        "created_at": 1000.0,
        "status": "running",
        "startup_ready": True,
        "config": {
            "nodes": [
                {
                    "id": "requirements",
                    "purpose": "需求分析与技术方案编制",
                    "default_task_type": "requirements",
                    "default_integration_mode": "none",
                    "depends_on": [],
                }
            ]
        },
    }
    store.save_workflow(workflow)
    config = workflow["config"]
    node = config["nodes"][0]

    payload = core.payload(workflow, config, node)
    op = {
        "id": 101,
        "workflow_id": "wf-test",
        "status": "running",
        "started": 1,
        "identity_key": payload["identity_key"],
        "payload": payload,
        "detail": {
            "expected_task_ids": ["task-req-1"],
            "deadline_at": 2000.0,
        },
    }

    pending_task = {
        "task_id": "task-req-1",
        "workflow_id": "wf-test",
        "node": "requirements",
        "status": "pending",
        "execution_id": "exec-1",
        "run_id": "run-req-1",
        "launch_intent_id": "intent-1",
        "dispatch_operation_id": 101,
    }

    # Before deadline: must return None (awaiting result / still in flight), NOT 'resolved'
    res = core.result(op, workflow, config, [pending_task], now=1500.0)
    assert res is None, f"Expected None for pending task before deadline, got {res}"

    # At or after deadline: must return 'waiting_human', NOT 'resolved'
    res_timeout = core.result(op, workflow, config, [pending_task], now=2100.0)
    assert res_timeout is not None
    assert res_timeout[0] == "waiting_human"
    assert res_timeout[1].get("reason") in {"dispatch_task_missing", "dispatch_task_pending_timeout"}

    # Once task reaches 'dispatched' (or 'working'), result() must resolve
    dispatched_task = dict(pending_task, status="dispatched")
    res_dispatched = core.result(op, workflow, config, [dispatched_task], now=1500.0)
    assert res_dispatched is not None
    assert res_dispatched[0] == "resolved"
    assert res_dispatched[1]["registered_task_ids"] == ["task-req-1"]

    # Even if task failed after launch, dispatch evidence is preserved so self-healing can supersede
    failed_task = dict(pending_task, status="failed")
    res_failed = core.result(op, workflow, config, [failed_task], now=1500.0)
    assert res_failed is not None
    assert res_failed[0] == "resolved"
    assert res_failed[1]["registered_task_ids"] == ["task-req-1"]


def test_replacement_complete_requires_non_pending_successor(test_db):
    """Replacement task in 'pending' status cannot satisfy replacement_complete."""
    store, db_path = test_db
    workflow = {
        "workflow_id": "wf-replace",
        "execution_id": "exec-1",
        "created_at": 1000.0,
        "status": "running",
        "startup_ready": True,
        "config": {"nodes": [{"id": "implementation", "depends_on": []}]},
    }
    store.save_workflow(workflow)
    config = workflow["config"]
    node = config["nodes"][0]

    predecessors = [{"task_id": "task-impl-1", "run_id": "run-impl-1"}]
    payload = core.payload(workflow, config, node, predecessors=predecessors)
    op = {
        "id": 102,
        "workflow_id": "wf-replace",
        "status": "running",
        "started": 1,
        "identity_key": payload["identity_key"],
        "payload": payload,
        "detail": {
            "expected_task_ids": ["task-impl-1-r2"],
            "deadline_at": 2000.0,
        },
    }

    pred_task = {
        "task_id": "task-impl-1",
        "workflow_id": "wf-replace",
        "node": "implementation",
        "status": "superseded",
        "replacement_pending": True,
        "execution_id": "exec-1",
        "run_id": "run-impl-1",
    }
    successor_pending = {
        "task_id": "task-impl-1-r2",
        "workflow_id": "wf-replace",
        "node": "implementation",
        "status": "pending",
        "supersedes": "task-impl-1",
        "execution_id": "exec-1",
        "run_id": "run-impl-1-r2",
        "launch_intent_id": "intent-r2",
        "dispatch_operation_id": 102,
    }

    tasks = [pred_task, successor_pending]
    res = core.result(op, workflow, config, tasks, now=1500.0)
    assert res is None, f"Expected None while replacement is pending, got {res}"

    successor_active = dict(successor_pending, status="working")
    tasks_active = [pred_task, successor_active]
    res_active = core.result(op, workflow, config, tasks_active, now=1500.0)
    assert res_active is not None
    assert res_active[0] == "resolved"


def test_direct_dispatch_timeout_writes_attention_and_does_not_falsely_resolve(test_db):
    """When herdr-task launch times out, direct_stage_advance must record an attention note

    and must not report or mark the durable operation as resolved.
    """
    store, db_path = test_db
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ctrl_test_mod", ROOT / "services" / "herdr-controller.py"
    )
    ctrl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctrl)

    workflow = {
        "workflow_id": "wf-timeout",
        "execution_id": "exec-timeout-1",
        "created_at": 1000.0,
        "status": "running",
        "startup_ready": True,
        "requirement": "requirements analysis",
        "base_branch": "main",
        "project_root": str(ROOT),
        "config": {
            "nodes": [
                {
                    "id": "requirements",
                    "purpose": "需求分析与技术方案编制",
                    "default_task_type": "requirements",
                    "default_integration_mode": "none",
                    "depends_on": [],
                }
            ]
        },
    }
    store.save_workflow(workflow)

    now = time.time()
    # Reconcile operation into DB
    nd.reconcile_workflow(db_path, "wf-timeout", now=now)
    ops = [
        o for o in recovery_store.list_operations(db_path, "wf-timeout")
        if o["payload"].get("kind") == "node_dispatch"
    ]
    assert len(ops) == 1
    op = ops[0]
    claimed = nd.claim(db_path, op["id"], "owner-timeout", now=now)
    assert claimed is not None

    attention_calls = []

    def mock_attention_note(*args, **kwargs):
        attention_calls.append((args, kwargs))

    item = {
        "workflow_id": "wf-timeout",
        "node_id": "requirements",
        "dispatch_operation_id": op["id"],
        "dispatch_owner": "owner-timeout",
        "node": workflow["config"]["nodes"][0],
    }

    from herdr.task_resources import begin_launch_intent

    # Simulate launch creating the task row in pending, but timing out before prompt delivery
    def mock_subprocess_run(cmd, **kwargs):
        if len(cmd) > 1 and cmd[1] == "launch":
            actual_task_id = cmd[cmd.index("--task-id") + 1]
            intent = begin_launch_intent(
                store,
                workflow_id="wf-timeout",
                node_id="requirements",
                task_id=actual_task_id,
                role="worker",
                dispatch_operation_id=op["id"],
                run_id="run-req-1",
                execution_id="exec-timeout-1",
                now=now,
            )["intent"]
            # Simulate launch persisting the pending task into DB
            store.save_task({
                "task_id": actual_task_id,
                "workflow_id": "wf-timeout",
                "node": "requirements",
                "status": "pending",
                "execution_id": "exec-timeout-1",
                "run_id": "run-req-1",
                "launch_intent_id": intent["intent_id"],
                "dispatch_operation_id": op["id"],
            })
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout", 600))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(ctrl.subprocess, "run", side_effect=mock_subprocess_run), \
         patch.object(ctrl, "attention_note", side_effect=mock_attention_note), \
         patch.object(ctrl, "attention_get", return_value=None), \
         patch.object(ctrl, "_get_store", return_value=store), \
         patch.object(ctrl, "project_for_workflow", return_value=workflow), \
         patch.object(ctrl, "workflow_config_for", return_value=workflow["config"]), \
         patch.object(ctrl, "load_tasks", side_effect=store.list_tasks):

        result = ctrl.try_direct_stage_advance(item)

    assert result is True

    # 1. Attention note MUST be recorded
    assert len(attention_calls) > 0, "Expected attention_note to be called on launch timeout"
    recorded_reasons = [
        kw.get("reason") or (args[2] if len(args) > 2 else None)
        for args, kw in attention_calls
    ]
    assert any(
        r in {"dispatch_launch_timeout", "direct_dispatch_delivery_unknown"}
        for r in recorded_reasons
    ), f"Expected dispatch_launch_timeout, got {recorded_reasons}"

    # 2. Durable operation MUST NOT be resolved!
    ops_after = recovery_store.list_operations(db_path, "wf-timeout")
    op_after = next(o for o in ops_after if o["id"] == op["id"])
    assert op_after["status"] != "resolved", (
        f"Operation was falsely resolved despite launch timeout! Status: {op_after['status']}"
    )


def test_sentinel_detects_stranded_pending_task(test_db):
    """Sentinel must detect tasks stuck in 'pending' beyond SLA (e.g. 180s) and alert."""
    store, db_path = test_db
    from herdr import liveness

    now = 2000.0
    task_stranded = {
        "task_id": "task-stuck-1",
        "workflow_id": "wf-stuck",
        "node": "requirements",
        "status": "pending",
        "updated_at": now - 300.0,  # 300s in pending (> 180s SLA)
        "created_at": now - 300.0,
    }
    task_fresh = {
        "task_id": "task-fresh-1",
        "workflow_id": "wf-stuck",
        "node": "requirements",
        "status": "pending",
        "updated_at": now - 30.0,  # 30s in pending (< 180s SLA)
        "created_at": now - 30.0,
    }

    # Evaluate pending stalls
    assert hasattr(liveness, "evaluate_pending_task_stalls"), (
        "liveness module should provide evaluate_pending_task_stalls"
    )
    breaches, _ = liveness.evaluate_pending_task_stalls([task_stranded, task_fresh], {}, now=now, sla=180.0)
    assert len(breaches) == 1
    assert breaches[0]["task_id"] == "task-stuck-1"
    assert breaches[0]["waited_seconds"] == 300

    # Sentinel integration check
    import importlib.util
    sentinel_spec = importlib.util.spec_from_file_location(
        "sentinel_test_mod", ROOT / "services" / "herdr-sentinel.py"
    )
    sentinel_mod = importlib.util.module_from_spec(sentinel_spec)
    sentinel_spec.loader.exec_module(sentinel_mod)

    notifications = []
    state = {}
    changed = sentinel_mod.check_pending_tasks(
        [task_stranded, task_fresh],
        state,
        notify_fn=lambda tid, wid, body: notifications.append((tid, wid, body)),
        now=now,
    )
    assert changed is True
    assert len(notifications) == 1
    assert notifications[0][0] == "task-stuck-1"
    assert "pending" in notifications[0][2]
    assert state["pending_stalls"]["task-stuck-1"]["waited_seconds"] == 300
