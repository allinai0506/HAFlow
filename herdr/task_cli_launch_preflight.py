"""Identity checks that run before task launch resource allocation."""

import sys
import sqlite3
import subprocess


def _frozen_candidate_sha(workflow_id):
    """Return the scheduler-frozen candidate, or empty when it cannot be proved."""
    try:
        from herdr import scheduler_facts

        return str(
            scheduler_facts.latest_frozen_candidate_sha(workflow_id) or ""
        ).strip()
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error):
        return ""


def _resolve_expected_candidate(workflow_id, claim):
    claim = str(claim or "").strip()
    frozen = _frozen_candidate_sha(workflow_id)
    if frozen and claim and claim == frozen:
        return frozen, "frozen"
    return "", ""


def _default_workflow_config(workflow_id):
    from .projects import workflow_config_for

    return workflow_config_for(workflow_id)


def _verifier_candidate_gate(workflow_id, node_id, workflow_config_for):
    """True when this node must prove a frozen candidate before it may launch.

    A candidate SHA is a Git fact, so a context workflow's declared verifier has
    nothing to prove; failing it closed would block templates that never had a
    candidate in the first place.
    """
    from .node_roles import candidate_identity_applies, is_verifier_node_id

    config = workflow_config_for(workflow_id) if workflow_config_for else None
    return is_verifier_node_id(config, node_id) and candidate_identity_applies(config)


def preflight_delivery_identity(
    workflow_id,
    node_id,
    task_id,
    claim="",
    *,
    get_store,
    resolve_expected_candidate=_resolve_expected_candidate,
    workflow_config_for=_default_workflow_config,
):
    """Prove the candidate identity before launching a task that verifies one."""
    if not _verifier_candidate_gate(workflow_id, node_id, workflow_config_for):
        return None
    from herdr import delivery_record, workflow_docs

    notes = workflow_docs.load_notes(workflow_id)
    legacy = delivery_record.unidentified_delivery_notes(notes)
    if legacy:
        shown = ", ".join(str(note.get("note_id") or "?") for note in legacy[:5])
        print(
            f"[BASELINE DEGRADED] {len(legacy)} identity-less delivery note(s) "
            f"ignored (human evidence, not delivery claims): {shown}"
        )
    delivery_notes = [note for note in notes if note.get("kind") == "delivery"]

    def reject(code, detail):
        payload = {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "stage": node_id,
            "failure_code": code,
            "detail": str(detail)[:500],
            "actionable": True,
            "remediation": (
                "record a valid delivery with herdr-task record-delivery "
                "before launching test/review"
            ),
        }
        try:
            get_store().record_event(
                "test_baseline_rejected",
                payload,
                workflow_id=workflow_id,
                node_id=node_id,
                task_id=task_id,
                source="herdr-task",
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            AttributeError,
            sqlite3.Error,
        ) as exc:
            print(
                f"[BASELINE EVENT WARN] rejection audit unavailable: {exc}",
                file=sys.stderr,
            )
        print(
            f"[BASELINE REJECTED] {code}: workflow={workflow_id} task={task_id}; "
            f"{detail}; launch refused (FR-6.2 fail-closed)."
        )
        raise SystemExit(2)

    if not delivery_notes:
        candidate, source = resolve_expected_candidate(workflow_id, claim)
        if source == "frozen":
            return {"candidate_sha": candidate, "identity_source": "frozen"}
        reject(
            "delivery_missing",
            "no valid delivery baseline exists and no matching frozen "
            "candidate is recorded for this dispatch claim",
        )

    try:
        effective = delivery_record.select_effective_delivery(notes)
    except delivery_record.DeliveryAmbiguityError as exc:
        reject("delivery_ambiguous", exc)
    if effective is None:
        candidate, source = resolve_expected_candidate(workflow_id, claim)
        if source == "frozen":
            return {"candidate_sha": candidate, "identity_source": "frozen"}
        reject(
            "delivery_invalidated",
            "delivery candidate was invalidated and no matching frozen "
            "candidate exists for this dispatch claim",
        )
    candidate, source = resolve_expected_candidate(workflow_id, claim)
    from .wrapup_delivery import is_current_round
    if (source == 'frozen' and is_current_round(get_store(), workflow_id, candidate)
            and candidate != delivery_record._body_value(effective, 'candidate_sha')):
        # A newly frozen candidate is being certified; the previous delivery
        # remains history until the fresh gates establish its replacement.
        return {'candidate_sha': candidate, 'identity_source': 'frozen'}
    return effective


