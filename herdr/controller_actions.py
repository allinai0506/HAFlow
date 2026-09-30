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
    category: str  # "fix", "rework", "bypass", "recovery", "pipeline"
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
    # Which console section this belongs to: "blocker" (something is wrong)
    # or "pipeline" (nothing is wrong, the task just needs its next step).
    group: str = "blocker"
    # Executable argv for the console, relative to ``command_base``.  Empty
    # means the action is served by ``api_endpoint``/``api_payload`` only.
    commands: List[List[str]] = field(default_factory=list)
    command_base: str = "herdr-task"

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


# ============================================================
# Pipeline / recovery actions
#
# `generate_controller_actions` above only fires when something is *wrong*
# (blocked / failed / rework).  A healthy task also has exactly one
# mechanical next step — finish the git finalization chain, revoke a
# finalize escalation, or re-drive a live pane.  Without these the console
# shows "no blockers" while the delivery pipeline is actually waiting.
# ============================================================

#: Task statuses that still owe the delivery pipeline their next step.
#: ``(status, step_name, subcommand_argvs, label, needs_git)`` — each subcommand
#: argv is prefixed with the task id when executed.  Order follows
#: herdr/transitions.py TASK_TRANSITIONS.
#:
#: ``needs_git`` is deliberately per-step, not per-table: ``commit``/``integrate``
#: exit 2 for a non-git or context-mode task (bin/herdr-task commit_task /
#: integrate_task guards), but ``finalize``/``cleanup`` only require a settled
#: status (TEARDOWN_BLOCKING_STATUSES).  Gating the whole table on git would
#: leave the majority non-git task class with no button at all.
GIT_PIPELINE_FORWARD = (
    ("agent_done", "accept_and_commit", (("set", "completed"), ("commit",)),
     "验收通过并提交交付", True),
    ("completed", "commit", (("commit",),),
     "提交交付产物", True),
    ("committed", "integrate", (("integrate",),),
     "集成到基线分支", True),
    ("integrated", "finalize", (("finalize",),),
     "收尾并清理现场", False),
    ("cleanup_ready", "finalize", (("finalize",),),
     "收尾并清理现场", False),
    ("cleanup_ready", "cleanup", (("cleanup",),),
     "标记归档完成", False),
)

#: Statuses where a live pane can still be re-driven with a real prompt.
LIVE_PANE_STATUSES = frozenset({
    "dispatched", "working", "blocked", "rework", "paused", "interrupted",
})

#: Statuses that are already settled or not yet started: no action to offer.
UNACTIONABLE_STATUSES = frozenset({"pending", "failed", "cleaned", "superseded"})

REDRIVE_DEFAULT = (
    "继续当前任务：请先确认是否已完成全部验收项。"
    "若已完成，请在终端单独输出一行 HERDR_TASK_DONE:<task_id>；"
    "若未完成，请继续就地解决，不要新建任务。"
)
STEER_DEFAULT = "请继续推进当前任务并在完成后输出 HERDR_TASK_DONE"


def _pipeline_effect(status, task_id):
    return {
        "accept_and_commit": (
            f"把 {task_id} 标记为验收通过并立即提交交付 commit，"
            f"让 Controller 继续走集成链路。"
        ),
        "commit": f"为 {task_id} 生成并登记交付 commit，进入待集成状态。",
        "integrate": (
            f"把 {task_id} 的交付分支 rebase 到基线并合并，"
            f"使其真正进入目标分支（幂等，已集成则返回 already_integrated）。"
        ),
        "finalize": f"对 {task_id} 执行收尾：留证据、关工位、推进到已归档。",
        "cleanup": f"把 {task_id} 从待归档标记为已归档（逻辑清理，保留工位与代码目录）。",
    }[status]


def _git_pipeline_actions(task, wid):
    """The task's next step in the delivery pipeline, if it owes one."""
    status = task.get("status") or ""
    tid = task.get("task_id") or ""
    if not tid:
        return []

    is_git = (task.get("integration_mode") or "none") == "git"
    is_context = task.get("execution_mode") == "context"

    actions = []
    for match_status, name, steps, label, needs_git in GIT_PIPELINE_FORWARD:
        if status != match_status:
            continue
        if needs_git and (not is_git or is_context):
            # commit / integrate exit 2 without a git clone (bin/herdr-task
            # commit_task / integrate_task guards).  finalize / cleanup do not
            # care, so they are gated per-step rather than per-table.
            continue
        # A status can carry more than one legitimate next step (cleanup_ready
        # accepts both finalize and cleanup), but only the first is the
        # recommendation so the console never shows two competing defaults.
        recommended = not actions
        commands = [[step[0], tid, *step[1:]] for step in steps]
        actions.append(ControllerAction(
            action_id=f"{tid}:{name}",
            title=label,
            description=(
                f"{tid} 当前处于 {status}，这是它在交付链路里的下一步；"
                f"点击后由 Controller 直接执行对应命令。"
            ),
            category="pipeline",
            command_line=" && ".join(
                build_cli_command(cmd[0], positionals=cmd[1:])
                for cmd in commands
            ),
            api_endpoint="/api/controller/execute-action",
            api_payload={
                "type": "task_git_step",
                "task_id": tid,
                "workflow_id": wid,
                "step": name,
            },
            recommended=recommended,
            blocker_task_id=tid,
            effect=_pipeline_effect(name, tid),
            stage=task.get("stage") or task.get("node") or "",
            group="pipeline",
            commands=commands,
        ))
    return actions


