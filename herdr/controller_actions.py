#!/usr/bin/env python3
"""Herdr Controller Action Resolution Engine.

Responsible for:
1. Detecting active blockers in a workflow (filtering out superseded/historical records).
2. Generating actionable, structured resolutions (ControllerAction) with exact CLI commands.
3. Providing payload mapping for direct frontend execution.
"""

from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


AGENT_CANDIDATE_POOL = ["codex", "claude", "opencode", "qodercli", "agy"]


@dataclass
class ControllerAction:
    """A structured resolution action recommended by Controller."""
    action_id: str
    title: str
    description: str
    category: str  # "fix", "rework", "bypass", "recovery"
    command_line: str
    api_endpoint: str
    api_payload: Dict[str, Any] = field(default_factory=dict)
    is_destructive: bool = False
    recommended: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_cli_command(subcmd: str, flags: Dict[str, Any]) -> str:
    """Format a standard bin/herdr-task CLI invocation with safe quoting."""
    parts = [f"bin/herdr-task {subcmd}"]
    for key, val in flags.items():
        if val is None or val is False:
            continue
        flag_name = f"--{key}"
        if val is True:
            parts.append(flag_name)
        else:
            str_val = str(val)
            # Quote if value contains spaces or special characters
            if any(ch in str_val for ch in ' \t\n\r"\'$;&|<>'):
                escaped = str_val.replace('"', '\\"')
                parts.append(f'{flag_name} "{escaped}"')
            else:
                parts.append(f"{flag_name} {str_val}")
    return " ".join(parts)


def resolve_workflow_blockers(tasks: List[Dict[str, Any]], workflow: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Filter tasks to find only currently active blocking items.

    Strictly ignores superseded tasks (already replaced by newer rounds)
    and successfully completed/cleaned tasks.
    """
    blockers = []
    for t in tasks:
        status = t.get("status")
        # Invariant: Superseded tasks are historical artifacts and NEVER active blockers
        if status == "superseded":
            continue

        verdict = t.get("stage_verdict")
        blocker_field = t.get("blocker")
        has_blocker_reason = bool(blocker_field and len(str(blocker_field).strip()))

        is_blocked = (
            verdict == "blocked"
            or status in {"blocked", "failed", "rework"}
            or has_blocker_reason
        )

        # Skip cleaned/completed tasks unless explicitly marked with an unresolved blocked verdict
        if status in {"completed", "cleaned", "committed", "integrated"} and verdict != "blocked":
            continue

        if is_blocked:
            blockers.append(t)

    return blockers


def pick_alternative_agent(current_agent: str) -> str:
    """Pick a suitable alternative agent from the roster."""
    for candidate in AGENT_CANDIDATE_POOL:
        if candidate != current_agent:
            return candidate
    return "codex"


def generate_controller_actions(
    task: Dict[str, Any],
    workflow: Dict[str, Any],
    project_root: str = "",
) -> List[ControllerAction]:
    """Generate structured controller actions for an active blocker."""
    actions: List[ControllerAction] = []
    tid = task.get("task_id", "")
    wid = workflow.get("workflow_id") or task.get("workflow_id", "")
    stage = task.get("stage") or task.get("node") or "implementation"
    current_agent = task.get("agent") or "auto"
    status = task.get("status", "")
    verdict_note = task.get("stage_verdict_note") or task.get("blocker") or ""
    proj_root = project_root or workflow.get("project_root") or "."

    alt_agent = pick_alternative_agent(current_agent)

    # 1. Scenario: Test Failure / Verification Defect (Fix-Loop required)
    if stage in {"test", "verification"} and (status == "failed" or "FAIL" in verdict_note or "defect" in verdict_note.lower()):
        # Action A: Dispatch Fix-Loop in implementation
        fix_prompt = f"根据测试发现的问题执行修复与自测: {verdict_note[:180]}" if verdict_note else "修复测试发现的缺陷"
        fix_flags = {
            "workflow-id": wid,
            "stage": "implementation",
            "source": proj_root,
            "agent": alt_agent,
            "goal": "执行测试缺陷修复",
            "prompt": fix_prompt,
        }
        actions.append(
            ControllerAction(
                action_id="dispatch_fix_loop",
                title="派发 Fix-Loop 修复任务",
                description="针对测试捕获的阻塞缺陷，由实现节点启动修复循环。",
                category="fix",
                command_line=build_cli_command("launch", fix_flags),
                api_endpoint="/api/controller/execute-action",
                api_payload={
                    "type": "launch",
                    "workflow_id": wid,
                    "stage": "implementation",
                    "agent": alt_agent,
                    "prompt": fix_prompt,
                },
                recommended=True,
            )
        )

        # Action B: Relaunch test with alternative agent
        retest_flags = {
            "workflow-id": wid,
            "stage": "test",
            "source": proj_root,
            "agent": alt_agent,
            "supersedes": tid,
            "goal": f"使用 {alt_agent} 重新执行测试验证",
            "prompt": "重新运行测试套件并出具完整验证报告",
        }
        actions.append(
            ControllerAction(
                action_id="relaunch_with_agent",
                title=f"换 Agent ({alt_agent}) 重新测试",
                description=f"作废当前失败测试，使用执行者 {alt_agent} 重新执行测试门禁。",
                category="rework",
                command_line=build_cli_command("launch", retest_flags),
                api_endpoint="/api/controller/execute-action",
                api_payload={
                    "type": "launch",
                    "workflow_id": wid,
                    "stage": "test",
                    "agent": alt_agent,
                    "supersedes": tid,
                },
            )
        )

    # 2. Scenario: Plan / Worker Rework / Empty Delivery / General Task Spin
    elif status in {"rework", "failed", "blocked"} or "DONE均零交付" in verdict_note or "重派" in verdict_note:
        relaunch_flags = {
            "workflow-id": wid,
            "stage": stage,
            "source": proj_root,
            "agent": alt_agent,
            "supersedes": tid,
            "goal": task.get("goal") or f"重新推进 {stage} 阶段目标",
            "prompt": f"重新执行并确保产物落盘: {task.get('goal', stage)}",
        }
        actions.append(
            ControllerAction(
                action_id="relaunch_with_agent",
                title=f"换 Agent ({alt_agent}) 重派当前任务",
                description=f"作废当前卡点任务，切换至活跃度更高的执行者 {alt_agent} 重新执行。",
                category="rework",
                command_line=build_cli_command("launch", relaunch_flags),
                api_endpoint="/api/controller/execute-action",
                api_payload={
                    "type": "launch",
                    "workflow_id": wid,
                    "stage": stage,
                    "agent": alt_agent,
                    "supersedes": tid,
                },
                recommended=True,
            )
        )

    # 3. Always available fallback: Gate Force Pass / Stage Advance
    advance_flags = {"workflow-id": wid}
    actions.append(
        ControllerAction(
            action_id="force_pass_advance",
            title="门禁豁免 / 强制推进至下一阶段",
            description="总指挥人工核查无误后，豁免当前卡点并将工作流推进至后续阶段。",
            category="bypass",
            command_line=build_cli_command("advance", advance_flags),
            api_endpoint="/api/workflow/advance",
            api_payload={"workflow_id": wid},
            is_destructive=True,
            recommended=False,
        )
    )

    return actions
