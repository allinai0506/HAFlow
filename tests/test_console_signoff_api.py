#!/usr/bin/env python3
"""Tests for Herdr Factory Console Signoff Chamber Endpoint (Phase 5).

Covers:
- POST /api/task/signoff (approve / pass gate)
- POST /api/task/signoff (reject / rollback & queue steer)
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch

from console import herdr_factory_console as c


@pytest.fixture
def console_signoff_env(tmp_path, monkeypatch):
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    steer_file = tmp_path / "steering.json"

    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("STEERING_FILE", str(steer_file))

    monkeypatch.setattr(c, "WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setattr(c, "TASKS_FILE", str(tasks_file))

    wf_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

    return {
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "steer_file": steer_file,
    }


def _seed_wf(env, wid="wf-test-5"):
    data = json.loads(env["wf_file"].read_text(encoding="utf-8"))
    data["workflows"][wid] = {
        "workflow_id": wid,
        "status": "running",
        "config": {
            "nodes": [
                {"id": "market_scope", "label": "范围界定", "depends_on": []},
                {"id": "data_extraction", "label": "数据爬取", "depends_on": ["market_scope"]},
                {"id": "executive_briefing", "label": "高管会签", "depends_on": ["data_extraction"], "gate": {"type": "hybrid", "retry_target": "data_extraction"}},
            ]
        },
    }
    env["wf_file"].write_text(json.dumps(data), encoding="utf-8")


def _seed_task(env, tid="task-signoff-1", wid="wf-test-5", node="executive_briefing", status="blocked"):
    t_data = json.loads(env["tasks_file"].read_text(encoding="utf-8"))
    t_data["tasks"].append({
        "task_id": tid,
        "workflow_id": wid,
        "node": node,
        "stage": node,
        "status": status,
        "stage_verdict": "blocked",
        "pane_id": "pane-mock-1",
        "version": 1,
    })
    env["tasks_file"].write_text(json.dumps(t_data), encoding="utf-8")


def test_api_task_signoff_approve(console_signoff_env):
    _seed_wf(console_signoff_env)
    _seed_task(console_signoff_env)

    req_body = {
        "task_id": "task-signoff-1",
        "action": "approve",
        "feedback": "各项财务数据均已核准，批准放行！",
        "operator": "CEO",
    }

    # Missing confirmation must be rejected
    with pytest.raises(RuntimeError) as exc_conf:
        c.api_task_signoff(req_body)
    assert "显式确认" in str(exc_conf.value)

    # Missing expected_version must be rejected
    with pytest.raises(RuntimeError) as exc_ver:
        c.api_task_signoff({**req_body, "confirmed": True})
    assert "expected_version" in str(exc_ver.value)

    # Valid signoff with confirmation and expected_version succeeds
    res = c.api_task_signoff({**req_body, "confirmed": True, "expected_version": 1})
    assert res.get("ok") is True
    assert res.get("action") == "approve"

    # Verify task verdict was updated
    tasks = json.loads(console_signoff_env["tasks_file"].read_text(encoding="utf-8"))["tasks"]
    t = next(x for x in tasks if x["task_id"] == "task-signoff-1")
    assert t["stage_verdict"] == "pass"
    assert "批准放行" in t["stage_verdict_note"]


def test_api_task_signoff_reject(console_signoff_env):
    _seed_wf(console_signoff_env)
    _seed_task(console_signoff_env)

    req_body = {
        "task_id": "task-signoff-1",
        "action": "reject",
        "retry_target": "data_extraction",
        "feedback": "竞品海外收入拆解不全，请补充 Q3 附注并重算",
        "operator": "CEO",
    }
    res = c.api_task_signoff(req_body)
    assert res.get("ok") is True
    assert res.get("action") == "reject"
    assert res.get("rollback", {}).get("ok") is True
    assert res.get("target_node") == "data_extraction"
    assert "海外收入拆解不全" in res.get("feedback", "")
