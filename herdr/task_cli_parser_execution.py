"""Argument definitions for the execution command family."""

import argparse


def add_execution_parsers(sub):
    add = sub.add_parser("add")
    add.add_argument("--task-id", required=True)
    add.add_argument("--workflow-id", required=True)
    add.add_argument("--node", default=None, help="Workflow node ID")
    add.add_argument("--stage", default=None, help="Workflow stage key (alias for --node)")
    add.add_argument("--workspace", required=True)
    add.add_argument("--pane", required=True)
    add.add_argument("--agent", required=True)
    add.add_argument("--clone")
    add.add_argument(
        "--integration-mode",
        choices=["git", "none"],
        default="none"
    )
    add.add_argument("--goal", required=True)
    add.add_argument("--customer", default=None, help="Business customer scope for enterprise experience attribution")
    add.add_argument("--business-task-type", default=None, help="Business rule type (e.g. quote); kept separate from dev --task-type routing")
    add.add_argument("--mandatory-experience-rules", action="store_true", help="Fail-closed on Hub outage when fetching enterprise rules")
    add.add_argument(
        "--acceptance",
        action="append",
        default=[]
    )

    for checkpoint_command in ('checkpoint-publish', 'checkpoint-read', 'checkpoint-aggregate'):
        task_checkpoint_parser = sub.add_parser(checkpoint_command, help='Run-bound published report segments')
        task_checkpoint_parser.add_argument('--task-id', required=True)
        task_checkpoint_parser.add_argument('--run-id', required=True)
        task_checkpoint_parser.add_argument('--epoch', required=True)
        if checkpoint_command == 'checkpoint-publish':
            task_checkpoint_parser.add_argument('--segment-file', required=True)
            task_checkpoint_parser.add_argument('--step', required=True, type=int)
            task_checkpoint_parser.add_argument('--completed-step', action='append', default=[])
            task_checkpoint_parser.add_argument('--next-step', default='')
            task_checkpoint_parser.add_argument('--summary', default='')

    tool_run = sub.add_parser('tool-run', help='Run one bounded HAFlow-owned tool and record uncertainty')
    tool_run.add_argument('--task-id', required=True)
    tool_run.add_argument('--run-id', required=True)
    tool_run.add_argument('--epoch', required=True)
    tool_run.add_argument('--timeout', type=float, default=30)
    tool_run.add_argument('--output-limit', type=int, default=65536)
    tool_run.add_argument('--input-file')
    tool_run.add_argument('tool_argv', nargs=argparse.REMAINDER)

    rework = sub.add_parser("rework", help="Continue a task on its existing owned pane")
    rework.add_argument("task_id")
    rework.add_argument("--prompt")
    rework.add_argument("--reason", default="review rework")
    rework.add_argument("--request-id", help="idempotent controller delivery token")
    reap = sub.add_parser("reap", help="Inspect/reclaim archived dynamic panes with identity checks")
    reap.add_argument("--workflow-id", required=True)
    reap.add_argument("--apply", action="store_true", help="Close proven owned panes; default is read-only")
    panes = sub.add_parser("panes", help="Show cumulative tasks, concurrency, and persisted pane references")
    panes.add_argument("--workflow-id", required=True)
    panes.add_argument("--json", action="store_true")
    launch_reconcile = sub.add_parser('launch-reconcile', help='Read-only launch resource inventory; apply releases only proven absence')
    launch_reconcile.add_argument('--workflow-id', required=True)
    launch_reconcile.add_argument('--node', required=True)
    launch_reconcile.add_argument('--dispatch-role', default='worker')
    launch_reconcile.add_argument('--candidate-sha', default='')
    launch_reconcile.add_argument('--dispatch-round', type=int, default=1)
    launch_reconcile.add_argument('--apply', action='store_true')
    launch_reconcile.add_argument('--authorize-terminal-id', help='Bind an inspected native terminal_id to a legacy never-started allocation; does not close it')
    launch_reconcile.add_argument('--confirm-agent-never-started', action='store_true', help='Explicit operator attestation; idle or missing agent alone is insufficient')
    launch_reconcile.add_argument('--reason', help='Evidence supporting never-started authorization')
    launch = sub.add_parser("launch")
    launch.add_argument("--ack-overflow", action="store_true",
                        help="Acknowledge legacy cumulative overflow (audited); never bypasses hard budgets")
    launch.add_argument("--supersede-reason", help="Explicit reason for replacement instead of same-task rework")
    launch.add_argument("--artifact-mode", choices=["repository_changes", "shared_artifacts"])
    launch.add_argument("--dispatch-role", default=None, help="Stable node role shared by automatic/manual launches")
    launch.add_argument("--dispatch-operation-id", type=int, default=None, help="Bind coordinator dispatch to its durable operation")
    launch.add_argument("--dispatch-attempt", type=int, default=None, help="Bind launch authorization to one controller attempt")
    launch.add_argument("--dispatch-round", type=int, default=None, help="New rounds require --supersedes")
    launch.add_argument("--task-id", required=True)
    launch.add_argument("--workflow-id", required=True)
    launch.add_argument("--node", default=None, help="Workflow node ID")
    launch.add_argument("--stage", default=None, help="Workflow stage key (alias for --node)")
    launch.add_argument("--source", required=True)
    launch.add_argument("--agent", default="auto")
    launch.add_argument(
        "--task-type",
        choices=[
            "fix",
            "feat",
            "refactor",
            "docs",
            "test",
            "chore",
            "perf",
            "ci"
        ],
        default="feat"
    )
    launch.add_argument(
        "--integration-mode",
        choices=["git", "none"],
        default="none"
    )
    launch.add_argument("--goal", required=True)
    launch.add_argument(
        "--acceptance",
        action="append",
        default=[]
    )
    launch.add_argument("--prompt", required=True)
    launch.add_argument("--customer", default=None, help="Business customer scope for enterprise experience attribution")
    launch.add_argument("--business-task-type", default=None, help="Business rule type (e.g. quote); kept separate from dev --task-type routing")
    launch.add_argument("--mandatory-experience-rules", action="store_true", help="Fail-closed on Hub outage when fetching enterprise rules")
    launch.add_argument("--run-id", default=None,
                        help="Use a caller-assigned Run identity, e.g. for Replay lineage")
    launch.add_argument("--execution-id", default=None,
                        help="Workflow execution identity inherited by the Task. "
                             "Tasks with distinct run_id values but the same "
                             "execution_id are provably one execution.")
    launch.add_argument("--replay-of", default=None,
                        help="Source Run identity for a Replay task")
    launch.add_argument("--agent-policy", default=None,
                        help="JSON policy override for Replay launch")
    launch.add_argument(
        "--supersedes",
        default=None,
        help="Task ID of the old task being replaced. Atomically marks it as superseded."
    )
    launch.add_argument(
        "--onto",
        default=None,
        help="Checkout this existing branch instead of creating a task branch "
             "(commits land on it, e.g. to continue an open PR). "
             "Fetched from origin; fails fast if missing."
    )
    launch.add_argument(
        "--candidate-sha",
        default=None,
        help="Scheduler v1: immutable candidate revision this task must "
             "verify (persisted on the task record for join-gate checks)."
    )
    launch.add_argument(
        "--test-cmd",
        default=None,
        help="Custom test command for .herdr-loop automated evaluation",
    )
    launch.add_argument(
        "--lint-cmd",
        default=None,
        help="Custom lint command for .herdr-loop automated evaluation",
    )
    launch.add_argument(
        "--repro-cmd",
        default=None,
        help="Target reproduction test command for fix-loop automated evaluation",
    )
    launch.add_argument(
        "--allow-reuse-implementation-agents",
        dest="allow_reuse",
        action="store_true",
        default=False,
        help="FR-6.1 opt-out: allow test/review to reuse implementation agents "
             "(requires --reuse-reason; event留痕, no approval; R9)",
    )
    launch.add_argument(
        "--reuse-reason",
        dest="reuse_reason",
        default=None,
        help="Reason for isolation opt-out (required with --allow-reuse-*, R9)",
    )

    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("task_id")
    dispatch.add_argument("--prompt", required=True)

    verify_cmd = sub.add_parser("verify-baseline")
    verify_cmd.add_argument("task_id")

    verify_metrics_cmd = sub.add_parser("verify-metrics")
    verify_metrics_cmd.add_argument("task_id")
    verify_metrics_cmd.add_argument(
        "--min-score",
        type=float,
        default=99.9,
        help="Minimum composite score required (default: 99.9)",
    )
    verify_metrics_cmd.add_argument(
        "--if-present",
        action="store_true",
        help="Skip cleanly (exit 0) if task does not have .herdr-loop configured",
    )

    diff_cmd = sub.add_parser(
        "differential-test",
        help="Run differential testing comparison between candidate and base test results",
    )
    diff_cmd.add_argument("--candidate-output", help="Path to candidate test execution output file")
    diff_cmd.add_argument("--base-output", help="Path to base branch test execution output file")
    diff_cmd.add_argument("--base-snapshot", help="Path to base branch BASELINE_TEST.json snapshot")
    diff_cmd.add_argument("--candidate-exit-code", type=int, default=0, help="Exit code of candidate test run")
    diff_cmd.add_argument("--base-exit-code", type=int, default=0, help="Exit code of base test run")

    commit_cmd = sub.add_parser("commit")
    commit_cmd.add_argument("task_id")
    commit_cmd.add_argument("--message")
    commit_cmd.add_argument(
        "--allow-rebase",
        action="store_true",
        help="allow adoption of rebased commits with rewritten committer date",
    )

    integrate = sub.add_parser("integrate")
    integrate.add_argument("task_id")

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("task_id")

    purge = sub.add_parser("purge")
    purge.add_argument("task_id")

    finalize = sub.add_parser("finalize")
    finalize.add_argument("task_id")
    finalize.add_argument(
        "--force",
        action="store_true",
        help="allow tearing down failed tasks (debugging现场默认保留)",
    )
    finalize.add_argument(
        "--purge-clones",
        dest="purge_clones",
        action="store_true",
        help="delete clones even without integration evidence",
    )

    close_wf = sub.add_parser("close-workflow")
    close_wf.add_argument("workflow_id", nargs="?", default=None, help="工作流 ID（在项目目录下可省略，或输入短后缀）")
    close_wf.add_argument(
        "--include-coordinator",
        dest="include_coordinator",
        action="store_true",
        help="also close the coordinator pane (run AFTER knowledge capture)",
    )
    close_wf.add_argument(
        "--purge-clones",
        dest="purge_clones",
        action="store_true",
        help="delete clones even without integration evidence",
    )
    close_wf.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="show the teardown plan without touching anything",
    )
    close_wf.add_argument(
        "--abandon",
        action="store_true",
        help="abandon the entire workflow deliverable "
             "(records outcome=abandoned; discards all deliverables; "
             "never triggered automatically; differs from --accept-escalated "
             "which keeps deliverables and --force which bypasses all gates)",
    )
    close_wf.add_argument(
        "--force",
        action="store_true",
        help="directly bypass the whole human-confirmation contract, "
             "including escalated tasks (keeps the historical force escape "
             "hatch; differs from scoped --accept-escalated)",
    )
    close_wf.add_argument(
        "--accept-escalated",
        dest="accept_escalated",
        action="store_true",
        help="human confirmation scoped to machine-set finalize_escalated "
             "tasks only (keeps deliverables; use after verifying them; "
             "differs from --force which bypasses all gates, --abandon "
             "which discards the whole workflow, supersede which "
             "permanently discards that branch commits, clear-escalation "
             "which revokes the flag and re-drives integrate and may "
             "re-conflict)",
    )
