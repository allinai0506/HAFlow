#!/usr/bin/env python3
"""Tests for Herdr Factory Console Kernel Control Endpoints (Phase 1).

Covers:
- POST /api/kernel/pause
- POST /api/kernel/resume
- POST /api/kernel/step
- POST /api/kernel/rollback
- POST /api/kernel/force-pass
- POST /api/kernel/checkpoint
- GET  /api/kernel/checkpoints
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch

from console import herdr_factory_console as c


@pytest.fixture
def console_kernel_env(tmp_path, monkeypatch):
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("CHECKPOINTS_DIR", str(cp_dir))

    # Also update module-level global paths in console if needed
    monkeypatch.setattr(c, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c, "TASKS_FILE", str(tasks_file))

    wf_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    return {
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "cp_dir": cp_dir,
    }


def _seed_wf(env, wid, status="running"):
    data = json.loads(env["wf_file"].read_text(encoding="utf-8"))
    data["workflows"][wid] = {
        "workflow_id": wid,
        "status": status,
        "config": {
            "nodes": [
                {"id": "req", "label": "需求分析", "depends_on": []},
                {"id": "impl", "label": "开发实现", "depends_on": ["req"]},
                {"id": "gate1", "label": "门禁验收", "depends_on": ["impl"], "gate": {"type": "auto"}},
            ]
        },
        "coordinator_pane_id": "pane_coord_1",
    }
    env["wf_file"].write_text(json.dumps(data, indent=2), encoding="utf-8")


def _seed_task(env, tid, wid, node, status="completed", verdict=None, version=1):
    data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    data["tasks"].append({
        "task_id": tid,
        "workflow_id": wid,
        "node": node,
        "status": status,
        "stage_verdict": verdict,
        "version": version,
    })
    env["tasks_file"].write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_api_kernel_pause_and_resume(console_kernel_env):
    wid = "wf_api_test"
    _seed_wf(console_kernel_env, wid, status="running")

    # Pause
    res = c.api_kernel_pause({"workflow_id": wid})
    assert res["ok"] is True
    assert res["status"] == "paused"

    # Resume
    res = c.api_kernel_resume({"workflow_id": wid})
    assert res["ok"] is True
    assert res["status"] == "running"


def test_api_kernel_step(console_kernel_env):
    wid = "wf_api_step"
    _seed_wf(console_kernel_env, wid, status="paused")
    _seed_task(console_kernel_env, "t_req", wid, "req", status="completed")

    res = c.api_kernel_step({"workflow_id": wid})
    assert res["ok"] is True
    assert res["stepped_node"] == "impl"


def test_api_kernel_rollback(console_kernel_env):
    wid = "wf_api_rb"
    _seed_wf(console_kernel_env, wid, status="running")
    _seed_task(console_kernel_env, "t_req", wid, "req", status="completed")
    _seed_task(console_kernel_env, "t_impl", wid, "impl", status="completed")

    res = c.api_kernel_rollback({"workflow_id": wid, "target_node_id": "impl", "reason": "Bug found"})
    assert res["ok"] is True
    assert "t_impl" in res["invalidated_tasks"]


def test_api_kernel_force_pass(console_kernel_env):
    wid = "wf_api_fp"
    _seed_wf(console_kernel_env, wid, status="running")
    _seed_task(console_kernel_env, "t_gate", wid, "gate1", status="completed", verdict="blocked")

    # Missing expected_version must be rejected
    with pytest.raises(RuntimeError) as exc:
        c.api_kernel_force_pass({"workflow_id": wid, "gate_node_id": "gate1", "note": "Emergency override", "confirmed": True})
    assert "版本快照保护字段" in str(exc.value)

    res = c.api_kernel_force_pass({
        "workflow_id": wid,
        "gate_node_id": "gate1",
        "note": "Emergency override",
        "confirmed": True,
        "expected_version": 1,
    })
    assert res["ok"] is True
    assert "t_gate" in res["updated_tasks"]


def test_api_kernel_checkpoint_lifecycle(console_kernel_env):
    wid = "wf_api_cp"
    _seed_wf(console_kernel_env, wid, status="running")
    _seed_task(console_kernel_env, "t_req", wid, "req", status="completed")

    # Create checkpoint
    res = c.api_kernel_checkpoint_create({"workflow_id": wid, "tag": "milestone_1"})
    assert res["ok"] is True
    cp_id = res["checkpoint_id"]

    # List checkpoints
    res_list = c.api_kernel_checkpoint_list(wid)
    assert len(res_list) == 1
    assert res_list[0]["checkpoint_id"] == cp_id
    assert res_list[0]["tag"] == "milestone_1"

    # Mutate workflow state
    wf_data = json.loads(console_kernel_env["wf_file"].read_text(encoding="utf-8"))
    wf_data["workflows"][wid]["status"] = "failed"
    console_kernel_env["wf_file"].write_text(json.dumps(wf_data), encoding="utf-8")

    # Restore checkpoint
    res_restore = c.api_kernel_checkpoint_restore({"workflow_id": wid, "checkpoint_id": cp_id})
    assert res_restore["ok"] is True
    assert res_restore["status"] == "running"

