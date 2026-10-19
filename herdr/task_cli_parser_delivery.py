"""Argument definitions for the delivery command family."""

import argparse


def add_delivery_parsers(sub):
    report_cmd = sub.add_parser("delivery-report")
    report_cmd.add_argument("workflow_id")

    renew_cmd = sub.add_parser("renew-completion")
    renew_cmd.add_argument("task_id")
    renew_cmd.add_argument("--operation-id", required=True)

    delivery_check = sub.add_parser("delivery-check", help="Validate pinned repository delivery before completion")
    delivery_check.add_argument("task_id")

    receipt_cmd = sub.add_parser("report-completion")
    receipt_cmd.add_argument("task_id")
    receipt_cmd.add_argument("--identity-file", required=True)
    receipt_cmd.add_argument("--artifact", action="append", default=[], metavar="OBSERVATION_ID:SHA256")

    cp_create_cmd = sub.add_parser("checkpoint-create")
    cp_create_cmd.add_argument("workflow_id")
    cp_create_cmd.add_argument("--tag", default="", help="快照人类可读标签")
    cp_create_cmd.add_argument("--parent", default=None, help="父检查点 ID")

    cp_list_cmd = sub.add_parser("checkpoint-list")
    cp_list_cmd.add_argument("workflow_id")
    cp_list_cmd.add_argument("--json", action="store_true", help="输出完整 JSON 列表")

    cp_restore_cmd = sub.add_parser("checkpoint-restore")
    cp_restore_cmd.add_argument("workflow_id")
    cp_restore_cmd.add_argument("checkpoint_id")

    cp_fork_cmd = sub.add_parser("checkpoint-fork")
    cp_fork_cmd.add_argument("checkpoint_id")
    cp_fork_cmd.add_argument("new_workflow_id")
    cp_fork_cmd.add_argument("--title", default=None, help="新工作流标题")

    freeze_cmd = sub.add_parser(
        "freeze-candidate",
        help="Freeze or re-freeze candidate SHA for workflow verifiers",
    )
    freeze_cmd.add_argument("workflow_id")
    freeze_cmd.add_argument("--candidate-sha", required=True, help="Candidate commit SHA to freeze")
    freeze_cmd.add_argument("--delivery-branch", default="", help="Delivery branch name")
    freeze_cmd.add_argument("--source-node", default="", help="Source stage/node that produced this candidate")

    note_add_cmd = sub.add_parser(
        "note-add",
        help="向 workflow 共享文档区追加一条记录（append-only，代码隔离文档共享）",
    )
    note_add_cmd.add_argument("--workflow-id", required=True)
    from herdr.workflow_docs import NOTE_KINDS
    note_add_cmd.add_argument("--kind", required=True, choices=NOTE_KINDS)
    note_add_cmd.add_argument("--title", required=True)
    note_body = note_add_cmd.add_mutually_exclusive_group()
    note_body.add_argument("--text", default=None, help="条目正文")
    note_body.add_argument("--file", default=None, help="从文件读取正文（与 --text 二选一）")
    note_add_cmd.add_argument("--node", default=None, help="来源节点 ID")
    note_add_cmd.add_argument("--task", default=None, help="来源任务 ID（自动补全 agent/base_sha）")
    note_add_cmd.add_argument("--agent", default=None)
    note_add_cmd.add_argument(
        "--source",
        default="agent",
        choices=["agent", "human"],
        help="controller 机器证据由门禁 verdict 自动落盘，CLI 不接受该来源",
    )
    note_add_cmd.add_argument("--base-sha", default=None)
    note_add_cmd.add_argument("--round", type=int, default=1)
    note_add_cmd.add_argument(
        "--invalidates",
        action="append",
        default=None,
        help="仅 kind=invalidation：作废哪些节点的历史条目",
    )
    note_add_cmd.add_argument(
        "--field",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=(
            "结构化字段（可重复），原样透传给 append_note(fields=...) 并由"
            "其白名单校验。用于决策契约：--field decision_id=DU-10 "
            "--field decision_status=open --field question='...' "
            "--field options='[\"入\",\"不入\"]' --field recommended=入"
        ),
    )

    note_list_cmd = sub.add_parser("note-list", help="查看 workflow 共享文档区条目")
    note_list_cmd.add_argument("workflow_id")
    note_list_cmd.add_argument("--node", default=None)
    note_list_cmd.add_argument("--limit", type=int, default=0)
    note_list_cmd.add_argument("--current-base-sha", default=None)
    note_list_cmd.add_argument("--json", action="store_true")

    delivery_cmd = sub.add_parser(
        "record-delivery",
        help="记录 workflow 唯一交付身份 (FR-4.1: delivery_branch/candidate_sha/review_task/test_gate)",
    )
    delivery_cmd.add_argument("--workflow-id", required=True)
    delivery_cmd.add_argument("--delivery-branch", required=True)
    delivery_cmd.add_argument("--candidate-sha", required=True)
    delivery_cmd.add_argument("--review-task", required=True)
    delivery_cmd.add_argument("--test-gate", required=True)
    delivery_cmd.add_argument("--base", default="")
    delivery_cmd.add_argument("--node", default="wrapup")
    delivery_cmd.add_argument(
        "--supersedes", default=None,
        help="explicit prior delivery identity replaced by this candidate",
    )

    node_config = sub.add_parser("node-config", help="读取可审计配置及修改所需SHA")
    node_config.add_argument("workflow_id")
    node_config.add_argument("node")
    node_config_set = sub.add_parser("node-config-set", help="校验并更新本workflow的required_task_ids，不修改共享项目文件")
    node_config_set.add_argument("workflow_id")
    node_config_set.add_argument("node")
    node_config_set.add_argument("--expected-sha", required=True)
    node_config_set.add_argument("--reason", required=True)
    required = node_config_set.add_mutually_exclusive_group(required=True)
    required.add_argument("--required-task-id", action="append")
    required.add_argument("--clear-required-tasks", action="store_true")
    authorize = sub.add_parser("authorize-completion", help="为legacy任务授权持久完成声明契约；不认定任务已完成")
    authorize.add_argument("task_id")
    authorize.add_argument("--run-id", required=True)
    authorize.add_argument("--expected-version", type=int, required=True)
    authorize.add_argument("--reason", required=True)

    pr_cmd = sub.add_parser("create-pr", help="平台为已集成且验收通过的候选推送并创建待审查PR")
    pr_cmd.add_argument("task_id")
    pr_cmd.add_argument("--title", required=True)
    pr_cmd.add_argument("--body-file")
    pr_cmd.add_argument("--draft", action="store_true")

    merge_pr_cmd = sub.add_parser("merge-pr", help="门禁裁决候选校验后合并PR (fail-closed 对齐 candidate_sha)")
    merge_pr_cmd.add_argument("task_id")
    merge_pr_cmd.add_argument("--number", type=int, default=None, help="PR number")
    merge_pr_cmd.add_argument("--method", default="merge", choices=["merge", "squash", "rebase"], help="Merge method")

    check_ob_cmd = sub.add_parser("check-obligations", help="扫描任务状态机僵尸 obligation (replacement_pending 无替代者)")
    check_ob_cmd.add_argument("--workflow-id", default=None)
    check_ob_cmd.add_argument("--json", action="store_true")

    check_delivery_cmd = sub.add_parser(
        "check-delivery",
        help="建 PR 前校验: 内容等价 + 四要素模板 + 同分支已合入告警 (FR-4.2/4.3/4.4, 告警不阻断)",
    )
    check_delivery_cmd.add_argument("--workflow-id", required=True)
    check_delivery_cmd.add_argument("--head-sha", required=True)
    check_delivery_cmd.add_argument("--head-ref", default="")
    check_delivery_cmd.add_argument(
        "--repo-path", default=None,
        help="GitHub repository working tree for merged-PR and tree-equivalence evidence",
    )
