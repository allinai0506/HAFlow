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
                "version": 1,
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

    assert res['actions'] == []
    assert len(res['recovery']) == 1
    assert res['recovery'][0]['payload']['task_ids'] == ['wf-test-01-test']
    assert res['recovery'][0]['status'] == 'waiting_human'


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
        c.api_workflow_controller_actions('wf-test-01')
        with pytest.raises(RuntimeError, match='持久恢复'):
            c.api_controller_execute_action(payload)
        mock_run.assert_not_called()


def test_api_controller_execute_action_force_pass_advance(console_actions_env):
    # Missing confirmation must be refused
    with pytest.raises(RuntimeError):
        c.api_controller_execute_action({
            "type": "force_pass_advance",
            "workflow_id": "wf-test-01",
            "stage": "test",
            "gate_node_id": "test",
        })

    # Missing expected_version must be refused (isolate from recovery obligation check)
    with patch.object(c, "api_workflow_recovery", return_value={"operations": []}):
        with pytest.raises(RuntimeError) as exc:
            c.api_controller_execute_action({
                "type": "force_pass_advance",
                "workflow_id": "wf-test-01",
                "stage": "test",
                "gate_node_id": "test",
                "confirmed": True,
                "reason": "人工在控制台审核确认通过",
            })
    assert "版本快照保护字段" in str(exc.value)

    payload = {
        "type": "force_pass_advance",
        "workflow_id": "wf-test-01",
        "task_id": "wf-test-01-test",
        "stage": "test",
        "gate_node_id": "test",
        "expected_version": 1,
        "confirmed": True,
        "reason": "人工在控制台审核确认通过",
    }
    with patch.object(c.herdr_kernel, "force_pass_gate") as mock_gate, patch.object(c, "manual_advance") as mock_adv:
        c.api_workflow_controller_actions('wf-test-01')
        with pytest.raises(RuntimeError, match='持久恢复'):
            c.api_controller_execute_action(payload)
        mock_gate.assert_not_called()
        mock_adv.assert_not_called()
