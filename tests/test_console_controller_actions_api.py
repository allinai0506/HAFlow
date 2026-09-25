#!/usr/bin/env python3
"""Tests for Herdr Factory Console Controller Actions API endpoints.

Covers:
- GET  /api/workflow/controller-actions
- POST /api/controller/execute-action
- Exclusion of superseded tasks in blocker resolution
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from console import herdr_factory_console as c


@pytest.fixture
def console_actions_env(tmp_path, monkeypatch):
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setattr(c, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c, "TASKS_FILE", str(tasks_file))

    # Seed a workflow with a superseded blocked task and an active failed task
    wf_data = {
        "workflows": {
            "wf-test-01": {
                "workflow_id": "wf-test-01",
                "title": "测试工作流",
                "status": "running",
                "project_root": "/tmp/test-proj",
                "config": {"nodes": []},
            }
        }
    }
    tasks_data = {
        "tasks": [
            {
                "task_id": "wf-test-01-old",
                "workflow_id": "wf-test-01",
                "stage": "plan",
                "status": "superseded",
                "stage_verdict": "blocked",
                "stage_verdict_note": "旧阻断",
            },
            {
                "task_id": "wf-test-01-test",
                "workflow_id": "wf-test-01",
                "stage": "test",
                "status": "failed",
                "stage_verdict": "blocked",
                "stage_verdict_note": "FAIL: 2 blocking defects",
                "agent": "qodercli",
            }
        ]
    }

    wf_file.write_text(json.dumps(wf_data), encoding="utf-8")
    tasks_file.write_text(json.dumps(tasks_data), encoding="utf-8")

    return {"wf_file": wf_file, "tasks_file": tasks_file}


def test_api_workflow_controller_actions_query(console_actions_env):
    res = c.api_workflow_controller_actions("wf-test-01")
    assert res["workflow_id"] == "wf-test-01"
    assert "blockers" in res
    assert "actions" in res

    # Superseded task must be excluded!
    blocker_ids = [b["task_id"] for b in res["blockers"]]
    assert "wf-test-01-old" not in blocker_ids
    assert "wf-test-01-test" in blocker_ids

    # Actions must include command_line
    actions = res["actions"]
    assert len(actions) >= 1
    assert any("bin/herdr-task launch" in a["command_line"] for a in actions)
    assert any(a["action_id"].endswith(":dispatch_fix_loop") for a in actions)


def test_api_controller_execute_action_launch(console_actions_env):
    payload = {
        "type": "launch",
        "task_id": "wf-test-01-fix-1",
        "workflow_id": "wf-test-01",
        "stage": "implementation",
        "agent": "codex",
        "prompt": "修复测试缺陷",
    }
    with patch.object(c, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"task_id": "wf-test-01-fix-1"}', stderr="")
        res = c.api_controller_execute_action(payload)
        assert res.get("ok") is True
        assert res.get("task_id") == "wf-test-01-fix-1"
        assert mock_run.called
        cmd_args = mock_run.call_args[0][0]
        assert "--task-id" in cmd_args
        assert "wf-test-01-fix-1" in cmd_args
        assert "--workflow-id" in cmd_args
        assert "wf-test-01" in cmd_args


def test_api_controller_execute_action_force_pass_advance(console_actions_env):
    payload = {
        "type": "force_pass_advance",
        "workflow_id": "wf-test-01",
        "stage": "test",
        "gate_node_id": "test",
    }
    with patch.object(c.herdr_kernel, "force_pass_gate") as mock_gate, patch.object(c, "manual_advance") as mock_adv:
        mock_adv.return_value = {"ok": True, "advanced": True}
        res = c.api_controller_execute_action(payload)
        assert res.get("ok") is True
        assert res.get("advanced") == {"ok": True, "advanced": True}
        mock_gate.assert_called_once()
        mock_adv.assert_called_once_with("wf-test-01")