def _escalation_actions(task, wid):
    """Human routes for a machine-set finalize escalation (H-1)."""
    tid = task.get("task_id") or ""
    if not tid or not task.get("finalize_escalated") or not wid:
        return []
    reason = str(task.get("finalize_escalate_reason") or "").strip() or "终化冲突"
    return [
        ControllerAction(
            action_id=f"{tid}:clear_escalation",
            title="解除升级锁并重新收尾",
            description=(
                f"撤销机器设置的 finalize_escalated 锁并重新驱动 integrate"
                f"（{reason}）。保留交付物；可能再次冲突并再次升级。"
            ),
            category="recovery",
            command_line=build_cli_command("clear-escalation", positionals=[tid]),
            api_endpoint="/api/controller/execute-action",
            api_payload={"type": "clear_escalation", "task_id": tid, "workflow_id": wid},
            recommended=True,
            blocker_task_id=tid,
            effect=f"清除 {tid} 的终化升级锁与对应告警，重新驱动集成；交付物不丢。",
            stage=task.get("stage") or task.get("node") or "",
            group="pipeline",
            commands=[["clear-escalation", tid]],
        ),
        ControllerAction(
            action_id=f"{tid}:close_workflow_accept_escalated",
            title="确认无误，保留交付并关闭工作流",
            description=(
                f"人工确认 {reason} 不影响交付，仅对机器升级标记放行，"
                f"保留交付物关闭本工作流。"
            ),
            category="bypass",
            command_line=build_cli_command(
                "close-workflow", positionals=[wid, "--accept-escalated"],
            ),
            api_endpoint="/api/controller/execute-action",
            api_payload={
                "type": "close_workflow",
                "workflow_id": wid,
                "task_id": tid,
                "accept_escalated": True,
            },
            is_destructive=True,
            blocker_task_id=tid,
            effect=f"带 --accept-escalated 关闭 {wid}；保留 {tid} 交付物，仅放行机器升级标记。",
            stage=task.get("stage") or task.get("node") or "",
            group="pipeline",
            commands=[["close-workflow", wid, "--accept-escalated"]],
        ),
        ControllerAction(
            action_id=f"{tid}:supersede",
            title="丢弃该任务分支并作废",
            description="永久丢弃该任务的分支提交并作废任务（不可回溯）。",
            category="bypass",
            command_line=build_cli_command(
                "supersede", positionals=[tid],
                flags={"reason": "human superseded from console"},
            ),
            api_endpoint="/api/controller/execute-action",
            api_payload={
                "type": "supersede",
                "task_id": tid,
                "workflow_id": wid,
                "reason": "human superseded from console",
            },
            is_destructive=True,
            blocker_task_id=tid,
            effect=f"作废 {tid} 并丢弃其分支提交；该任务的产物不会进入基线。",
            old_task_id=tid,
            stage=task.get("stage") or task.get("node") or "",
            group="pipeline",
            commands=[["supersede", tid, "--reason", "human superseded from console"]],
        ),
    ]


