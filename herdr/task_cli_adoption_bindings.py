"""Bind Git adoption probes and publication helpers to CLI operations."""


def build_adoption_bindings(namespace):
    def run_commit_index_command(task_id, clone, command):
        from herdr.task_cli_adoption import run_commit_index_command

        return run_commit_index_command(
            task_id, clone, command, run_command=namespace["subprocess"].run
        )

    def adoption_skew_seconds():
        from herdr.git_adoption_probe import adoption_skew_seconds

        return adoption_skew_seconds()

    def git_head_sha(clone):
        from herdr.git_adoption_probe import git_head_sha

        return git_head_sha(clone)

    def git_is_ancestor(clone, baseline, head):
        from herdr.git_adoption_probe import git_is_ancestor

        return git_is_ancestor(clone, baseline, head)

    def git_log_commits(clone, rev, limit=None, first_parent=False):
        from herdr.git_adoption_probe import git_log_commits

        return git_log_commits(clone, rev, limit=limit, first_parent=first_parent)

    def git_commit_paths(clone, sha):
        from herdr.git_adoption_probe import git_commit_paths

        return git_commit_paths(clone, sha)

    def git_current_branch(clone):
        from herdr.git_adoption_probe import git_current_branch

        return git_current_branch(clone)

    def git_remote_contained_shas(clone, shas, exclude_branches=None):
        from herdr.git_adoption_probe import git_remote_contained_shas

        return git_remote_contained_shas(
            clone, shas, exclude_branches=exclude_branches
        )

    def git_interval_commits(clone, baseline, head):
        from herdr.git_adoption_probe import git_interval_commits

        return git_interval_commits(
            clone,
            baseline,
            head,
            log_commits=namespace["_git_log_commits"],
            commit_paths=namespace["_git_commit_paths"],
        )

    def git_head_history(clone, head, limit=100):
        from herdr.git_adoption_probe import git_head_history

        return git_head_history(
            clone,
            head,
            limit=limit,
            log_commits=namespace["_git_log_commits"],
            commit_paths=namespace["_git_commit_paths"],
        )

    def adopt_head_if_attributable(task_id, task, clone, allow_rebase=False):
        from herdr.task_cli_adoption import adopt_head_if_attributable

        names = (
            "_get_store", "_adoption_skew_seconds", "_git_head_sha",
            "_git_current_branch", "_git_is_ancestor", "_git_interval_commits",
            "_git_head_history", "_git_remote_contained_shas", "_coerce_epoch",
            "set_status",
        )
        return adopt_head_if_attributable(
            task_id,
            task,
            clone,
            allow_rebase=allow_rebase,
            **{name[1:] if name.startswith("_") else name: namespace[name] for name in names},
        )

    def publish_integrated_task(task_id):
        from herdr.scheduler_facts import publish_integrated_candidate

        try:
            publish_integrated_candidate(namespace["_get_store"](), task_id)
        except ValueError as exc:
            print(
                f"[CANDIDATE PUBLICATION REJECTED] {exc}",
                file=namespace["sys"].stderr,
            )

    return {
        "_run_commit_index_command": run_commit_index_command,
        "_adoption_skew_seconds": adoption_skew_seconds,
        "_git_head_sha": git_head_sha,
        "_git_is_ancestor": git_is_ancestor,
        "_git_log_commits": git_log_commits,
        "_git_commit_paths": git_commit_paths,
        "_git_current_branch": git_current_branch,
        "_git_remote_contained_shas": git_remote_contained_shas,
        "_git_interval_commits": git_interval_commits,
        "_git_head_history": git_head_history,
        "_adopt_head_if_attributable": adopt_head_if_attributable,
        "_publish_integrated_task": publish_integrated_task,
    }
