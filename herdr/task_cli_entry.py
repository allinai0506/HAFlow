"""Bind command handlers and enter the herdr-task argument parser."""

ENTRY_HANDLERS = (
    "_get_store",
    "add_task",
    "adopt_verdict_cli",
    "bypass_gate_cli",
    "check_delivery_for_pr",
    "check_zombie_obligations",
    "cleanup_task",
    "clear_finalize_escalation",
    "close_workflow",
    "cmd_canary_eval",
    "cmd_compact",
    "cmd_eval",
    "cmd_eval_compare",
    "cmd_launch_reconcile",
    "cmd_metrics",
    "cmd_observe",
    "cmd_panes",
    "cmd_reap",
    "cmd_renew_completion",
    "cmd_replay",
    "cmd_reverification",
    "cmd_rework",
    "cmd_rollout",
    "cmd_route_shadow",
    "cmd_shadow_eval",
    "cmd_task_checkpoint",
    "cmd_tool_run",
    "cmd_workflow_migration",
    "cmd_workflow_recovery",
    "cmd_working_context",
    "commit_task",
    "create_pull_request",
    "differential_test",
    "dispatch_task",
    "ensure_node_runtime",
    "finalize_task",
    "force_advance",
    "get_task",
    "integrate_task",
    "launch_task",
    "list_tasks",
    "merge_pull_request",
    "node_status",
    "note_add",
    "note_list",
    "ops_center",
    "pause_task",
    "purge_task",
    "record_delivery_note",
    "record_review_closure",
    "reopen_workflow",
    "resolve_workflow_id_for_close",
    "resume_task",
    "set_status",
    "stage_reset",
    "supersede_task",
    "verify_baseline",
    "verify_metrics",
)

TASK_REGISTRY_HANDLERS = frozenset({
    "add_task", "get_task", "list_tasks", "check_zombie_obligations",
})


def build_task_registry_handlers(dependencies):
    from functools import partial
    from herdr import task_cli_tasks

    common = {
        "load_tasks": dependencies["load_tasks"],
        "find_task": dependencies["find_task"],
    }
    add_deps = {
        **common,
        "new_run_id": dependencies["new_run_id"],
        "now": dependencies["_now"],
        "init_status_history": dependencies["_init_status_history"],
        "save_tasks": dependencies["save_tasks"],
        "get_store": dependencies["_get_store"],
        "trajectory_ledger": dependencies["TrajectoryLedger"],
        "record_trajectory_event": dependencies["record_trajectory_event_best_effort"],
    }
    return {
        "add_task": partial(task_cli_tasks.add_task, deps=add_deps),
        "get_task": partial(task_cli_tasks.get_task, deps=common),
        "list_tasks": partial(task_cli_tasks.list_tasks, deps={"load_tasks": common["load_tasks"]}),
        "check_zombie_obligations": partial(
            task_cli_tasks.check_zombie_obligations,
            deps={"load_tasks": common["load_tasks"]},
        ),
    }


def main(*, dependencies, cli_main=None, handler_names=ENTRY_HANDLERS):
    if cli_main is None:
        from herdr.task_cli import main as cli_main
    selected = set(handler_names)
    handlers = {
        name: dependencies[name]
        for name in selected - TASK_REGISTRY_HANDLERS
        if name in dependencies
    }
    selected_registry = selected & TASK_REGISTRY_HANDLERS
    if selected_registry == TASK_REGISTRY_HANDLERS:
        handlers.update(build_task_registry_handlers(dependencies))
    else:
        handlers.update({name: dependencies[name] for name in selected_registry})
    return cli_main(handlers=handlers)
