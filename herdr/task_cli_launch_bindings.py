"""Bind launch policy helpers to the live CLI dependency namespace."""


def build_launch_bindings(namespace):
    """Build stable CLI names while resolving dependencies at call time.

    The namespace stays live so existing integrations that replace a CLI
    dependency (for example, a test store) keep the same behavior.
    """
    from herdr import task_cli_launch, task_cli_launch_preflight, task_cli_resources

    def refuse_loop_launch(args, refusal, *, clone_path, pane_id, pane_source):
        return task_cli_launch.refuse_loop_launch(
            args,
            refusal,
            clone_path=clone_path,
            pane_id=pane_id,
            pane_source=pane_source,
            reclaim_resources=namespace["_reclaim_unregistered_launch_resources"],
            get_store=namespace["_get_store"],
        )

    def is_isolation_rejection(exc):
        try:
            from herdr.agent_router import RouterIsolationRejection

            if isinstance(exc, RouterIsolationRejection):
                return True
        except ImportError:
            pass
        msg = str(exc).lower()
        return (
            "isolation" in msg
            or "prohibited for stage" in msg
            or ("candidates" in msg and "used in stage" in msg)
        )

    def record_router_failure_task(
        args,
        node_id,
        error,
        *,
        reason="router_isolation_rejected",
        failure_code=None,
        remediation=None,
    ):
        return task_cli_launch.record_router_failure_task(
            args,
            node_id,
            error,
            reason=reason,
            failure_code=failure_code,
            remediation=remediation,
            get_store=namespace["_get_store"],
            new_run_id=namespace["new_run_id"],
            now=namespace["_now"],
            release_agent_reservation=namespace["release_agent_reservation"],
        )

    def frozen_candidate_sha(workflow_id):
        return task_cli_launch_preflight._frozen_candidate_sha(workflow_id)

    def resolve_expected_candidate(workflow_id, claim):
        return task_cli_launch_preflight._resolve_expected_candidate(workflow_id, claim)

    def preflight_delivery_identity(workflow_id, node_id, task_id, claim=""):
        return task_cli_launch_preflight.preflight_delivery_identity(
            workflow_id,
            node_id,
            task_id,
            claim,
            get_store=namespace["_get_store"],
            resolve_expected_candidate=namespace["_resolve_expected_candidate"],
        )

    def preflight_baseline_freshness(args, source_repo, base_branch):
        return task_cli_launch_preflight.preflight_baseline_freshness(
            args,
            source_repo,
            base_branch,
            get_store=namespace["_get_store"],
            record_router_failure=namespace["_record_router_failure_task"],
        )

    def reclaim_unregistered_launch_resources(
        args, clone_path="", pane_id="", pane_source=""
    ):
        return task_cli_resources.reclaim_unregistered_launch_resources(
            args,
            clone_path,
            pane_id,
            pane_source,
            close_pane=namespace["close_pane"],
            delete_clone_safely=namespace["delete_clone_safely"],
            release_agent_reservation=namespace["release_agent_reservation"],
        )

    def validate_supersedes_for_launch(args, node_id, store):
        return task_cli_launch_preflight.validate_supersedes_for_launch(
            args, node_id, store
        )

    def validate_test_review_baseline(args, baseline_commit, source_repo):
        return task_cli_launch_preflight.validate_test_review_baseline(
            args,
            baseline_commit,
            source_repo,
            get_store=namespace["_get_store"],
            resolve_expected_candidate=namespace["_resolve_expected_candidate"],
            run=namespace["subprocess"].run,
        )

    def canonical_commit(sha, source_repo):
        return task_cli_launch_preflight._canonical_commit(
            sha, source_repo, run=namespace["subprocess"].run
        )

    def same_commit(left, right, source_repo):
        return task_cli_launch_preflight.same_commit(
            left, right, source_repo, run=namespace["subprocess"].run
        )

    def validate_test_delivery_baseline(
        args, candidate_sha, baseline_commit, source_repo
    ):
        return task_cli_launch_preflight.validate_test_delivery_baseline(
            args,
            candidate_sha,
            baseline_commit,
            source_repo,
            get_store=namespace["_get_store"],
            run=namespace["subprocess"].run,
        )

    def validate_selective_replacement_baseline(
        args, candidate_sha, baseline_commit, source_repo
    ):
        return task_cli_launch_preflight.validate_selective_replacement_baseline(
            args,
            candidate_sha,
            baseline_commit,
            source_repo,
            get_store=namespace["_get_store"],
            run=namespace["subprocess"].run,
        )

    return {
        "_refuse_loop_launch": refuse_loop_launch,
        "_is_isolation_rejection": is_isolation_rejection,
        "_record_router_failure_task": record_router_failure_task,
        "_frozen_candidate_sha": frozen_candidate_sha,
        "_resolve_expected_candidate": resolve_expected_candidate,
        "_preflight_delivery_identity": preflight_delivery_identity,
        "_preflight_baseline_freshness": preflight_baseline_freshness,
        "_reclaim_unregistered_launch_resources": reclaim_unregistered_launch_resources,
        "_validate_supersedes_for_launch": validate_supersedes_for_launch,
        "_validate_test_review_baseline": validate_test_review_baseline,
        "_canonical_commit": canonical_commit,
        "_same_commit": same_commit,
        "_validate_test_delivery_baseline": validate_test_delivery_baseline,
        "_validate_selective_replacement_baseline": validate_selective_replacement_baseline,
    }
