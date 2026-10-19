"""Argument definitions for the observability command family."""

import argparse


def add_observability_parsers(sub):
    observe_cmd = sub.add_parser(
        "observe",
        help="Trajectory Observer: 观察一个 Run 并输出结构化 Finding(只读旁路)",
    )
    observe_target = observe_cmd.add_mutually_exclusive_group(required=True)
    observe_target.add_argument("--run-id", default=None)
    observe_target.add_argument("--task-id", default=None)
    observe_cmd.add_argument("--json", action="store_true")
    observe_cmd.add_argument(
        "--no-model",
        action="store_true",
        help="只用确定性规则,不调用 Provider 确认(弱信号将被丢弃)",
    )

    compact_cmd = sub.add_parser(
        "compact",
        help="为一个 Run 生成 bounded、可追溯的 Semantic ContextPack",
    )
    compact_cmd.add_argument("--run-id", required=True)
    compact_cmd.add_argument("--task-id", default=None)
    compact_cmd.add_argument("--json", action="store_true")
    compact_cmd.add_argument("--no-model", action="store_true")

    metrics_cmd = sub.add_parser(
        "metrics",
        help="读取一个 Run 的 Harness Metrics（只读旁路）",
    )
    metrics_cmd.add_argument("--run-id", required=True)
    metrics_cmd.add_argument("--json", action="store_true")

    working_context_cmd = sub.add_parser(
        "working-context",
        help="按 context_id 读取不可变 WorkingContext（只读旁路）",
    )
    working_context_cmd.add_argument(
        "action", choices=["get"],
        help="读取动作：get 按 context_id 返回完整快照",
    )
    working_context_cmd.add_argument("--context-id", required=True)

    eval_cmd = sub.add_parser(
        "eval",
        help="评估一个 Run（只读；--record 落盘一条 eval 事实）",
    )
    eval_cmd.add_argument("--run-id", required=True)
    eval_cmd.add_argument("--json", action="store_true")
    eval_cmd.add_argument("--record", action="store_true")
    eval_cmd.add_argument("--revision", type=int, default=None)

    replay_cmd = sub.add_parser(
        "replay",
        help="从历史 Run 回放出新 Run（默认落盘；--dry-run 只规划不写）",
    )
    replay_cmd.add_argument("--source-run", required=True)
    replay_cmd.add_argument("--replay-run", default=None)
    replay_cmd.add_argument("--workflow-id", default=None)
    replay_cmd.add_argument("--task-id", default=None)
    replay_cmd.add_argument("--definition", default=None)
    replay_cmd.add_argument("--definition-file", default=None)
    replay_cmd.add_argument("--policy", default=None,
                            help="回放 policy override（JSON 对象字符串）")
    replay_cmd.add_argument("--policy-file", default=None,
                            help="回放 policy override（JSON 对象文件）")
    replay_cmd.add_argument("--agent", default=None)
    replay_cmd.add_argument("--goal", default=None)
    replay_cmd.add_argument("--prompt", default=None)
    replay_cmd.add_argument("--source", default="replay")
    replay_cmd.add_argument("--launch", action="store_true",
                            default=True, help="兼容参数：Replay 始终执行真实 launch 链")
    replay_cmd.add_argument("--run-preflight", action="store_true",
                            default=True, help="兼容参数：Replay 始终执行真实 preflight 探针")
    replay_cmd.add_argument("--dry-run", action="store_true")
    replay_cmd.add_argument("--json", action="store_true")

    eval_compare_cmd = sub.add_parser(
        "eval-compare",
        help="事实对比两条已落盘 eval（只读）",
    )
    eval_compare_cmd.add_argument("--before-run", required=True)
    eval_compare_cmd.add_argument("--before-revision", type=int, default=None)
    eval_compare_cmd.add_argument("--after-run", required=True)
    eval_compare_cmd.add_argument("--after-revision", type=int, default=None)
    eval_compare_cmd.add_argument("--json", action="store_true")

    route_shadow_cmd = sub.add_parser(
        "route-shadow",
        help="Adaptive Router v1 影子排名（只读，不派发）",
    )
    route_shadow_cmd.add_argument("--workflow-id", default=None)
    route_shadow_cmd.add_argument("--node", required=True)
    route_shadow_cmd.add_argument("--task-type", default="docs")
    route_shadow_cmd.add_argument(
        "--agents", default=None,
        help="逗号分隔候选集；缺省按项目池偏好顺序近似",
    )
    route_shadow_cmd.add_argument("--json", action="store_true")

    shadow_eval_cmd = sub.add_parser(
        "shadow-eval",
        help="Adaptive Router 影子评估（只读：frozen 预测 vs  settled outcome）",
    )
    shadow_eval_cmd.add_argument("--node", default=None)
    shadow_eval_cmd.add_argument("--task-type", default=None)
    shadow_eval_cmd.add_argument(
        "--agent", default=None,
        help="仅保留 actual 或 recommended 为该 Agent 的决策",
    )
    shadow_eval_cmd.add_argument(
        "--since", default=None,
        help="仅评估该时间之后的决策：7d / 24h 或 epoch 秒",
    )
    shadow_eval_cmd.add_argument(
        "--limit", default="1000",
        help="返回最多 N 条匹配决策；为寻找匹配项可能扫描更多历史记录",
    )
    shadow_eval_cmd.add_argument("--json", action="store_true")

    canary_eval_cmd = sub.add_parser(
        "canary-eval",
        help="Adaptive Router Canary 评估（只读：diverted vs legacy 两臂 observed 对比）",
    )
    canary_eval_cmd.add_argument("--node", default=None)
    canary_eval_cmd.add_argument("--task-type", default=None)
    canary_eval_cmd.add_argument(
        "--agent", default=None,
        help="仅保留 actual / recommended / legacy 为该 Agent 的决策",
    )
    canary_eval_cmd.add_argument(
        "--since", default=None,
        help="仅评估该时间之后的决策：7d / 24h 或 epoch 秒",
    )
    canary_eval_cmd.add_argument(
        "--limit", default="1000",
        help="返回最多 N 条匹配决策；为寻找匹配项可能扫描更多历史记录",
    )
    canary_eval_cmd.add_argument("--json", action="store_true")

    reverification_cmd = sub.add_parser(
        "reverification",
        help="Selective Reverification v1 审计（只读：status/history）",
    )
    reverification_sub = reverification_cmd.add_subparsers(
        dest="reverification_command")

    reverification_status = reverification_sub.add_parser(
        "status", help="查看当前候选上各 verifier 的 reuse/rerun 判定与原因")
    reverification_status.add_argument("--workflow-id", required=True)
    reverification_status.add_argument("--json", action="store_true")

    reverification_history = reverification_sub.add_parser(
        "history", help="审计历史判定（只读）")
    reverification_history.add_argument("--workflow-id", required=True)
    reverification_history.add_argument("--limit", default="100")
    reverification_history.add_argument("--json", action="store_true")

    rollout_cmd = sub.add_parser(
        "rollout",
        help="Adaptive Router Controlled Rollout（人工扩量/回退/审计）",
    )
    rollout_sub = rollout_cmd.add_subparsers(dest="rollout_command")

    rollout_status = rollout_sub.add_parser("status", help="查看所有 bucket 当前阶段")
    rollout_status.add_argument("--json", action="store_true")

    rollout_set = rollout_sub.add_parser("set", help="人工推进一个 stage（相邻）")
    rollout_set.add_argument("--agent", required=True)
    rollout_set.add_argument("--node", required=True)
    rollout_set.add_argument("--task-type", default="")
    rollout_set.add_argument("--percentage", required=True,
                             help="off/5/10/25/50")
    rollout_set.add_argument("--reason", required=True)
    rollout_set.add_argument("--source", default="cli")
    rollout_set.add_argument("--json", action="store_true")

    rollout_off = rollout_sub.add_parser("off", help="紧急回退到 off")
    rollout_off.add_argument("--agent", required=True)
    rollout_off.add_argument("--node", required=True)
    rollout_off.add_argument("--task-type", default="")
    rollout_off.add_argument("--reason", required=True)
    rollout_off.add_argument("--source", default="cli")
    rollout_off.add_argument("--json", action="store_true")

    rollout_hist = rollout_sub.add_parser("history", help="审计历史（只读）")
    rollout_hist.add_argument("--agent", default=None)
    rollout_hist.add_argument("--node", default=None)
    rollout_hist.add_argument("--task-type", default=None)
    rollout_hist.add_argument("--limit", default="100")
    rollout_hist.add_argument("--json", action="store_true")

    rollout_guard = rollout_sub.add_parser(
        "check-guard", help="Safety Guard 只读评估/自动回退")
    rollout_guard.add_argument("--agent", required=True)
    rollout_guard.add_argument("--node", required=True)
    rollout_guard.add_argument("--task-type", default="")
    rollout_guard.add_argument("--auto-rollback", action="store_true")
    rollout_guard.add_argument("--reason", default="safety guard triggered")
    rollout_guard.add_argument("--source", default="guard")
    rollout_guard.add_argument("--json", action="store_true")

    recovery_status = sub.add_parser('recovery-status', help='只读工作流持久恢复义务')
    recovery_status.add_argument('--workflow-id', required=True)
    recovery_decide = sub.add_parser('recovery-decide', help='按版本确认恢复重试或有期限等待')
    recovery_decide.add_argument('--operation-id', type=int, required=True)
    recovery_decide.add_argument('--expected-version', type=int, required=True)
    recovery_decide.add_argument('--operator', required=True)
    recovery_decide.add_argument('--action', choices=['retry', 'hold', 'verify'], required=True)
    recovery_decide.add_argument('--reason', required=True)
    recovery_decide.add_argument('--until', type=float)
    partial_retire = sub.add_parser('partial-launch-retire', help='审计并归档旧代未登记部分启动后解除当前派发阻断')
    partial_retire.add_argument('--workflow-id', required=True)
    partial_retire.add_argument('--operation-id', type=int, required=True)
    partial_retire.add_argument('--expected-version', type=int, required=True)
    partial_retire.add_argument('--prior-operation-id', type=int, required=True)
    partial_retire.add_argument('--expected-prior-version', type=int, required=True)
    partial_retire.add_argument('--candidate-sha', required=True)
    partial_retire.add_argument('--intent-id', required=True)
    partial_retire.add_argument('--task-id', required=True)
    partial_retire.add_argument('--pane-id', required=True)
    partial_retire.add_argument('--terminal-id', required=True)
    partial_retire.add_argument('--transcript-sha256', required=True)
    partial_retire.add_argument('--operator', required=True)
    partial_retire.add_argument('--reason', required=True)
    partial_retire.add_argument('--confirm-startup-only', action='store_true', required=True)
    partial_preview = sub.add_parser('partial-launch-preview', help='只读显示指定 Pane transcript 并生成 SHA-256')
    partial_preview.add_argument('--pane-id', required=True)

    migrate = sub.add_parser('workflow-migrate', help='Evidence-backed legacy identity/config migration')
    migrate.add_argument('workflow_id')
    migrate.add_argument('--action', choices=['plan', 'apply', 'rollback'], default='plan')
    migrate.add_argument('--input', help='Reviewed plan or rollback receipt JSON')
    migrate.add_argument('--output', help='New output file (never overwritten)')

    budget = sub.add_parser('node-budget-extend', help='Explicit bounded cumulative budget grant with config CAS')
    budget.add_argument('workflow_id')
    budget.add_argument('node')
    budget.add_argument('--expected-sha', required=True)
    budget.add_argument('--additional', type=int, required=True)
    budget.add_argument('--operator', required=True)
    budget.add_argument('--reason', required=True)

    acceptance = sub.add_parser('acceptance-record', help='Bind explicit business verdict to verified report checkpoints')
    acceptance.add_argument('--task-id', required=True)
    acceptance.add_argument('--run-id', required=True)
    acceptance.add_argument('--epoch', required=True)
    acceptance.add_argument('--candidate-sha', required=True)
    acceptance.add_argument('--verdict', choices=['pass', 'blocked'], required=True)
    acceptance.add_argument('--artifact', action='append', required=True)
    acceptance.add_argument('--criterion', action='append', required=True)

    sync_exp_cmd = sub.add_parser('sync-experience', help='Sync pending local business rule candidates with Hub')
    sync_exp_cmd.add_argument('--json', action='store_true', help='Output JSON result')
    sync_exp_cmd.add_argument('--force', action='store_true', help='Retry pending uploads immediately, ignoring backoff schedule')
    sync_exp_cmd.add_argument('--token', type=str, default=None, help='Hub auth token (defaults to HERDR_HUB_TOKEN env var)')
    sync_exp_cmd.add_argument('--include-lessons', action='store_true', help='Extract rules from docs/lessons/lessons-learned.md')
    sync_exp_cmd.add_argument('--lessons-path', type=str, default=None, help='Path to lessons markdown file')
    sync_exp_cmd.add_argument('--workflow-id', type=str, default=None, help='Enqueue and extract rules for all terminal tasks in workflow')
