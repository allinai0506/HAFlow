#!/usr/bin/env python3
"""Tests for Herdr Kernel Control Primitives (Phase 1).

Covers:
- pause_workflow / resume_workflow (global and node-level)
- force_pass_gate (gate verdict override and audit note)
- rollback_workflow (topological downstream invalidation and state reset)
- step_workflow (single-step ready node triggering while workflow is paused)
- checkpoint primitives (create, list, get, restore snapshot)
"""

import json
import os
import shutil
import pytest
from pathlib import Path

from herdr import kernel


@pytest.fixture
def kernel_env(tmp_path, monkeypatch):
    """Set up an isolated environment for kernel tests."""
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    s_file = tmp_path / "stage-state.json"
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("STAGE_STATE_FILE", str(s_file))
    monkeypatch.setenv("CHECKPOINTS_DIR", str(cp_dir))

    # Initialize empty workflows, tasks, and stage-state files
    wf_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    s_file.write_text(json.dumps({}), encoding="utf-8")

    return {
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "s_file": s_file,
        "cp_dir": cp_dir,
    }


def _seed_workflow(env, workflow_id, status="running", nodes=None):
    """Helper to seed workflow entry in workflows.json."""
    if nodes is None:
        nodes = [
            {"id": "req", "label": "需求分析", "depends_on": []},
            {"id": "impl", "label": "代码开发", "depends_on": ["req"]},
            {"id": "test", "label": "质量验收", "depends_on": ["impl"], "gate": {"type": "auto"}},
            {"id": "deploy", "label": "交付部署", "depends_on": ["test"]},
        ]
    data = json.loads(env["wf_file"].read_text(encoding="utf-8"))
    data["workflows"][workflow_id] = {
        "workflow_id": workflow_id,
        "status": status,
        "config": {"nodes": nodes},
        "coordinator_pane_id": "pane_coord_1",
    }
    env["wf_file"].write_text(json.dumps(data, indent=2), encoding="utf-8")


def _seed_tasks(env, tasks):
    """Helper to seed tasks in tasks.json."""
    data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    data["tasks"].extend(tasks)
    env["tasks_file"].write_text(json.dumps(data, indent=2), encoding="utf-8")


# ============================================================
# 1. Pause & Resume Tests
# ============================================================

