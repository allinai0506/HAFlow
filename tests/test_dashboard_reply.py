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


def test_steer_rejects_unowned_or_mismatched_pane_identity(monkeypatch):
    from console.dashboard_reply import _steer
    from herdr import steering

    queued_steers = []
    monkeypatch.setattr(steering, "queue_steer", lambda *a, **kw: queued_steers.append((a, kw)))

    task = {
        "task_id": "t-reused",
        "status": "blocked",
        "workflow_id": "wf-1",
        "pane_id": "pane-stale-99",
    }
    plan = reply_plan(task, "继续执行")
    assert plan["ok"] is True

    # Identity mismatch / dead pane probe: must fail closed before steering.queue_steer
    mismatched_probe = lambda t: (False, "identity_mismatch")
    res = _steer(plan, task=task, probe_pane=mismatched_probe)
    assert res["ok"] is False
    assert res["status"] == "blocked"
    assert res["reason"] == "identity_mismatch"
    assert len(queued_steers) == 0, "No interrupt or injection must occur when pane identity is mismatched"

    # api_dashboard_reply must translate to friendly error without calling queue_steer
    with pytest.raises(RuntimeError, match="没有活着的工位"):
        api_dashboard_reply(
            {"task_id": "t-reused", "instruction": "继续执行"},
            load_task=lambda tid: task,
            probe_pane=mismatched_probe,
        )
    assert len(queued_steers) == 0


def test_reply_plan_reopens_paused_and_interrupted_tasks():
    # Regular paused task reopens to working
    task_paused = {"task_id": "t-p", "status": "paused", "workflow_id": "wf-1"}
    plan_paused = reply_plan(task_paused, "继续做")
    assert plan_paused["ok"] is True
    assert plan_paused["mode"] == "steer"
    assert plan_paused["reopen_to"] == "working"

    # Paused task that originated from rework reopens to rework
    task_from_rework = {
        "task_id": "t-pr",
        "status": "paused",
        "workflow_id": "wf-1",
        "status_history": [
            {"from": "working", "to": "rework"},
            {"from": "rework", "to": "paused"},
        ],
    }
    plan_rework = reply_plan(task_from_rework, "继续做")
    assert plan_rework["ok"] is True
    assert plan_rework["reopen_to"] == "rework"

    # Interrupted task reopens to working
    task_interrupted = {"task_id": "t-i", "status": "interrupted", "workflow_id": "wf-1"}
    plan_interrupted = reply_plan(task_interrupted, "继续做")
    assert plan_interrupted["ok"] is True
    assert plan_interrupted["reopen_to"] == "working"


def test_api_dashboard_reply_reopens_paused_task_before_steering(monkeypatch):
    from console.dashboard_reply import _steer
    from herdr import steering

    reopened = []
    queued = []

    monkeypatch.setattr(steering, "queue_steer", lambda *a, **kw: queued.append((a, kw)) or {"ok": True, "status": "dispatched"})

    task = {
        "task_id": "t-p1",
        "status": "paused",
        "workflow_id": "wf-1",
        "pane_id": "pane-1",
    }
    res = api_dashboard_reply(
        {"task_id": "t-p1", "instruction": "继续推进"},
        load_task=lambda tid: task,
        probe_pane=lambda t: (True, "identity_match"),
        reopen_task=lambda tid, target: reopened.append((tid, target)),
    )
    assert res["ok"] is True
    assert len(reopened) == 1
    assert reopened[0] == ("t-p1", "working")
    assert len(queued) == 1


def test_paused_task_reopen_allows_task_listener_to_consume_events(tmp_path, monkeypatch):
    """Reproduce: working/done events after successful reply transition out of paused."""
    from herdr import kernel, state_db
    from herdr.state_store import SQLiteStateStore
    from services.controller import task_listener

    db_file = tmp_path / "test.db"
    store = SQLiteStateStore(db_file)
    monkeypatch.setattr(kernel, "get_state_store", lambda: store)

    store.save_workflow({"workflow_id": "wf-1", "title": "WF 1", "status": "running"})
    store.save_task({
        "task_id": "t-paused",
        "workflow_id": "wf-1",
        "node": "impl",
        "stage": "implementation",
        "agent": "codex",
        "status": "paused",
        "pane_id": "pane-1",
    })

    # 1. Before reply: task_listener ignores working/done because status is 'paused'
    class FakePorts:
        def __init__(self):
            self.observed = []
            self.acked = []
        def get_task(self, task_id):
            return store.get_task(task_id)
        def blocked_event_type(self, task):
            return None
        def _set_observed_status(self, task, to_status, reason):
            self.observed.append((task["task_id"], to_status, reason))
            return True
        def maybe_ack_on_working(self, task_id):
            self.acked.append(task_id)
        def _completion_marker_snapshot(self, task):
            return True, "ALL TASKS COMPLETED"
        def _record_completion_sample(self, task, has_marker, agent_status):
            self.observed.append((task["task_id"], "completion_sample", agent_status))

    ports = FakePorts()
    # Emitting events while paused produces no transition
    task_listener.handle_event(ports, "t-paused", "working")
    assert len(ports.observed) == 0, "task_listener ignores events for paused tasks"

    # 2. User replies from dashboard -> reopens task to working
    task_obj = store.get_task("t-paused")
    reply_res = api_dashboard_reply(
        {"task_id": "t-paused", "instruction": "按计划继续"},
        load_task=lambda tid: store.get_task(tid),
        probe_pane=lambda t: (True, "identity_match"),
        steer=lambda p: {"ok": True, "status": "dispatched"},
        reopen_task=lambda tid, target: kernel.transition_task(
            task_id=tid, to_status=target, reason="dashboard_reply_resume", store=store
        ),
    )
    assert reply_res["ok"] is True
    reopened_task = store.get_task("t-paused")
    assert reopened_task["status"] == "working", "Task must leave paused and enter working"

    # 3. After reply: agent reports working -> task_listener processes it
    ports.observed.clear()
    task_listener.handle_event(ports, "t-paused", "working")
    task_listener.handle_event(ports, "t-paused", "done")
    assert any(item[1] == "completion_sample" for item in ports.observed), "Completion sample is processed now that task is working"
