#!/usr/bin/env python3
"""Unit tests for Herdr Controller Action Resolution Engine.

Tests:
- Identification and resolution of active workflow blockers (excluding superseded tasks)
- Generation of actionable controller resolutions (Fix-Loop, Relaunch with Agent, Gate Advance)
- Correct CLI command string formatting for bin/herdr-task
"""

import pytest
from herdr.controller_actions import (
    ControllerAction,
    resolve_workflow_blockers,
    generate_controller_actions,
    build_cli_command,
    next_replacement_id,
)


def test_next_replacement_id_lineage():
    assert next_replacement_id("wf-01-test") == "wf-01-test-r2"
    assert next_replacement_id("wf-01-test-r2") == "wf-01-test-r3"
    assert next_replacement_id("wf-01-test-r9") == "wf-01-test-r10"
    assert next_replacement_id("", "fallback-r2") == "fallback-r2"


def test_resolve_workflow_blockers_excludes_superseded():
    tasks = [
        {
            "task_id": "t-old-blocked",
            "node": "plan",
            "stage": "plan",
            "status": "superseded",
            "stage_verdict": "blocked",
            "stage_verdict_note": "old blocked note",
        },
        {
            "task_id": "t-done",
            "node": "requirements",
            "stage": "requirements",
            "status": "cleaned",
            "stage_verdict": "pass",
        },
        {
            "task_id": "t-active-failed",
            "node": "test",
            "stage": "test",
            "status": "failed",
            "stage_verdict": "blocked",
            "stage_verdict_note": "FAIL: 3 blocking defects",
        },
    ]
    workflow = {"workflow_id": "wf-001"}
    blockers = resolve_workflow_blockers(tasks, workflow)
    assert len(blockers) == 1
    assert blockers[0]["task_id"] == "t-active-failed"


def test_generate_actions_for_test_failure():
    task = {
        "task_id": "wf-001-test-r1",
        "workflow_id": "wf-001",
        "node": "test",
        "stage": "test",
        "status": "failed",
        "stage_verdict": "blocked",
        "stage_verdict_note": "测试裁决 FAIL, blocking defects=3: ① 缺少状态CAS原子更新; ② 超时时间未限制; ③ 日志脱敏不全",
        "agent": "qodercli",
    }
    workflow = {
        "workflow_id": "wf-001",
        "project_root": "/path/to/project",
        "title": "测试修复工作流",
    }
    actions = generate_controller_actions(task, workflow, project_root="/path/to/project")

    # Should offer Fix-Loop, Relaunch with alternative agent, and Gate Bypass
    action_ids = [a.action_id for a in actions]
    assert "wf-001-test-r1:dispatch_fix_loop" in action_ids
    assert "wf-001-test-r1:retest_with_agent" in action_ids
    assert "wf-001-test-r1:force_pass_advance" in action_ids

    fix_act = next(a for a in actions if a.action_id.endswith(":dispatch_fix_loop"))
    assert "--task-id wf-001-impl-fix" in fix_act.command_line
    assert "--stage implementation" in fix_act.command_line
    assert "--workflow-id wf-001" in fix_act.command_line
    assert fix_act.category == "fix"
    assert fix_act.recommended is True
    assert fix_act.api_payload["task_id"] == "wf-001-impl-fix"

    retest_act = next(a for a in actions if a.action_id.endswith(":retest_with_agent"))
    assert "--task-id wf-001-test-r2" in retest_act.command_line
    assert "--stage test" in retest_act.command_line
    assert "--supersedes wf-001-test-r1" in retest_act.command_line
    assert retest_act.category == "rework"
    assert "换执行者" in retest_act.title
    assert retest_act.api_payload["task_id"] == "wf-001-test-r2"

    advance_act = next(a for a in actions if a.action_id.endswith(":force_pass_advance"))
    assert "bin/herdr-task advance wf-001" in advance_act.command_line
    assert advance_act.api_payload["type"] == "force_pass_advance"
    assert advance_act.is_destructive is True


def test_generate_actions_carry_task_binding_and_effect():
    """End-user console needs task-bound buttons: blocker id + plain-language effect, no CLI reading."""
    task = {
        "task_id": "wf-001-test-r1",
        "workflow_id": "wf-001",
        "node": "test",
        "stage": "test",
        "status": "failed",
        "stage_verdict": "blocked",
        "stage_verdict_note": "FAIL blocking defects=3",
        "agent": "qodercli",
    }
    workflow = {"workflow_id": "wf-001", "project_root": "/tmp/p"}
    actions = generate_controller_actions(task, workflow, project_root="/tmp/p")
    assert actions, "expected actions for failed test blocker"
    for a in actions:
        d = a.to_dict()
        assert d["blocker_task_id"] == "wf-001-test-r1"
        assert d["effect"], f"missing human-readable effect for {a.action_id}"
        assert "wf-001-test-r1" in d["effect"] or "当前阶段" in d["effect"]


def test_generate_actions_for_plan_rework_spin():
    task = {
        "task_id": "wf-001-plan-spin",
        "workflow_id": "wf-001",
        "node": "plan",
        "stage": "plan",
        "status": "rework",
        "stage_verdict": "blocked",
        "stage_verdict_note": "plan-attack三轮DONE均零交付: 请总指挥裁决: 1)换agent重派; 2)或豁免",
        "agent": "qodercli",
    }
    workflow = {
        "workflow_id": "wf-001",
        "project_root": "/path/to/project",
    }
    actions = generate_controller_actions(task, workflow, project_root="/path/to/project")
    action_ids = [a.action_id for a in actions]
    assert "wf-001-plan-spin:relaunch_with_agent" in action_ids
    assert "wf-001-plan-spin:force_pass_advance" in action_ids

    relaunch = next(a for a in actions if a.action_id.endswith(":relaunch_with_agent"))
    assert "--task-id wf-001-plan-spin-r2" in relaunch.command_line
    assert "--supersedes wf-001-plan-spin" in relaunch.command_line
    # Suggested agent should not be the failed one (qodercli)
    assert "--agent qodercli" not in relaunch.command_line
    assert "换执行者" in relaunch.title


def test_build_cli_command_formatting():
    cmd = build_cli_command("launch", {
        "task-id": "wf-100-impl",
        "workflow-id": "wf-100",
        "stage": "implementation",
        "agent": "codex",
        "supersedes": "wf-100-old",
        "prompt": "修复缺陷: A & B",
    })
    assert cmd.startswith("bin/herdr-task launch")
    assert "--task-id wf-100-impl" in cmd
    assert "--workflow-id wf-100" in cmd
    assert "--stage implementation" in cmd
    assert "--agent codex" in cmd
    assert "--supersedes wf-100-old" in cmd
    assert "'修复缺陷: A & B'" in cmd

    # Test positional arguments
    adv_cmd = build_cli_command("advance", positionals=["wf-100"])
    assert adv_cmd == "bin/herdr-task advance wf-100"

    clear_cmd = build_cli_command("clear-escalation", positionals=["task-123"])
    assert clear_cmd == "bin/herdr-task clear-escalation task-123"
