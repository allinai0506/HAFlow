"""Bind task status and review commands to the live CLI namespace."""


def build_status_bindings(namespace):
    def enforce_wrapup_review_closure(
        task_id,
        task,
        *,
        skip,
        note,
        operator=None,
        operator_token=None,
        operator_credentials=None,
    ):
        from herdr.task_cli_review import _enforce_wrapup_review_closure

        return _enforce_wrapup_review_closure(
            task_id,
            task,
            skip=skip,
            note=note,
            operator=operator,
            operator_token=operator_token,
            operator_credentials=operator_credentials,
            deps={"get_store": namespace["_get_store"]},
        )

    def record_review_closure(
        task_id,
        pr_url=None,
        ci_status=None,
        findings=None,
        deferred=False,
        reason=None,
        head_sha=None,
        unresolved_p1=None,
        unresolved_p2=None,
    ):
        from herdr.task_cli_review import record_review_closure as record

        return record(
            task_id,
            pr_url,
            ci_status,
            findings,
            deferred,
            reason,
            head_sha,
            unresolved_p1,
            unresolved_p2,
            deps={
                "get_store": namespace["_get_store"],
                "load_tasks": namespace["load_tasks"],
                "find_task": namespace["find_task"],
            },
        )

    def is_gate_task(task):
        from herdr.task_cli_verification import _is_gate_task

        return _is_gate_task(task)

    def gate_verdict_file_candidates(task):
        from herdr.task_cli_verification import _gate_verdict_file_candidates

        return _gate_verdict_file_candidates(task)

    def authenticate_gate_verdict_evidence(task, expected_verdict=None):
        from herdr.task_cli_verification import _authenticate_gate_verdict_evidence

        return _authenticate_gate_verdict_evidence(task, expected_verdict)

    def set_status(
        task_id,
        new_status,
        verdict=None,
        note=None,
        affected_task_ids=None,
        skip_review_closure=False,
        skip_operator=None,
        operator_token=None,
        operator_credentials=None,
    ):
        from herdr.task_cli_status import set_task_status

        return set_task_status(
            task_id,
            new_status,
            verdict=verdict,
            note=note,
            affected_task_ids=affected_task_ids,
            skip_review_closure=skip_review_closure,
            skip_operator=skip_operator,
            operator_token=operator_token,
            operator_credentials=operator_credentials,
            load_tasks=namespace["load_tasks"],
            find_task=namespace["find_task"],
            get_store=namespace["_get_store"],
            transitions=namespace["TRANSITIONS"],
            active_task_statuses=namespace["ACTIVE_TASK_STATUSES"],
            is_gate_task=namespace["_is_gate_task"],
            capture_verified_candidate=namespace["_capture_verified_candidate"],
            enforce_wrapup_review_closure=namespace["_enforce_wrapup_review_closure"],
            authenticate_gate_verdict_evidence=namespace[
                "_authenticate_gate_verdict_evidence"
            ],
            record_gate_note=namespace["_record_gate_note"],
            clear_suppress_auto_close=namespace["_clear_suppress_auto_close"],
            sync_tasks_projection=namespace["sync_tasks_projection"],
            tasks_file=namespace.get("TASKS_FILE"),
        )

    def auto_init_task_loop(
        clone_path,
        goal,
        acceptance_list,
        test_cmd=None,
        lint_cmd=None,
        repro_cmd=None,
        node=None,
        task_type=None,
    ):
        from herdr.task_cli_launch import auto_init_task_loop

        return auto_init_task_loop(
            clone_path,
            goal,
            acceptance_list,
            test_cmd=test_cmd,
            lint_cmd=lint_cmd,
            repro_cmd=repro_cmd,
            node=node,
            task_type=task_type,
        )

    return {
        "_enforce_wrapup_review_closure": enforce_wrapup_review_closure,
        "record_review_closure": record_review_closure,
        "_is_gate_task": is_gate_task,
        "_gate_verdict_file_candidates": gate_verdict_file_candidates,
        "_authenticate_gate_verdict_evidence": authenticate_gate_verdict_evidence,
        "set_status": set_status,
        "auto_init_task_loop": auto_init_task_loop,
    }
