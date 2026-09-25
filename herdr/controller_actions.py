#!/usr/bin/env python3
"""Herdr Controller Action Resolution Engine.

Responsible for:
1. Detecting active blockers in a workflow (filtering out superseded/historical records).
2. Generating actionable, structured resolutions (ControllerAction) with exact CLI commands.
3. Providing payload mapping for direct frontend execution.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


AGENT_CANDIDATE_POOL = ["codex", "claude", "opencode", "qodercli", "agy"]
REPLACEMENT_SUFFIX_RE = re.compile(r"-r(\d+)$")


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
    # Task binding for end-user console: which blocked task this resolves,
    # and a plain-language consequence shown on buttons (no CLI needed).
    blocker_task_id: str = ""
    effect: str = ""
    old_task_id: str = ""
    new_task_id: str = ""
    new_agent: str = ""
    stage: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def next_replacement_id(old_task_id: str, default_fallback: str = "task-r2") -> str:
    """Generate next replacement task ID following the -rN lineage convention."""
    if not old_task_id:
        return default_fallback
    match = REPLACEMENT_SUFFIX_RE.search(old_task_id)
    if match:
        base = old_task_id[:match.start()]
        index = int(match.group(1)) + 1
        return f"{base}-r{index}"
    return f"{old_task_id}-r2"


def build_cli_command(
    subcmd: str,
    flags: Dict[str, Any] | None = None,
    positionals: List[str] | None = None,
) -> str:
    """Format a standard bin/herdr-task CLI invocation with safe shell quoting."""
    parts = ["bin/herdr-task", subcmd]
    if positionals:
        for pos in positionals:
            parts.append(shlex.quote(str(pos)))
    if flags:
        for key, val in flags.items():
            if val is None or val is False:
                continue
            flag_name = f"--{key}"
            if val is True:
                parts.append(flag_name)
            else:
                parts.append(f"{flag_name} {shlex.quote(str(val))}")
    return " ".join(parts)


def resolve_workflow_blockers(tasks: List[Dict[str, Any]], workflow: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Filter tasks to find only currently active blocking items.

    Strictly ignores superseded tasks (already replaced by newer rounds)
    and successfully completed/cleaned tasks.
    """
    blockers = []
    for t in tasks:
        status = t.get("status")
        # Invariant: Superseded and cleaned tasks are historical artifacts and NEVER active blockers
        if status in {"superseded", "cleaned"}:
            continue

        verdict = t.get("stage_verdict")
        blocker_field = t.get("blocker")
        has_blocker_reason = bool(blocker_field and len(str(blocker_field).strip()))

        is_blocked = (
            verdict == "blocked"
            or status in {"blocked", "failed", "rework"}
            or has_blocker_reason
        )

        # Skip completed/committed/integrated tasks unless explicitly marked with an unresolved blocked verdict
        if status in {"completed", "committed", "integrated"} and verdict != "blocked":
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
        fix_task_id = f"{wid}-impl-fix"
        fix_prompt = f"根据测试发现的问题执行修复与自测: {verdict_note[:180]}" if verdict_note else "修复测试发现的缺陷"
        fix_flags = {
            "task-id": fix_task_id,
            "workflow-id": wid,
            "stage": "implementation",
            "source": proj_root,
            "agent": alt_agent,
            "goal": "执行测试缺陷修复",
            "prompt": fix_prompt,
        }
        actions.append(
            ControllerAction(
                action_id=f"{tid}:dispatch_fix_loop" if tid else "dispatch_fix_loop",
                title="派发 Fix-Loop 修复任务",
                description="针对测试捕获的阻塞缺陷，由实现节点启动修复循环。",
                category="fix",
                command_line=build_cli_command("launch", fix_flags),
                api_endpoint="/api/controller/execute-action",
                api_payload={
                    "type": "launch",
                    "task_id": fix_task_id,
                    "workflow_id": wid,
                    "stage": "implementation",
                    "agent": alt_agent,
                    "goal": "执行测试缺陷修复",
                    "prompt": fix_prompt,
                    "source": proj_root,
                },
                recommended=True,
                blocker_task_id=tid,
                effect=f"在实现阶段新建修复任务 {fix_task_id}（执行者 {alt_agent}），修完自动回测；原失败测试任务 {tid or '—'} 保留备查，无需你敲命令。",
                old_task_id="",
                new_task_id=fix_task_id,
                new_agent=alt_agent,
                stage="implementation",
            )
        )

        # Action B: Relaunch test with alternative agent
        retest_task_id = next_replacement_id(tid, f"{wid}-test-r2")
        retest_flags = {
            "task-id": retest_task_id,
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
                action_id=f"{tid}:retest_with_agent" if tid else "retest_with_agent",
                title=f"换执行者 ({alt_agent}) 重新测试",
                description=f"作废当前失败测试，使用执行者 {alt_agent} 重新执行测试门禁。",
                category="rework",
                command_line=build_cli_command("launch", retest_flags),
                api_endpoint="/api/controller/execute-action",
                api_payload={
                    "type": "launch",
                    "task_id": retest_task_id,
                    "workflow_id": wid,
                    "stage": "test",
                    "agent": alt_agent,
                    "supersedes": tid,
                    "goal": f"使用 {alt_agent} 重新执行测试验证",
                    "prompt": "重新运行测试套件并出具完整验证报告",
                    "source": proj_root,
                },
                blocker_task_id=tid,
                effect=f"将作废旧测试任务 {tid or '—'}，用 {alt_agent} 新建 {retest_task_id} 重跑测试门禁；旧任务标记取代，可回溯。",
                old_task_id=tid,
                new_task_id=retest_task_id,
                new_agent=alt_agent,
                stage="test",
            )
        )

    # 2. Scenario: Plan / Worker Rework / Empty Delivery / General Task Spin
    elif status in {"rework", "failed", "blocked"} or "DONE均零交付" in verdict_note or "重派" in verdict_note:
        relaunch_task_id = next_replacement_id(tid, f"{wid}-{stage}-r2")
        relaunch_flags = {
            "task-id": relaunch_task_id,
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
                action_id=f"{tid}:relaunch_with_agent" if tid else "relaunch_with_agent",
                title=f"换执行者 ({alt_agent}) 重派当前任务",
                description=f"作废当前卡点任务，切换至活跃度更高的执行者 {alt_agent} 重新执行。",
                category="rework",
                command_line=build_cli_command("launch", relaunch_flags),
                api_endpoint="/api/controller/execute-action",
                api_payload={
                    "type": "launch",
                    "task_id": relaunch_task_id,
                    "workflow_id": wid,
                    "stage": stage,
                    "agent": alt_agent,
                    "supersedes": tid,
                    "goal": task.get("goal") or f"重新推进 {stage} 阶段目标",
                    "prompt": f"重新执行并确保产物落盘: {task.get('goal', stage)}",
                    "source": proj_root,
                },
                recommended=True,
                blocker_task_id=tid,
                effect=f"将作废卡点任务 {tid or '—'}，用 {alt_agent} 新建 {relaunch_task_id} 重跑 {stage}；旧任务标记取代，可回溯。",
                old_task_id=tid,
                new_task_id=relaunch_task_id,
                new_agent=alt_agent,
                stage=stage,
            )
        )

    # 3. Always available fallback: Gate Force Pass / Stage Advance
    if tid:
        advance_cmd = f"bin/herdr-task set {shlex.quote(tid)} completed --verdict pass && bin/herdr-task advance {shlex.quote(wid)}"
    else:
        advance_cmd = build_cli_command("advance", positionals=[wid])
    actions.append(
        ControllerAction(
            action_id=f"{tid}:force_pass_advance" if tid else "force_pass_advance",
            title="门禁豁免 / 强制推进至下一阶段",
            description="总指挥人工核查无误后，豁免当前卡点并将工作流推进至后续阶段。",
            category="bypass",
            command_line=advance_cmd,
            api_endpoint="/api/controller/execute-action",
            api_payload={
                "type": "force_pass_advance",
                "workflow_id": wid,
                "stage": stage,
                "task_id": tid,
                "gate_node_id": stage,
            },
            is_destructive=True,
            recommended=False,
            blocker_task_id=tid,
            effect=f"将把卡点任务 {tid or '当前阶段'} 标记通过并尝试推进工作流到下一阶段；属高风险豁免，请确认已人工核查产物。",
            old_task_id=tid,
            new_task_id="",
            new_agent="",
            stage=stage,
        )
    )

    return actions
