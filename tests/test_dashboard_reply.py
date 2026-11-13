"""Tests for dashboard reply: one human sentence continues a stuck or failed task."""

import pytest

from console.dashboard_reply import api_dashboard_reply
from herdr.dashboard import reply_mode_for, reply_plan


def test_reply_mode_for_different_statuses():
    assert reply_mode_for({"status": "working"}) == "steer"
    assert reply_mode_for({"status": "dispatched"}) == "steer"
    assert reply_mode_for({"status": "rework"}) == "steer"
    assert reply_mode_for({"status": "blocked"}) == "steer"
    assert reply_mode_for({"status": "paused"}) == "steer"
    assert reply_mode_for({"status": "interrupted"}) == "steer"

    assert reply_mode_for({"status": "failed"}) == "resume"

    assert reply_mode_for({"status": "completed"}) == "none"
    assert reply_mode_for({"status": "committed"}) == "none"
    assert reply_mode_for({"status": "integrated"}) == "none"
    assert reply_mode_for({"status": "cleaned"}) == "none"
    assert reply_mode_for({"status": "superseded"}) == "none"
    assert reply_mode_for({"status": "pending"}) == "none"
    assert reply_mode_for({}) == "none"
    assert reply_mode_for(None) == "none"


def test_reply_plan_validates_instruction_and_task():
    task = {"task_id": "t-1", "status": "working", "workflow_id": "wf-1"}

    plan = reply_plan(task, "")
    assert not plan["ok"]
    assert "请先写一句话" in plan["error"]

    plan = reply_plan(task, "   ")
    assert not plan["ok"]
    assert "请先写一句话" in plan["error"]

    plan = reply_plan(task, None)
    assert not plan["ok"]
    assert "请先写一句话" in plan["error"]

    plan = reply_plan(task, "a" * 1001)
    assert not plan["ok"]
    assert "1000" in plan["error"]

    plan = reply_plan({}, "继续")
    assert not plan["ok"]
    assert "找不到" in plan["error"]


def test_reply_plan_for_steer_mode():
    task = {
        "task_id": "t-1",
        "status": "working",
        "workflow_id": "wf-1",
        "agent": "codex",
    }
    plan = reply_plan(task, "请换用备用方案")
    assert plan["ok"]
    assert plan["mode"] == "steer"
    assert plan["task_id"] == "t-1"
    assert plan["instruction"] == "请换用备用方案"
    assert plan["urgent"] is True
    assert "已经送到这个工位" in plan["message"]


def test_reply_plan_for_resume_mode_and_no_force_pass():
    task = {
        "task_id": "wf-1-impl",
        "status": "failed",
        "workflow_id": "wf-1",
        "stage": "implementation",
        "agent": "codex",
        "goal": "完成核心模块",
        "source": "/path/to/project",
    }
    plan = reply_plan(task, "直接用现成库，别手写")
    assert plan["ok"]
    assert plan["mode"] == "resume"
    assert plan["task_id"] == "wf-1-impl"
    assert plan["new_task_id"] == "wf-1-impl-r2"
    assert "重新开始" in plan["message"]

    launch = plan["launch"]
    assert launch["type"] == "launch"
    assert launch["task_id"] == "wf-1-impl-r2"
    assert launch["workflow_id"] == "wf-1"
    assert launch["stage"] == "implementation"
    assert launch["agent"] == "codex"
    assert launch["supersedes"] == "wf-1-impl"
    assert launch["goal"] == "完成核心模块"
    assert launch["source"] == "/path/to/project"
    assert "人的回复：直接用现成库，别手写" in launch["prompt"]
    assert "原目标：完成核心模块" in launch["prompt"]

    # Security & gate discipline: no force_pass in resume
    assert "force_pass" not in launch
    assert "force_pass_advance" not in launch


def test_reply_plan_rejects_closed_task():
    task = {"task_id": "t-done", "status": "completed", "workflow_id": "wf-1"}
    plan = reply_plan(task, "继续")
    assert not plan["ok"]
    assert "已经结束" in plan["error"]


def test_reply_plan_rejects_failed_task_without_workflow():
    task = {"task_id": "t-orphan", "status": "failed"}
    plan = reply_plan(task, "继续")
    assert not plan["ok"]
    assert "没有工作流" in plan["error"]


def test_api_dashboard_reply_steer_success():
    task = {"task_id": "t-1", "status": "working", "workflow_id": "wf-1"}
    steer_calls = []

    def fake_steer(plan):
        steer_calls.append(plan)
        return {"ok": True, "status": "dispatched"}

    res = api_dashboard_reply(
        {"task_id": "t-1", "instruction": "快点做"},
        load_task=lambda tid: task,
        steer=fake_steer,
    )
    assert res["ok"] is True
    assert res["mode"] == "steer"
    assert res["task_id"] == "t-1"
    assert len(steer_calls) == 1
    assert steer_calls[0]["instruction"] == "快点做"


def test_api_dashboard_reply_steer_error_handling():
    task = {"task_id": "t-1", "status": "working", "workflow_id": "wf-1"}

    with pytest.raises(RuntimeError, match="没有活着的工位"):
        api_dashboard_reply(
            {"task_id": "t-1", "instruction": "快点做"},
            load_task=lambda tid: task,
            steer=lambda p: {"ok": False, "reason": "no_pane_id"},
        )

    with pytest.raises(RuntimeError, match="不能接收这句话"):
        api_dashboard_reply(
            {"task_id": "t-1", "instruction": "快点做"},
            load_task=lambda tid: task,
            steer=lambda p: {"ok": False, "reason": "soft_steer_not_supported"},
        )

    with pytest.raises(RuntimeError, match="没有送到工位"):
        api_dashboard_reply(
            {"task_id": "t-1", "instruction": "快点做"},
            load_task=lambda tid: task,
            steer=lambda p: {"ok": False, "reason": "unknown_failure"},
        )


def test_api_dashboard_reply_resume_success():
    task = {
        "task_id": "wf-1-test",
        "status": "failed",
        "workflow_id": "wf-1",
        "stage": "test",
        "agent": "codex",
    }
    launch_calls = []

    def fake_launch(payload):
        launch_calls.append(payload)
        return {"ok": True, "launched": True}

    res = api_dashboard_reply(
        {"task_id": "wf-1-test", "instruction": "加上超时重试"},
        load_task=lambda tid: task,
        launch=fake_launch,
    )
    assert res["ok"] is True
    assert res["mode"] == "resume"
    assert res["task_id"] == "wf-1-test-r2"
    assert res["supersedes"] == "wf-1-test"
    assert len(launch_calls) == 1
    assert launch_calls[0]["type"] == "launch"


def test_api_dashboard_reply_validates_inputs():
    with pytest.raises(RuntimeError, match="找不到这件任务"):
        api_dashboard_reply({"task_id": "", "instruction": "继续"})

    with pytest.raises(RuntimeError, match="找不到这件任务"):
        api_dashboard_reply(
            {"task_id": "nonexistent", "instruction": "继续"},
            load_task=lambda tid: None,
        )

    task = {"task_id": "t-1", "status": "working", "workflow_id": "wf-1"}
    with pytest.raises(RuntimeError, match="请先写一句话"):
        api_dashboard_reply(
            {"task_id": "t-1", "instruction": ""},
            load_task=lambda tid: task,
        )
