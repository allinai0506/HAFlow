"""Argument definitions for the workflow command family."""

import argparse


def add_workflow_parsers(sub):
    reopen_wf = sub.add_parser("reopen-workflow")
    reopen_wf.add_argument("workflow_id")

    get = sub.add_parser("get")
    get.add_argument("task_id")

    set_cmd = sub.add_parser("set")
    set_cmd.add_argument("task_id")
    set_cmd.add_argument("status")
    set_cmd.add_argument(
        "--verdict",
        choices=["pass", "blocked"],
        default=None,
        help="Gate-stage acceptance verdict, stored as stage_verdict "
             "(only valid with 'completed'; 'blocked' requires --note)",
    )
    set_cmd.add_argument(
        "--note",
        default=None,
        help="Blocker list / verdict detail, stored as stage_verdict_note",
    )
    set_cmd.add_argument(
        "--affected-task-id",
        dest="affected_task_ids",
        action="append",
        default=None,
        help="Implementation task ID explicitly blamed by a blocked gate "
             "verdict (repeatable; only valid with --verdict blocked; "
             "stored as stage_verdict_affected_task_ids for PR #110 "
             "selective replan)",
    )
    set_cmd.add_argument(
        "--skip-review-closure",
        action="store_true",
        default=False,
        help="Audited emergency bypass for the wrapup review-closure receipt "
             "gate (requires --note, --operator and the HERDR_OPERATOR=1 "
             "capability; records a review_closure_skipped event)",
    )
    set_cmd.add_argument(
        "--operator",
        default=None,
        help="Audited operator identity for --skip-review-closure (must be "
             "independent of the task worker; requires HERDR_OPERATOR=1)",
    )
    set_cmd.add_argument(
        "--operator-token",
        default=None,
        help="Authenticated operator token for --skip-review-closure",
    )
    set_cmd.add_argument(
        "--operator-credentials",
        default=None,
        help="Path to file containing operator token (must be outside task clone)",
    )

    adopt_verdict_cmd = sub.add_parser(
        "adopt-verdict",
        help="从门禁结论文件采纳裁决并完成门禁任务（支持从 failed 状态恢复）",
    )
    adopt_verdict_cmd.add_argument("task_id")
    adopt_verdict_cmd.add_argument("--verdict", choices=["pass", "blocked"], default=None, help="Explicit verdict override")
    adopt_verdict_cmd.add_argument("--note", default=None, help="Explicit note override")
    adopt_verdict_cmd.add_argument("--operator", default="human", help="Audited operator identity")
    adopt_verdict_cmd.add_argument("--json", action="store_true", help="Output result as JSON")

    closure_cmd = sub.add_parser(
        "record-review-closure",
        help="记录 wrapup 审核闭环回执（PR URL / CI / findings；完成转换会 fail-closed 校验）",
    )
    closure_cmd.add_argument("task_id")
    closure_cmd.add_argument("--pr-url", default=None, help="交付 PR URL（未 deferred 时必填）")
    closure_cmd.add_argument("--ci-status", default=None, help="CI/必过检查结果（如 pass / fail / pending）")
    closure_cmd.add_argument("--findings", default=None, help="评审 findings 结论（未决 P1/P2 数量与处置）")
    closure_cmd.add_argument("--head-sha", default=None, help="被审（闭环）的 40-hex 提交 SHA；head 变更后回执即失效（必填）")
    closure_cmd.add_argument("--unresolved-p1", type=int, default=None, help="结构化：审核后未决 P1 数（非 deferred 必填，必须为 0）")
    closure_cmd.add_argument("--unresolved-p2", type=int, default=None, help="结构化：未决 P2 数（非 deferred 必填）")
    closure_cmd.add_argument("--deferred", action="store_true", help="确实无法创建 PR 时记录延迟与证据")
    closure_cmd.add_argument("--reason", default=None, help="--deferred 必填：阻断证据（命令+错误输出）")

    bypass_gate_cmd = sub.add_parser("bypass-gate", help="结构化记录门禁豁免凭证，解除任务挂起/阻塞")
    bypass_gate_cmd.add_argument("task_id", help="目标任务 ID")
    bypass_gate_cmd.add_argument("--reason", required=True, help="门禁豁免原因（必填审计凭据）")
    bypass_gate_cmd.add_argument("--operator", default="human", help="操作人标识（默认 human）")
    bypass_gate_cmd.add_argument("--token", "--operator-token", dest="token", default=None, help="操作人凭据令牌 (HMAC / Bearer Token)")
    bypass_gate_cmd.add_argument("--credentials", "--operator-credentials", dest="credentials_path", default=None, help="操作人凭据文件路径")
    bypass_gate_cmd.add_argument("--note", default=None, help="附加说明")
    bypass_gate_cmd.add_argument("--expected-version", type=int, default=None, help="目标任务期望版本号 (CAS 乐观锁)")
    bypass_gate_cmd.add_argument("--json", action="store_true", help="以 JSON 格式输出")

    stage_cmd = sub.add_parser("stage-status")
    stage_cmd.add_argument("workflow_id")
    stage_cmd.add_argument("stage")

    node_cmd = sub.add_parser("node-status")
    node_cmd.add_argument("workflow_id")
    node_cmd.add_argument("node")

    ops_center_cmd = sub.add_parser("ops-center")
    ops_center_cmd.add_argument("--workflow-id", default=None)
    ops_center_cmd.add_argument("--include-tasks", action="store_true")

    ensure_cmd = sub.add_parser("ensure-runtime")
    ensure_cmd.add_argument("workflow_id")
    ensure_cmd.add_argument("node")

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--workflow-id")

    supersede_cmd = sub.add_parser(
        "supersede",
        help="supersede a task while retaining its replacement obligation; "
             "use --by to link a replacement or --abandon to explicitly remove the obligation",
    )
    supersede_cmd.add_argument("task_id")
    supersede_cmd.add_argument("--by", dest="new_task_id", default=None)
    supersede_cmd.add_argument("--reason", default=None)
    supersede_cmd.add_argument("--abandon", action="store_true",
                               help="Explicitly remove this task obligation; requires --reason and no --by")
    supersede_cmd.add_argument("--allow-pending", action="store_true",
                               help="Allow unlinked supersede pending redispatch (controller orchestration only)")

    clear_esc_cmd = sub.add_parser(
        "clear-escalation",
        help="revoke machine-set finalize_escalated so finalize re-drives "
             "(H-1; clears flag plus finalize attention episode; re-driven "
             "integrate may re-conflict and re-escalate; differs from "
             "supersede which discards commits and --accept-escalated which "
             "keeps them for close)",
    )
    clear_esc_cmd.add_argument("task_id")

    stage_reset_cmd = sub.add_parser("stage-reset")
    stage_reset_cmd.add_argument("workflow_id")
    stage_reset_cmd.add_argument("stage", nargs="?", default=None)

    advance_cmd = sub.add_parser("advance")
    advance_cmd.add_argument("workflow_id")
    advance_cmd.add_argument("stage", nargs="?", default=None)

    pause_cmd = sub.add_parser("pause")
    pause_cmd.add_argument("task_id")
    pause_cmd.add_argument("--reason", default=None)

    resume_cmd = sub.add_parser("resume")
    resume_cmd.add_argument("task_id")

    halt_cmd = sub.add_parser("halt")
    halt_cmd.add_argument("task_id")
    halt_cmd.add_argument("--reason", default="human interrupt")
    halt_cmd.add_argument("--operator", default="human")

    steer_cmd = sub.add_parser("steer")
    steer_cmd.add_argument("task_id")
    steer_cmd.add_argument("instruction")
    steer_cmd.add_argument("--urgent", action="store_true", default=False)
    steer_cmd.add_argument("--operator", default="human")

    steer_queue_cmd = sub.add_parser("steer-queue")
    steer_queue_cmd.add_argument("task_id")

    adapters_cmd = sub.add_parser("adapters", help="查看各 AgentAdapter 运行时能力矩阵 (TTY Prototype)")
    adapters_cmd.add_argument("--json", action="store_true", help="输出完整 JSON 数据")

    project_cmd = sub.add_parser("project")
    project_cmd.add_argument("task_id")
    project_cmd.add_argument("--json", action="store_true", help="输出完整 JSON 数据")

    artifacts_cmd = sub.add_parser("artifacts")
    artifacts_cmd.add_argument("task_id")
    artifacts_cmd.add_argument("--json", action="store_true", help="输出完整 JSON 数据")
