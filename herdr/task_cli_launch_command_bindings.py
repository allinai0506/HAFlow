"""Bind launch, resource, and bounded-tool commands to CLI dependencies."""


def build_launch_command_bindings(namespace):
    def cmd_launch_reconcile(args):
        from herdr.task_cli_resources import handle_launch_reconcile

        return handle_launch_reconcile(
            args,
            get_store=namespace["_get_store"],
            release_agent_reservation=namespace["release_agent_reservation"],
        )

    def cmd_tool_run(args):
        from herdr.task_cli_execution import handle_tool_run

        return handle_tool_run(args, get_store=namespace["_get_store"])

    def cmd_task_checkpoint(args):
        from herdr.task_cli_execution import handle_task_checkpoint

        return handle_task_checkpoint(args, get_store=namespace["_get_store"])

    def capacity_definition(project):
        from herdr.task_cli_resources import capacity_definition

        return capacity_definition(project)

    def check_launch_capacity(args, project, tasks, node_id):
        from herdr.task_cli_resources import check_launch_capacity

        return check_launch_capacity(
            args,
            project,
            tasks,
            node_id,
            get_store=namespace["_get_store"],
            load_capacity_definition=namespace["_capacity_definition"],
        )

    def cmd_panes(args):
        from herdr.task_cli_resources import handle_panes

        return handle_panes(
            args,
            get_store=namespace["_get_store"],
            load_capacity_definition=namespace["_capacity_definition"],
        )

    def launch(args):
        from herdr.task_cli_launch_orchestrator import launch_task_orchestrated

        names = (
            "BranchOwnershipError", "HERDR_ROOT", "TrajectoryLedger",
            "_apply_execution_identity", "_capacity_definition", "_check_launch_capacity",
            "_finish_committed_recovery_launch", "_get_store", "_init_status_history",
            "_is_gate_task", "_is_isolation_rejection", "_mandatory_experience_gate",
            "_now", "_preflight_baseline_freshness", "_preflight_delivery_identity",
            "_reclaim_unregistered_launch_resources", "_record_router_failure_task",
            "_refuse_loop_launch", "_validate_selective_replacement_baseline",
            "_validate_supersedes_for_launch", "_validate_test_review_baseline",
            "acquire_pane_for_task", "auto_init_task_loop", "choose_agent", "dispatch_task",
            "ensure_branch_available", "ensure_stage_topology", "execution_fields_from_workflow",
            "find_task", "load_tasks", "new_run_id", "pinned_local_onto_matches",
            "project_for_workflow", "record_trajectory_event_best_effort",
            "release_agent_reservation", "supersede_task", "validate_integration_for_execution",
            "validate_onto_for_execution", "workflow_config_for",
        )
        return launch_task_orchestrated(
            args, deps={name: namespace[name] for name in names}
        )

    def launch_task(args):
        from herdr.task_cli_launch import launch_task as handle
        from herdr.task_resources import (
            NodeLaunchBusy,
            record_launch_rejection,
            workflow_launch_lock,
        )

        return handle(
            args,
            deps={
                "NodeLaunchBusy": NodeLaunchBusy,
                "workflow_launch_lock": workflow_launch_lock,
                "record_launch_rejection": record_launch_rejection,
                "get_store": namespace["_get_store"],
                "launch": namespace["_launch_task"],
                "release_agent_reservation": namespace["release_agent_reservation"],
                "GitOperationLock": namespace["GitOperationLock"],
                "GitOperationBusyError": namespace["GitOperationBusyError"],
                "GitProcessActiveError": namespace["GitProcessActiveError"],
                "ensure_no_git_processes": namespace["ensure_no_git_processes"],
            },
        )

    return {
        "cmd_launch_reconcile": cmd_launch_reconcile,
        "cmd_tool_run": cmd_tool_run,
        "cmd_task_checkpoint": cmd_task_checkpoint,
        "_capacity_definition": capacity_definition,
        "_check_launch_capacity": check_launch_capacity,
        "cmd_panes": cmd_panes,
        "_launch_task": launch,
        "launch_task": launch_task,
    }
