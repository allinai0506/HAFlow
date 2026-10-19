"""Bind teardown and finalization commands to the live CLI namespace."""


def build_teardown_bindings(namespace):
    def herdr(*args):
        from herdr.task_cli_teardown import herdr_command

        return herdr_command(
            *args,
            deps={
                "resolve_binary": namespace["resolve_binary"],
                "run_command": namespace["subprocess"].run,
            },
        )

    def delete_clone_safely(clone_path):
        from herdr.task_cli_teardown import delete_clone_safely

        return delete_clone_safely(clone_path, deps={"clone_root": namespace["CLONE_ROOT"]})

    def teardown_output_guard(task):
        from herdr.task_cli_teardown import _teardown_output_guard

        return _teardown_output_guard(task)

    def record_output_retention(task, diagnosis, *, authorized_via=""):
        from herdr.task_cli_teardown import _record_output_retention

        return _record_output_retention(
            task,
            diagnosis,
            authorized_via=authorized_via,
            deps={"get_store": namespace["_get_store"]},
        )

    def clone_deletable(task):
        from herdr.task_cli_teardown import clone_deletable

        return clone_deletable(
            task,
            deps={"teardown_output_guard": namespace["_teardown_output_guard"]},
        )

    def dump_transcript(task):
        from herdr.task_cli_teardown import dump_transcript

        return dump_transcript(
            task,
            deps={
                "herdr": namespace["_herdr"],
                "now": namespace["_now"],
                "evidence_root": namespace["EVIDENCE_ROOT"],
            },
        )

    def close_pane(pane_id):
        from herdr.task_cli_teardown import close_pane

        return close_pane(
            pane_id,
            deps={"get_store": namespace["_get_store"], "herdr": namespace["_herdr"]},
        )

    def advance_to_cleaned(task_id, status):
        from herdr.task_cli_teardown import _advance_to_cleaned

        return _advance_to_cleaned(
            task_id,
            status,
            deps={
                "finalize_advance": namespace["FINALIZE_ADVANCE"],
                "set_status": namespace["set_status"],
            },
        )

    def close_task_identity(task):
        from herdr.task_cli_teardown import _close_task_identity

        return _close_task_identity(task)

    def close_owned_pane(identity):
        from herdr.task_cli_teardown import _close_owned_pane

        return _close_owned_pane(
            identity,
            deps={"get_store": namespace["_get_store"], "herdr": namespace["_herdr"]},
        )

    def finalize_one(
        task,
        purge_clones=False,
        dry_run=False,
        close_claim=None,
        output_override="",
        reclaim_archive=None,
        reclaim_plan=None,
    ):
        from herdr.task_cli_teardown import finalize_one

        names = (
            "_advance_to_cleaned", "_close_owned_pane", "_close_task_identity",
            "_get_store", "_record_output_retention", "_teardown_output_guard",
            "clone_deletable", "close_pane", "delete_clone_safely", "dump_transcript",
            "set_status",
        )
        deps = {name.lstrip("_"): namespace[name] for name in names}
        deps.update(
            clone_root=namespace["CLONE_ROOT"],
            finalize_advance=namespace["FINALIZE_ADVANCE"],
        )
        return finalize_one(
            task,
            purge_clones=purge_clones,
            dry_run=dry_run,
            close_claim=close_claim,
            output_override=output_override,
            reclaim_archive=reclaim_archive,
            reclaim_plan=reclaim_plan,
            deps=deps,
        )

    def finalize_task(task_id, force=False, purge_clones=False):
        from herdr.task_cli_delivery import finalize_task

        return finalize_task(
            task_id,
            force=force,
            purge_clones=purge_clones,
            load_tasks=namespace["load_tasks"],
            find_task=namespace["find_task"],
            teardown_blocking_statuses=namespace["TEARDOWN_BLOCKING_STATUSES"],
            finalize_one=namespace["_finalize_one"],
        )

    return {
        "_herdr": herdr,
        "delete_clone_safely": delete_clone_safely,
        "_teardown_output_guard": teardown_output_guard,
        "_record_output_retention": record_output_retention,
        "clone_deletable": clone_deletable,
        "dump_transcript": dump_transcript,
        "close_pane": close_pane,
        "_advance_to_cleaned": advance_to_cleaned,
        "_close_task_identity": close_task_identity,
        "_close_owned_pane": close_owned_pane,
        "_finalize_one": finalize_one,
        "finalize_task": finalize_task,
    }