def validate_supersedes_for_launch(args, node_id, store):
    """Require an explicit predecessor when a node has an open replacement obligation."""
    if not getattr(args, "workflow_id", None) or not node_id:
        return
    if getattr(args, "supersedes", None):
        return
    node_tasks = [
        task
        for task in store.list_tasks(workflow_id=args.workflow_id)
        if task.get("node") == node_id or task.get("stage") == node_id
    ]
    unreplaced = [
        task["task_id"]
        for task in node_tasks
        if task.get("status") == "superseded"
        and task.get("replacement_pending")
        and not task.get("superseded_by")
    ]
    if unreplaced:
        raise ValueError(
            f"Node '{node_id}' has pending replacement obligation(s): {', '.join(unreplaced)}. "
            "Relaunch must explicitly specify --supersedes <prev_task_id> to close the lineage chain."
        )


def preflight_baseline_freshness(
    args, source_repo, base_branch, *, get_store, record_router_failure
):
    """Fail fast on a stale claimed candidate before launch resources are created."""
    node_id = getattr(args, "node", "") or getattr(args, "stage", "")
    claim = str(getattr(args, "candidate_sha", None) or "").strip()
    onto = str(getattr(args, "onto", None) or "").strip()
    if not claim and not onto:
        return None

    from herdr import launch_baseline

    probe = launch_baseline.probe_claim_freshness(
        source_repo,
        base_branch=base_branch,
        claim_sha=claim,
        onto_branch=onto or None,
    )
    if probe["status"] in ("fresh", "skipped"):
        return probe
    if probe["status"] == "unknown":
        print(
            "[BASELINE PREFLIGHT] baseline freshness unknown "
            f"({probe.get('detail')}); proceeding, FR-6.2 stays fail-closed."
        )
        return probe

    verdict = launch_baseline.decide_baseline_failfast(
        dev_tip=probe["dev_tip"],
        claim_sha=probe["claim_sha"],
        behind_by=probe["behind_by"],
        claim_label=onto or "candidate",
    )
    detail = f"{verdict['message']} {verdict['remediation']}"[:1000]
    try:
        get_store().record_event(
            "baseline_stale_refused",
            {
                "workflow_id": getattr(args, "workflow_id", ""),
                "task_id": getattr(args, "task_id", ""),
                "stage": node_id,
                "failure_code": "baseline_stale",
                "behind_by": verdict["behind_by"],
                "dev_tip": verdict["dev_tip"],
                "claim_sha": verdict["claim_sha"],
                "detail": detail[:500],
                "actionable": True,
                "remediation": verdict["remediation"],
            },
            workflow_id=getattr(args, "workflow_id", ""),
            node_id=node_id,
            task_id=getattr(args, "task_id", ""),
            source="herdr-task",
        )
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error) as exc:
        print(
            f"[BASELINE EVENT WARN] rejection audit unavailable: {exc}", file=sys.stderr
        )

    record_router_failure(
        args,
        node_id,
        detail,
        reason="baseline_stale",
        failure_code="baseline_stale",
        remediation=verdict["remediation"],
    )
    # 拒绝必须可听见:wf-haflow-1022-01 的 baseline_stale 拒绝行静默躺了
    # 1.5 小时才被人工发现。通知是旁路,best-effort,绝不改变拒绝语义。
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        notifier.notify(
            "Herdr Factory · 基线过期拒绝",
            f"{getattr(args, 'workflow_id', 'unknown')} · baseline_stale",
            f"{verdict['message']}\n{verdict['remediation']}",
            url=notifier.build_console_url(
                workflow_id=getattr(args, "workflow_id", ""),
                task_id=getattr(args, "task_id", ""),
            ),
        )
    except Exception as exc:
        print(f"[BASELINE NOTIFY WARN] {exc}", file=sys.stderr)
    print(
        f"[BASELINE STALE] workflow={getattr(args, 'workflow_id', '')} "
        f"task={getattr(args, 'task_id', '')}; {detail}; "
        "launch refused before creating clone/Pane (fail-fast)."
    )
    raise SystemExit(2)


