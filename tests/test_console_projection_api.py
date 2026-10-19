#!/usr/bin/env python3
"""Tests for Herdr Factory Console Projection Endpoints (Phase 3).

Covers:
- GET /api/task/projection
- GET /api/workflow/projection
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from console import herdr_factory_console as c


@pytest.fixture
def console_proj_env(tmp_path, monkeypatch):
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))

    monkeypatch.setattr(c.common, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c.common, "TASKS_FILE", str(tasks_file))

    wf_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    return {
        "wf_file": wf_file,
        "tasks_file": tasks_file,
    }


def _seed_wf(env, wid, status="running"):
    data = json.loads(env["wf_file"].read_text(encoding="utf-8"))
    data["workflows"][wid] = {
        "workflow_id": wid,
        "status": status,
        "config": {
            "nodes": [
                {"id": "req", "label": "需求分析"},
                {"id": "impl", "label": "开发实现"},
            ]
        },
    }
    env["wf_file"].write_text(json.dumps(data), encoding="utf-8")
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(env["wf_file"].parent / "state.db", auto_migrate_json=False)
    store.save_workflow(data["workflows"][wid])


def _seed_task(env, task_id, wid="wf-proj-test", status="working", goal="Build Telemetry"):
    t_data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    task = {
        "task_id": task_id,
        "workflow_id": wid,
        "status": status,
        "goal": goal,
        "pane_id": "pane-mock-1",
        "agent": "codex",
        "node": "impl",
    }
    t_data["tasks"].append(task)
    env["tasks_file"].write_text(json.dumps(t_data), encoding="utf-8")
    from herdr.state_store import SQLiteStateStore
    store = SQLiteStateStore(env["tasks_file"].parent / "state.db", auto_migrate_json=False)
    store.save_task(task)
    return task


def test_api_task_projection(console_proj_env):
    task_id = "t-api-proj-1"
    _seed_task(console_proj_env, task_id, goal="Extract clean telemetries")

    with patch("herdr.projection._read_pane_content", return_value="Compiling...\n"):
        res = c.api_task_projection(task_id)

    assert res["task_id"] == task_id
    assert res["status"] == "working"
    assert res["intent"] == "Extract clean telemetries"
    assert "artifacts" in res
    assert "milestones" in res


def test_api_workflow_projection(console_proj_env):
    wid = "wf-api-proj-1"
    _seed_wf(console_proj_env, wid)
    _seed_task(console_proj_env, "t-api-1", wid=wid, status="completed")
    _seed_task(console_proj_env, "t-api-2", wid=wid, status="working")

    with patch("herdr.projection._read_pane_content", return_value=""):
        res = c.api_workflow_projection(wid)

    assert res["workflow_id"] == wid
    assert len(res["tasks"]) == 2
    assert res["progress"]["total_tasks"] == 2
    assert res["progress"]["completed_tasks"] == 1
