"""Bind dispatch context helpers to the live CLI namespace."""


def build_dispatch_bindings(namespace):
    def finish_committed_recovery_launch(store, task, predecessor, reason):
        from herdr.recovery_successor import (
            has_confirmed_delivery,
            link_committed_successor,
        )

        if not has_confirmed_delivery(store, task):
            print(
                "[DISPATCH RECOVERY_REQUIRED] registered successor delivery unconfirmed; reconcile retained intent"
            )
            namespace["sys"].exit(2)
        if (
            task.get("recovery_predecessor") != predecessor["task_id"]
            or task.get("expected_predecessor_sha") != predecessor.get("commit")
            or type(task.get("expected_predecessor_version")) is not int
        ):
            raise ValueError("Recovery successor lacks captured predecessor identity")
        return link_committed_successor(
            store,
            predecessor["task_id"],
            task["task_id"],
            task["expected_predecessor_version"],
            task["expected_predecessor_sha"],
            reason,
        )

    def apply_execution_identity(task, args):
        execution_id = str(getattr(args, "execution_id", None) or "").strip()
        if execution_id:
            task["execution_id"] = execution_id

    def compile_working_context_ref(task):
        from herdr.task_cli_execution import compile_working_context_ref

        return compile_working_context_ref(task, get_store=namespace["_get_store"])

    def fetch_experience_rules_block(task):
        from herdr.task_cli_execution import fetch_experience_rules_block

        return fetch_experience_rules_block(task, get_store=namespace["_get_store"])

    def prefetch_mandatory_rules(task):
        from herdr.task_cli_execution import prefetch_mandatory_rules

        return prefetch_mandatory_rules(task, get_store=namespace["_get_store"])

    def mandatory_experience_gate(args, node_id):
        from herdr.task_cli_execution import mandatory_experience_gate

        return mandatory_experience_gate(args, node_id)

    def dispatch_task(
        task_id,
        prompt,
        prefetched_experience_suffix=None,
        prefetched_experience_refs=None,
    ):
        from herdr.task_cli_execution import handle_dispatch_task

        return handle_dispatch_task(
            task_id,
            prompt,
            prefetched_experience_suffix=prefetched_experience_suffix,
            prefetched_experience_refs=prefetched_experience_refs,
            get_store=namespace["_get_store"],
            set_status=lambda task, status: namespace["set_status"](task, status),
            compile_context_ref=namespace["_compile_working_context_ref"],
            fetch_rules_block=namespace["_fetch_experience_rules_block"],
            prefetch_rules=namespace["_prefetch_mandatory_rules"],
            timeout=namespace["PROMPT_DELIVERY_TIMEOUT"],
        )

    return {
        "_finish_committed_recovery_launch": finish_committed_recovery_launch,
        "_apply_execution_identity": apply_execution_identity,
        "_compile_working_context_ref": compile_working_context_ref,
        "_fetch_experience_rules_block": fetch_experience_rules_block,
        "_prefetch_mandatory_rules": prefetch_mandatory_rules,
        "_mandatory_experience_gate": mandatory_experience_gate,
        "dispatch_task": dispatch_task,
    }