def _canonical_commit(sha, source_repo, *, run=subprocess.run):
    sha = str(sha or "").strip()
    if not sha:
        return ""
    try:
        probe = run(
            [
                "git",
                "-C",
                str(source_repo),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{sha}^{{commit}}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return ""
    if probe.returncode != 0:
        return ""
    return (probe.stdout or "").strip().lower()


def same_commit(left, right, source_repo, *, run=subprocess.run):
    """Compare canonical commit IDs, requiring Git to resolve both claims."""
    left = str(left or "").strip().lower()
    right = str(right or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    full_left = _canonical_commit(left, source_repo, run=run)
    full_right = _canonical_commit(right, source_repo, run=run)
    return bool(full_left and full_right and full_left == full_right)


def validate_test_delivery_baseline(
    args, candidate_sha, baseline_commit, source_repo, *, get_store, run=subprocess.run
):
    """Require the verifier checkout to be exactly the selected candidate."""
    candidate_sha = str(candidate_sha or "").strip()
    baseline_commit = str(baseline_commit or "").strip()
    if (
        candidate_sha
        and baseline_commit
        and same_commit(candidate_sha, baseline_commit, source_repo, run=run)
    ):
        return
    try:
        get_store().record_event(
            "test_baseline_rejected",
            {
                "candidate_sha": candidate_sha,
                "baseline_commit": baseline_commit,
                "stage": getattr(args, "node", "") or getattr(args, "stage", ""),
                "failure_code": "baseline_unproven",
                "actionable": True,
                "remediation": "rebase the test/review clone onto the recorded delivery candidate and relaunch",
            },
            workflow_id=args.workflow_id,
            node_id=getattr(args, "node", "") or getattr(args, "stage", ""),
            task_id=args.task_id,
            source="herdr-task",
        )
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error) as exc:
        print(
            f"[BASELINE EVENT WARN] could not persist rejection: {exc}", file=sys.stderr
        )
    print(
        f"[BASELINE REJECTED] test/review task {args.task_id} baseline "
        f"{baseline_commit} does not contain candidate {candidate_sha}; "
        "launch refused (FR-6.2 fail-closed)."
    )
    raise SystemExit(2)


def validate_selective_replacement_baseline(
    args, candidate_sha, baseline_commit, source_repo, *, get_store, run=subprocess.run
):
    """Require replacement checkout HEAD to equal the claimed candidate."""
    candidate_sha = str(candidate_sha or "").strip()
    baseline_commit = str(baseline_commit or "").strip()
    if (
        candidate_sha
        and baseline_commit
        and same_commit(candidate_sha, baseline_commit, source_repo, run=run)
    ):
        return
    try:
        get_store().record_event(
            "selective_baseline_rejected",
            {
                "candidate_sha": candidate_sha,
                "baseline_commit": baseline_commit,
                "stage": getattr(args, "node", "") or getattr(args, "stage", ""),
                "onto_branch": str(getattr(args, "onto", None) or ""),
                "failure_code": "replacement_baseline_mismatch",
                "actionable": True,
                "remediation": "re-freeze the candidate and relaunch the replacement onto the frozen branch",
            },
            workflow_id=args.workflow_id,
            node_id=getattr(args, "node", "") or getattr(args, "stage", ""),
            task_id=args.task_id,
            source="herdr-task",
        )
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error) as exc:
        print(
            f"[BASELINE EVENT WARN] could not persist rejection: {exc}", file=sys.stderr
        )
    print(
        f"[BASELINE REJECTED] replacement task {args.task_id} baseline "
        f"{baseline_commit} != claimed candidate {candidate_sha}; "
        "launch refused (selective-replan fail-closed)."
    )
    raise SystemExit(2)


def validate_test_review_baseline(
    args,
    baseline_commit,
    source_repo,
    *,
    get_store,
    resolve_expected_candidate,
    run=subprocess.run,
):
    from herdr import delivery_record, workflow_docs

    notes = workflow_docs.load_notes(args.workflow_id)
    try:
        effective = delivery_record.select_effective_delivery(notes)
    except delivery_record.DeliveryAmbiguityError as exc:
        print(f"[DELIVERY AMBIGUOUS] {exc}; launch refused")
        sys.exit(2)
    if effective is not None:
        base = str(
            effective.get("base")
            or delivery_record._body_value(effective, "base")
            or effective.get("base_sha")
            or ""
        )
        effective = delivery_record.select_effective_delivery(
            workflow_docs.annotate_notes(notes, current_base_sha=base or None)
        )

    claim = str(getattr(args, "candidate_sha", None) or "").strip()
    from .wrapup_delivery import is_current_round
    if claim and is_current_round(get_store(), args.workflow_id, claim):
        effective = None
    if effective is not None:
        candidate = str(effective.get("candidate_sha") or "")
        if not candidate:
            for line in (effective.get("body") or "").splitlines():
                if line.startswith("candidate_sha:"):
                    candidate = line.split(":", 1)[1].strip()
                    break
        if not candidate:
            candidate = str(effective.get("base_sha") or "")
        if claim and not same_commit(claim, candidate, source_repo, run=run):
            validate_test_delivery_baseline(
                args,
                candidate,
                "claim_mismatch:" + claim,
                source_repo,
                get_store=get_store,
                run=run,
            )
    else:
        candidate, identity_source = resolve_expected_candidate(args.workflow_id, claim)
        if identity_source != "frozen":
            validate_test_delivery_baseline(
                args,
                "",
                baseline_commit,
                source_repo,
                get_store=get_store,
                run=run,
            )
    validate_test_delivery_baseline(
        args,
        candidate,
        baseline_commit,
        source_repo,
        get_store=get_store,
        run=run,
    )


def validate_allocated_launch_baselines(
    args,
    node_id,
    baseline_commit,
    source_repo,
    onto_branch,
    *,
    clone_path,
    pane_id,
    pane_source,
    validate_test_review,
    validate_selective,
    reclaim,
    get_store,
    workflow_config_for=_default_workflow_config,
):
    """Run post-worker baseline gates and reclaim unregistered resources on refusal."""
    verifies_candidate = _verifier_candidate_gate(args.workflow_id, node_id, workflow_config_for)
    if verifies_candidate:
        try:
            validate_test_review(args, baseline_commit, source_repo)
        except SystemExit:
            reclaim(
                args, clone_path=clone_path, pane_id=pane_id, pane_source=pane_source
            )
            raise
        except (
            ImportError,
            AttributeError,
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            _reject_allocated_baseline(
                args,
                node_id,
                "test_baseline_rejected",
                "test/review",
                exc,
                clone_path,
                pane_id,
                pane_source,
                reclaim,
                get_store,
            )

    claim = str(getattr(args, "candidate_sha", None) or "").strip()
    if not verifies_candidate and claim and onto_branch:
        try:
            validate_selective(args, claim, baseline_commit, source_repo)
        except SystemExit:
            reclaim(
                args, clone_path=clone_path, pane_id=pane_id, pane_source=pane_source
            )
            raise
        except (
            ImportError,
            AttributeError,
            OSError,
            ValueError,
            RuntimeError,
            sqlite3.Error,
            subprocess.SubprocessError,
        ) as exc:
            _reject_allocated_baseline(
                args,
                node_id,
                "selective_baseline_rejected",
                "replacement",
                exc,
                clone_path,
                pane_id,
                pane_source,
                reclaim,
                get_store,
            )


def _reject_allocated_baseline(
    args,
    node_id,
    event_type,
    label,
    error,
    clone_path,
    pane_id,
    pane_source,
    reclaim,
    get_store,
):
    reclaim(args, clone_path=clone_path, pane_id=pane_id, pane_source=pane_source)
    try:
        get_store().record_event(
            event_type,
            {
                "stage": node_id,
                "failure_code": "baseline_check_unavailable",
                "detail": str(error)[:300],
                "actionable": True,
                "remediation": "repair workflow delivery metadata and relaunch",
            },
            workflow_id=args.workflow_id,
            node_id=node_id,
            task_id=args.task_id,
            source="herdr-task",
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        AttributeError,
        sqlite3.Error,
    ) as event_exc:
        print(
            f"[BASELINE EVENT WARN] could not persist rejection: {event_exc}",
            file=sys.stderr,
        )
    if label == "test/review":
        message = (
            f"[BASELINE REJECTED] could not prove test/review baseline: {error}; "
            "launch refused (FR-6.2 fail-closed)."
        )
    else:
        message = (
            f"[BASELINE REJECTED] could not prove replacement baseline: {error}; "
            "launch refused (selective-replan fail-closed)."
        )
    print(message)
    sys.exit(2)
