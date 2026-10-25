"""Bind workflow lifecycle commands to the live CLI dependency namespace."""


def build_workflow_bindings(namespace):
    """Create workflow command adapters while preserving late dependency lookup."""

    def load_workflow_entry(workflow_id):
        store = namespace["_get_store"]()
        wf = store.get_workflow(workflow_id)
        all_wfs = {
            w["workflow_id"]: w
            for w in store.list_workflows()
            if w.get("workflow_id")
        }
        return wf, all_wfs

    def mark_workflow_completed(workflow_id, outcome="delivered", force=False):
        from herdr.task_cli_workflow_close import mark_workflow_completed as mark

        return mark(
            workflow_id,
            outcome,
            force,
            deps={
                "get_store": namespace["_get_store"],
                "load_workflow_entry": namespace["_load_workflow_entry"],
                "now": namespace["_now"],
                "sync_workflows_projection": namespace["sync_workflows_projection"],
                "workflows_file": namespace.get("WORKFLOWS_FILE") or namespace["WORKFLOWS_FILE"],
            },
        )

    def workflow_stage_tabs(workflow_id, tasks):
        from herdr.task_cli_workflow_close_inventory import _workflow_stage_tabs

        return _workflow_stage_tabs(
            workflow_id,
            tasks,
            deps={"project_for_workflow": namespace["project_for_workflow"], "os": namespace["os"]},
        )

    def live_workspace_panes(workspace_id):
        from herdr.task_cli_workflow_close_inventory import _live_workspace_panes

        return _live_workspace_panes(workspace_id, deps={"herdr": namespace["_herdr"]})

    def foreign_panes_in_tab(live_panes, tab_id, owned_pane_ids):
        from herdr.task_cli_workflow_close_inventory import _foreign_panes_in_tab

        return _foreign_panes_in_tab(live_panes, tab_id, owned_pane_ids)

    def resolve_workflow_id_for_close(input_id=None):
        from herdr.task_cli_workflow_close_inventory import resolve_workflow_id_for_close

        return resolve_workflow_id_for_close(
            input_id,
            deps={
                "load_workflows": namespace["load_workflows"],
                "load_tasks": namespace["load_tasks"],
                "detect_git_root": namespace["detect_git_root"],
                "project_by_root": namespace["project_by_root"],
            },
        )

    def close_workflow(
        workflow_id,
        include_coordinator=False,
        purge_clones=False,
        dry_run=False,
        abandon=False,
        force=False,
        accept_escalated=False,
        confirm_force=None,
    ):
        from herdr.business_gate import business_gate_blockers
        from herdr.delivery_report import build_delivery_report
        from herdr.task_cli_workflow_close import close_workflow as close
        from herdr.workflow import validate_delivery_inventory
        from herdr.workflow_close import workflow_close_claim

        return close(
            workflow_id,
            include_coordinator,
            purge_clones,
            dry_run,
            abandon,
            force,
            accept_escalated,
            confirm_force,
            get_store=namespace["_get_store"],
            workflow_close_claim=workflow_close_claim,
            business_gate_blockers=business_gate_blockers,
            validate_delivery_inventory=validate_delivery_inventory,
            close_workflow_owned=namespace["_close_workflow_owned"],
            build_delivery_report=build_delivery_report,
        )

    def close_workflow_owned(
        workflow_id,
        include_coordinator=False,
        purge_clones=False,
        dry_run=False,
        abandon=False,
        force=False,
        accept_escalated=False,
        confirm_force=None,
        close_claim=None,
    ):
        from herdr.task_cli_workflow_close import close_workflow_owned

        names = (
            "load_tasks", "_load_workflow_entry", "_get_store", "_finalize_one",
            "close_pane", "stage_reset", "_mark_workflow_completed",
            "_workflow_stage_tabs", "_live_workspace_panes", "_foreign_panes_in_tab",
            "_close_owned_pane", "_teardown_output_guard", "_record_output_retention",
        )
        deps = {key[1:] if key.startswith("_") else key: namespace[key] for key in names}
        deps.update(
            clone_root=namespace["CLONE_ROOT"],
            teardown_blocking_statuses=namespace["TEARDOWN_BLOCKING_STATUSES"],
        )
        return close_workflow_owned(
            workflow_id,
            include_coordinator,
            purge_clones,
            dry_run,
            abandon,
            force,
            accept_escalated,
            confirm_force,
            close_claim,
            **deps,
        )

    def node_status(workflow_id, node_key):
        from herdr.task_cli_workflow import node_status as get_node_status
        from herdr.task_cli_tasks import safe_workflow

        return get_node_status(
            workflow_id,
            node_key,
            resolve_node=namespace["resolve_node"],
            load_tasks=namespace["load_tasks"],
            safe_workflow=lambda wid: safe_workflow(
                wid, load_workflow=namespace["load_workflow"]
            ),
            normalize_workflow_nodes=namespace["_normalize_workflow_nodes"],
            node_delivery_complete=namespace["_node_delivery_complete"],
        )

    def autosave_clone_wip(task):
        from herdr.task_cli_rework import autosave_clone_wip

        return autosave_clone_wip(
            task,
            run_command=namespace["subprocess"].run,
            internal_untracked_exact=namespace["INTERNAL_UNTRACKED_EXACT"],
        )

    def supersede_task(
        task_id,
        new_task_id=None,
        reason=None,
        allow_new_run=False,
        abandon=False,
        allow_pending=False,
    ):
        from herdr.task_cli_workflow import supersede_task as supersede

        return supersede(
            task_id,
            new_task_id=new_task_id,
            reason=reason,
            allow_new_run=allow_new_run,
            abandon=abandon,
            allow_pending=allow_pending,
            load_tasks=namespace["load_tasks"],
            find_task=namespace["find_task"],
            get_store=namespace["_get_store"],
            transitions=namespace["TRANSITIONS"],
            sync_tasks_projection=namespace["sync_tasks_projection"],
            autosave_clone_wip=namespace["_autosave_clone_wip"],
            tasks_file=namespace.get("TASKS_FILE"),
            attention_file=namespace.get("ATTENTION_FILE"),
        )

    def clear_finalize_escalation(task_id):
        from herdr.task_cli_workflow import clear_finalize_escalation

        return clear_finalize_escalation(
            task_id,
            deps={
                "get_store": namespace["_get_store"],
                "find_task": namespace["find_task"],
                "load_tasks": namespace["load_tasks"],
                "attention_file": namespace["ATTENTION_FILE"],
            },
        )

    def pause_task(task_id, reason=None):
        from herdr.task_cli_workflow import pause_task

        return pause_task(
            task_id, reason,
            deps={"get_store": namespace["_get_store"], "set_status": namespace["set_status"]},
        )

    def resume_task(task_id):
        from herdr.task_cli_workflow import resume_task

        return resume_task(
            task_id,
            deps={
                "find_task": namespace["find_task"],
                "load_tasks": namespace["load_tasks"],
                "set_status": namespace["set_status"],
            },
        )

    def clear_suppress_auto_close(workflow_id):
        from herdr.task_cli_workflow import _clear_suppress_auto_close

        return _clear_suppress_auto_close(workflow_id, deps={"get_store": namespace["_get_store"]})

    def reopen_workflow(workflow_id):
        from herdr.task_cli_workflow import reopen_workflow, reopen_owned_workflow

        return reopen_workflow(
            workflow_id,
            get_store=namespace["_get_store"],
            reopen_owned=lambda wid: reopen_owned_workflow(
                wid,
                load_workflow_entry=namespace["_load_workflow_entry"],
                herdr=namespace["_herdr"],
                get_store=namespace["_get_store"],
                now=namespace["_now"],
                sync_workflows_projection=namespace["sync_workflows_projection"],
                workflows_file=namespace.get("WORKFLOWS_FILE"),
                reset_stage=namespace["stage_reset"],
            ),
        )

    def stage_reset(workflow_id, stage=None):
        from herdr.task_cli_workflow import stage_reset

        return stage_reset(
            workflow_id,
            stage,
            stage_state_file=namespace["STAGE_STATE_FILE"],
            path_exists=namespace["os"].path.exists,
        )

    def force_advance(workflow_id, stage=None):
        from herdr.task_cli_workflow import force_advance

        return force_advance(workflow_id, stage, reset_stage=namespace["stage_reset"])

    def cmd_workflow_migration(args):
        from herdr.task_cli_workflow_admin import cmd_workflow_migration

        return cmd_workflow_migration(args)

    def cmd_workflow_recovery(args):
        from herdr.task_cli_workflow_admin import cmd_workflow_recovery

        return cmd_workflow_recovery(args)

    return {
        "_load_workflow_entry": load_workflow_entry,
        "_mark_workflow_completed": mark_workflow_completed,
        "_workflow_stage_tabs": workflow_stage_tabs,
        "_live_workspace_panes": live_workspace_panes,
        "_foreign_panes_in_tab": foreign_panes_in_tab,
        "resolve_workflow_id_for_close": resolve_workflow_id_for_close,
        "close_workflow": close_workflow,
        "_close_workflow_owned": close_workflow_owned,
        "node_status": node_status,
        "_autosave_clone_wip": autosave_clone_wip,
        "supersede_task": supersede_task,
        "clear_finalize_escalation": clear_finalize_escalation,
        "pause_task": pause_task,
        "resume_task": resume_task,
        "_clear_suppress_auto_close": clear_suppress_auto_close,
        "reopen_workflow": reopen_workflow,
        "stage_reset": stage_reset,
        "force_advance": force_advance,
        "cmd_workflow_migration": cmd_workflow_migration,
        "cmd_workflow_recovery": cmd_workflow_recovery,
    }
