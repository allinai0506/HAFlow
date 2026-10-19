"""Bind workspace identity and verification commands to live CLI helpers."""


def build_verification_bindings(namespace):
    def is_internal_untracked(path):
        from herdr.workspace_fingerprint import is_internal_untracked

        return is_internal_untracked(
            path,
            namespace["INTERNAL_UNTRACKED_EXACT"],
            namespace["INTERNAL_UNTRACKED_PREFIXES"],
        )

    def fingerprint_file(repo, relpath):
        from herdr.workspace_fingerprint import fingerprint_file

        return fingerprint_file(repo, relpath)

    def current_workspace_fingerprint(repo):
        from herdr.workspace_fingerprint import current_workspace_fingerprint

        return current_workspace_fingerprint(
            repo,
            is_internal_untracked=namespace["_is_internal_untracked"],
            fingerprint_file=namespace["fingerprint_file"],
        )

    def execution_fields_from_workflow(workflow_record):
        from herdr.workspace_fingerprint import execution_fields_from_workflow

        return execution_fields_from_workflow(
            workflow_record, execution_mode=namespace["execution_mode"]
        )

    def context_workspace_fingerprint(repo):
        from herdr.workspace_fingerprint import context_workspace_fingerprint

        return context_workspace_fingerprint(
            repo,
            is_internal_untracked=namespace["_is_internal_untracked"],
            fingerprint_file=namespace["fingerprint_file"],
        )

    def verify_baseline(task_id):
        from herdr.task_cli_verification import verify_baseline

        return verify_baseline(
            task_id,
            load_tasks=namespace["load_tasks"],
            find_task=namespace["find_task"],
            _git_head_sha=namespace["_git_head_sha"],
            _git_interval_commits=namespace["_git_interval_commits"],
            context_workspace_fingerprint=namespace["context_workspace_fingerprint"],
            current_workspace_fingerprint=namespace["current_workspace_fingerprint"],
        )

    def verify_metrics(task_id, min_score=99.9, if_present=False):
        from herdr.task_cli_verification import verify_metrics

        return verify_metrics(
            task_id,
            min_score,
            if_present,
            load_tasks=namespace["load_tasks"],
            find_task=namespace["find_task"],
        )

    def differential_test(args):
        from herdr.task_cli_verification import differential_test

        return differential_test(args)

    return {
        "_is_internal_untracked": is_internal_untracked,
        "fingerprint_file": fingerprint_file,
        "current_workspace_fingerprint": current_workspace_fingerprint,
        "execution_fields_from_workflow": execution_fields_from_workflow,
        "context_workspace_fingerprint": context_workspace_fingerprint,
        "verify_baseline": verify_baseline,
        "verify_metrics": verify_metrics,
        "differential_test": differential_test,
    }
