#!/usr/bin/env python3
"""Tests for Herdr Factory Console Steering Endpoints (Phase 2).

Covers:
- POST /api/task/steer
- POST /api/task/halt
- GET  /api/task/steer/queue
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from console import herdr_factory_console as c
from herdr import steering


@pytest.fixture
def console_steering_env(tmp_path, monkeypatch):
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    steering_file = tmp_path / "steering.json"

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("STEERING_FILE", str(steering_file))

    monkeypatch.setattr(c.common, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c.common, "TASKS_FILE", str(tasks_file))

    wf_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    steering_file.write_text(json.dumps({"steering_queues": {}, "history": []}), encoding="utf-8")

    return {
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "steering_file": steering_file,
    }


def _seed_task(env, task_id, status="working", pane_id="pane-api-1"):
    t_data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    task = {
        "task_id": task_id,
        "workflow_id": "wf-api",
        "status": status,
        "pane_id": pane_id,
        "node": "dev",
        "agent": "codex",
    }
    t_data["tasks"].append(task)
    env["tasks_file"].write_text(json.dumps(t_data), encoding="utf-8")
    return task


def test_console_api_task_steer_queued(console_steering_env):
    task_id = "task-c-01"
    _seed_task(console_steering_env, task_id, status="working")

    res = c.api_task_steer({
        "task_id": task_id,
        "instruction": "Prefer simple dictionary lookup",
        "urgent": False,
    })
    assert res["ok"] is True
    assert res["status"] == "queued"

    queue_res = c.api_task_steer_queue(task_id)
    assert len(queue_res) == 1
    assert queue_res[0]["instruction"] == "Prefer simple dictionary lookup"


def test_console_api_task_steer_urgent(console_steering_env):
    task_id = "task-c-02"
    _seed_task(console_steering_env, task_id, status="working")

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        res = c.api_task_steer({
            "task_id": task_id,
            "instruction": "Halt and rewrite loop",
            "urgent": True,
        })
    assert res["ok"] is True
    assert res["status"] == "dispatched"


def test_console_api_task_halt(console_steering_env):
    task_id = "task-c-03"
    _seed_task(console_steering_env, task_id, status="working")

    mock_run = MagicMock()
    mock_run.return_value.returncode = 0

    with patch("subprocess.run", mock_run):
        res = c.api_task_halt({
            "task_id": task_id,
            "reason": "Runaway execution detected",
        })
    assert res["ok"] is True
    assert res["status"] == "interrupted"

    # Verify task status in tasks.json
    t_data = json.loads(console_steering_env["tasks_file"].read_text(encoding="utf-8"))
    t = next(x for x in t_data["tasks"] if x["task_id"] == task_id)
    assert t["status"] == "interrupted"