def test_pause_and_resume_workflow(kernel_env):
    wid = "wf_test_pause"
    _seed_workflow(kernel_env, wid, status="running")

    # 1. Pause
    res = kernel.pause_workflow(wid)
    assert res["ok"] is True
    assert res["status"] == "paused"

    wf_data = json.loads(kernel_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
    assert wf_data["status"] == "paused"

    # 2. Resume
    res = kernel.resume_workflow(wid)
    assert res["ok"] is True
    assert res["status"] == "running"

    wf_data = json.loads(kernel_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
    assert wf_data["status"] == "running"


def test_resume_checks_occupancy_in_explicit_database(tmp_path, monkeypatch):
    from herdr.state_store import SQLiteStateStore

    default = SQLiteStateStore(tmp_path / "default.db")
    selected = SQLiteStateStore(tmp_path / "selected.db")
    monkeypatch.setenv("HERDR_STATE_DB", str(default.db_path))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    monkeypatch.setenv("HERDR_SKIP_LEGACY_BOOTSTRAP_DB", "1")

    def save(store, workflow_id, status):
        store.save_workflow({"workflow_id": workflow_id, "project_id": "project", "status": status})

    save(default, "elsewhere", "completed")
    save(selected, "paused", "paused")
    save(selected, "busy", "running")

    with pytest.raises(RuntimeError, match="occupied.*busy"):
        kernel.resume_workflow("paused", store=selected)
    assert selected.get_workflow("paused")["status"] == "paused"

    save(selected, "busy", "completed")
    save(default, "elsewhere", "running")
    assert kernel.resume_workflow("paused", store=selected)["status"] == "running"
    assert default.get_workflow("elsewhere")["status"] == "running"
    assert default.get_workflow("paused") is None


def test_pause_node_level(kernel_env):
    wid = "wf_test_pause_node"
    _seed_workflow(kernel_env, wid, status="running")

    res = kernel.pause_workflow(wid, node_id="impl")
    assert res["ok"] is True
    assert "impl" in res.get("paused_nodes", [])

    wf_data = json.loads(kernel_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
    assert "impl" in wf_data.get("paused_nodes", [])

    # Resume node
    res = kernel.resume_workflow(wid, node_id="impl")
    assert res["ok"] is True
    assert "impl" not in res.get("paused_nodes", [])


# ============================================================
# 2. Force Pass Gate Tests
# ============================================================

def test_force_pass_gate(kernel_env):
    wid = "wf_test_force_pass"
    _seed_workflow(kernel_env, wid)
    _seed_tasks(
        kernel_env,
        [
            {
                "task_id": "task_test_1",
                "workflow_id": wid,
                "node": "test",
                "status": "completed",
                "stage_verdict": "blocked",
                "stage_verdict_note": "Lint failed: unused import",
            }
        ],
    )

    res = kernel.force_pass_gate(
        wid,
        gate_node_id="test",
        note="Manual override: lint issue is cosmetic and approved",
        operator="lead_architect",
    )

    assert res["ok"] is True
    assert res["gate_node_id"] == "test"
    assert "task_test_1" in res["updated_tasks"]

    # Verify task updated in tasks.json
    tasks = json.loads(kernel_env["tasks_file"].read_text(encoding="utf-8"))["tasks"]
    task = next(t for t in tasks if t["task_id"] == "task_test_1")
    assert task["stage_verdict"] == "pass"
    assert "[FORCE PASS by lead_architect]" in task["stage_verdict_note"]


# ============================================================
# 3. Rollback Tests
# ============================================================

def test_rollback_workflow_downstream_invalidation(kernel_env):
    wid = "wf_test_rollback"
    _seed_workflow(kernel_env, wid)
    _seed_tasks(
        kernel_env,
        [
            {"task_id": "t_req", "workflow_id": wid, "node": "req", "status": "completed"},
            {"task_id": "t_impl", "workflow_id": wid, "node": "impl", "status": "completed"},
            {"task_id": "t_test", "workflow_id": wid, "node": "test", "status": "active", "stage_verdict": "blocked"},
            {"task_id": "t_deploy", "workflow_id": wid, "node": "deploy", "status": "pending"},
        ],
    )

    # Seed stage-state.json with advance locks for req and impl
    kernel_env["s_file"].write_text(json.dumps({
        f"{wid}:req": "notified",
        f"{wid}:impl": "notified",
        f"{wid}:test": "notified",
    }), encoding="utf-8")

    # Roll back to "impl"
    # Target "impl" and downstream "test", "deploy" should be invalidated
    # "req" should remain unaffected!
    res = kernel.rollback_workflow(wid, target_node_id="impl", reason="Refactor needed")

    assert res["ok"] is True
    assert set(res["affected_nodes"]) == {"impl", "test", "deploy"}
    assert "t_impl" in res["invalidated_tasks"]
    assert "t_test" in res["invalidated_tasks"]
    assert "t_deploy" in res["invalidated_tasks"]
    assert "t_req" not in res["invalidated_tasks"]

    tasks = {t["task_id"]: t for t in json.loads(kernel_env["tasks_file"].read_text(encoding="utf-8"))["tasks"]}
    assert tasks["t_req"]["status"] == "completed"
    assert tasks["t_impl"]["status"] == "superseded"
    assert tasks["t_test"]["status"] == "superseded"
    assert tasks["t_deploy"]["status"] == "superseded"

    # Verify stage-state.json: affected nodes cleared, unaffected kept!
    s_data = json.loads(kernel_env["s_file"].read_text(encoding="utf-8"))
    assert f"{wid}:req" in s_data
    assert f"{wid}:impl" not in s_data
    assert f"{wid}:test" not in s_data


# ============================================================
# 4. Step Workflow Tests
# ============================================================

def test_step_workflow_triggers_single_ready_node(kernel_env):
    wid = "wf_test_step"
    _seed_workflow(kernel_env, wid, status="paused")
    # req is completed, impl is ready, test and deploy are waiting
    _seed_tasks(
        kernel_env,
        [
            {"task_id": "t_req", "workflow_id": wid, "node": "req", "status": "completed"},
        ],
    )

    res = kernel.step_workflow(wid)
    assert res["ok"] is True
    assert res["stepped_node"] == "impl"
    # Workflow must remain paused in single-step mode!
    wf_data = json.loads(kernel_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
    assert wf_data["status"] == "paused"


def test_step_workflow_no_ready_nodes(kernel_env):
    wid = "wf_test_step_none"
    _seed_workflow(kernel_env, wid, status="paused")
    # All nodes completed
    _seed_tasks(
        kernel_env,
        [
            {"task_id": "t_req", "workflow_id": wid, "node": "req", "status": "completed"},
            {"task_id": "t_impl", "workflow_id": wid, "node": "impl", "status": "completed"},
            {"task_id": "t_test", "workflow_id": wid, "node": "test", "status": "completed"},
            {"task_id": "t_deploy", "workflow_id": wid, "node": "deploy", "status": "completed"},
        ],
    )

    res = kernel.step_workflow(wid)
    assert res["ok"] is False
    assert res["reason"] == "no_ready_nodes"


# ============================================================
# 5. Checkpoint Snapshot Tests
# ============================================================

def test_checkpoint_save_list_and_restore(kernel_env):
    wid = "wf_test_cp"
    _seed_workflow(kernel_env, wid, status="running")
    _seed_tasks(
        kernel_env,
        [
            {"task_id": "t_req", "workflow_id": wid, "node": "req", "status": "completed"},
            {"task_id": "t_impl", "workflow_id": wid, "node": "impl", "status": "active"},
        ],
    )

    # 1. Create checkpoint
    cp = kernel.create_checkpoint(wid, tag="before_risky_step")
    assert cp["ok"] is True
    cp_id = cp["checkpoint_id"]
    assert cp["tag"] == "before_risky_step"

    # 2. List checkpoints
    cps = kernel.list_checkpoints(wid)
    assert len(cps) == 1
    assert cps[0]["checkpoint_id"] == cp_id
    assert cps[0]["tag"] == "before_risky_step"

    # 3. Modify current state (simulate corruption or bad step)
    kernel.pause_workflow(wid)
    _seed_tasks(
        kernel_env,
        [
            {"task_id": "t_corrupt", "workflow_id": wid, "node": "impl", "status": "failed"},
        ],
    )

    # 4. Restore checkpoint
    restored = kernel.restore_checkpoint(wid, cp_id)
    assert restored["ok"] is True
    assert restored["checkpoint_id"] == cp_id

    # Verify workflow restored to "running" and tasks restored
    wf_data = json.loads(kernel_env["wf_file"].read_text(encoding="utf-8"))["workflows"][wid]
    assert wf_data["status"] == "running"
    tasks = json.loads(kernel_env["tasks_file"].read_text(encoding="utf-8"))["tasks"]
    task_ids = [t["task_id"] for t in tasks if t.get("workflow_id") == wid]
    assert set(task_ids) == {"t_req", "t_impl"}
    assert "t_corrupt" not in task_ids