def _live_pane_actions(task, wid):
    """Real re-drive / steer / halt for a task with a live pane."""
    status = task.get("status") or ""
    tid = task.get("task_id") or ""
    pane_id = str(task.get("pane_id") or "").strip()
    if not tid or status not in LIVE_PANE_STATUSES:
        return []

    actions = []
    if pane_id:
        message = REDRIVE_DEFAULT.replace("<task_id>", tid)
        actions.append(ControllerAction(
            action_id=f"{tid}:redrive",
            title="真实 re-drive：直接向工位重推提示",
            description=(
                f"向工位 {pane_id} 直接重发一条提示（herdr agent prompt），"
                f"让执行者自己收敛到完成态。这是真实重驱，不是 steer 插话队列。"
            ),
            category="recovery",
            command_line=" ".join(
                shlex.quote(part) for part in
                ["herdr", "agent", "prompt", pane_id, message,
                 "--wait", "--timeout", "180000"]
            ),
            api_endpoint="/api/controller/execute-action",
            api_payload={
                "type": "redrive",
                "task_id": tid,
                "workflow_id": wid,
                "pane_id": pane_id,
                "instruction": message,
            },
            blocker_task_id=tid,
            effect=(
                f"向工位 {pane_id} 重推一条真实提示并等待回执；"
                f"不改任务状态，状态由 Controller 与工位完成标记自然收敛。"
            ),
            stage=task.get("stage") or task.get("node") or "",
            group="pipeline",
            commands=[["agent", "prompt", pane_id, message,
                       "--wait", "--timeout", "180000"]],
            command_base="herdr",
        ))
        actions.append(ControllerAction(
            action_id=f"{tid}:steer",
            title="插话指导（进入 steer 队列）",
            description=(
                "把一条指导写入 steer 队列，由 Worker 在下一个轮询点读取；"
                "不会立刻打断当前执行。"
            ),
            category="fix",
            command_line=build_cli_command("steer", positionals=[tid, STEER_DEFAULT]),
            api_endpoint="/api/controller/execute-action",
            api_payload={
                "type": "steer",
                "task_id": tid,
                "workflow_id": wid,
                "instruction": STEER_DEFAULT,
            },
            blocker_task_id=tid,
            effect=f"向 {tid} 的 steer 队列写入一条指导；下一个轮询点生效，不改状态。",
            stage=task.get("stage") or task.get("node") or "",
            group="pipeline",
            commands=[["steer", tid, STEER_DEFAULT]],
        ))

    actions.append(ControllerAction(
        action_id=f"{tid}:halt",
        title="紧急制动该任务",
        description="中断该工位并把任务置为需人工处置的终态。",
        category="recovery",
        command_line=build_cli_command("halt", positionals=[tid]),
        api_endpoint="/api/controller/execute-action",
        api_payload={"type": "halt", "task_id": tid, "workflow_id": wid},
        is_destructive=True,
        blocker_task_id=tid,
        effect=f"中断 {tid} 的执行；现场保留，需要你随后决定重派或作废。",
        stage=task.get("stage") or task.get("node") or "",
        group="pipeline",
        commands=[["halt", tid]],
    ))
    return actions


def generate_progress_actions(task, workflow=None):
    """Next mechanical step for a task that is healthy but not finished.

    Complements :func:`generate_controller_actions` (which only fires on
    blockers).  Returns ``[]`` for settled or not-yet-started tasks.
    """
    task = task if isinstance(task, dict) else {}
    workflow = workflow if isinstance(workflow, dict) else {}
    tid = str(task.get("task_id") or "").strip()
    if not tid:
        return []

    status = str(task.get("status") or "").strip()
    if status in UNACTIONABLE_STATUSES:
        return []
    wid = str(workflow.get("workflow_id") or task.get("workflow_id") or "").strip()

    actions: List[ControllerAction] = []
    if task.get("finalize_escalated"):
        actions.extend(_escalation_actions(task, wid))
    actions.extend(_git_pipeline_actions(task, wid))
    if not actions:
        actions.extend(_live_pane_actions(task, wid))
    return actions


def collect_workflow_actions(tasks, workflow, project_root="", workflow_paused=False):
    """All console actions for a workflow: blockers first, then pipeline steps.

    ``action_id`` is unique across the whole result, so the console can key
    its button map on it directly.
    """
    workflow = workflow if isinstance(workflow, dict) else {}
    wid = str(workflow.get("workflow_id") or "").strip()
    actions: List[ControllerAction] = []
    seen = set()

    def _add(action):
        if action.action_id in seen:
            return
        seen.add(action.action_id)
        actions.append(action)

    # Blocker actions come only from real blockers.  generate_controller_actions
    # always appends the destructive force_pass_advance fallback, so calling it
    # for every task would offer "强制放行" on cleaned / superseded history.
    for task in resolve_workflow_blockers(tasks or [], workflow):
        if not isinstance(task, dict):
            continue
        for action in generate_controller_actions(task, workflow, project_root=project_root):
            _add(action)

    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for action in generate_progress_actions(task, workflow):
            _add(action)

    if workflow_paused and wid:
        _add(ControllerAction(
            action_id=f"{wid}:resume_workflow",
            title="恢复工作流调度",
            description=(
                "该工作流当前处于 paused：Controller 不会派发新任务、"
                "也不会推进门禁。恢复后按原 DAG 继续。"
            ),
            category="recovery",
            command_line="",
            api_endpoint="/api/kernel/resume",
            api_payload={"workflow_id": wid, "node_id": None},
            recommended=True,
            blocker_task_id="",
            effect=f"把 {wid} 从 paused 恢复为可调度；不会重跑已完成任务。",
            stage="",
            group="pipeline",
            commands=[],
        ))
    return actions
