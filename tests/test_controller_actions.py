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
)


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
    assert "dispatch_fix_loop" in action_ids
    assert "relaunch_with_agent" in action_ids
    assert "force_pass_advance" in action_ids

    fix_act = next(a for a in actions if a.action_id == "dispatch_fix_loop")
    assert "--stage implementation" in fix_act.command_line
    assert "--workflow-id wf-001" in fix_act.command_line
    assert fix_act.category == "fix"
    assert fix_act.recommended is True

    retest_act = next(a for a in actions if a.action_id == "relaunch_with_agent")
    assert "--stage test" in retest_act.command_line
    assert "--supersedes wf-001-test-r1" in retest_act.command_line
    assert retest_act.category == "rework"


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
    assert "relaunch_with_agent" in action_ids
    assert "force_pass_advance" in action_ids

    relaunch = next(a for a in actions if a.action_id == "relaunch_with_agent")
    assert "--supersedes wf-001-plan-spin" in relaunch.command_line
    # Suggested agent should not be the failed one (qodercli)
    assert "--agent qodercli" not in relaunch.command_line


def test_build_cli_command_formatting():
    cmd = build_cli_command("launch", {
        "workflow-id": "wf-100",
        "stage": "implementation",
        "agent": "codex",
        "supersedes": "wf-100-old",
        "prompt": "修复缺陷: A & B",
    })
    assert cmd.startswith("bin/herdr-task launch")
    assert "--workflow-id wf-100" in cmd
    assert "--stage implementation" in cmd
    assert "--agent codex" in cmd
    assert "--supersedes wf-100-old" in cmd
    assert '--prompt "修复缺陷: A & B"' in cmd
