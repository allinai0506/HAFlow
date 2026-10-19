"""Launch orchestration after identity preparation."""

import os
import subprocess
import sys
from pathlib import Path


def launch_task_orchestrated(args, *, deps):
    BranchOwnershipError = deps["BranchOwnershipError"]
    HERDR_ROOT = deps["HERDR_ROOT"]
    TrajectoryLedger = deps["TrajectoryLedger"]
    _apply_execution_identity = deps["_apply_execution_identity"]
    _capacity_definition = deps["_capacity_definition"]
    _check_launch_capacity = deps["_check_launch_capacity"]
    _finish_committed_recovery_launch = deps["_finish_committed_recovery_launch"]
    _get_store = deps["_get_store"]
    _init_status_history = deps["_init_status_history"]
    _is_gate_task = deps["_is_gate_task"]
    _is_isolation_rejection = deps["_is_isolation_rejection"]
    _mandatory_experience_gate = deps["_mandatory_experience_gate"]
    _now = deps["_now"]
    _preflight_baseline_freshness = deps["_preflight_baseline_freshness"]
    _preflight_delivery_identity = deps["_preflight_delivery_identity"]
    _reclaim_unregistered_launch_resources = deps[
        "_reclaim_unregistered_launch_resources"
    ]
    _record_router_failure_task = deps["_record_router_failure_task"]
    _refuse_loop_launch = deps["_refuse_loop_launch"]
    _validate_selective_replacement_baseline = deps[
        "_validate_selective_replacement_baseline"
    ]
    _validate_supersedes_for_launch = deps["_validate_supersedes_for_launch"]
    _validate_test_review_baseline = deps["_validate_test_review_baseline"]
    acquire_pane_for_task = deps["acquire_pane_for_task"]
    auto_init_task_loop = deps["auto_init_task_loop"]
    choose_agent = deps["choose_agent"]
    dispatch_task = deps["dispatch_task"]
    ensure_branch_available = deps["ensure_branch_available"]
    ensure_stage_topology = deps["ensure_stage_topology"]
    execution_fields_from_workflow = deps["execution_fields_from_workflow"]
    find_task = deps["find_task"]
    load_tasks = deps["load_tasks"]
    new_run_id = deps["new_run_id"]
    pinned_local_onto_matches = deps["pinned_local_onto_matches"]
    project_for_workflow = deps["project_for_workflow"]
    record_trajectory_event_best_effort = deps["record_trajectory_event_best_effort"]
    release_agent_reservation = deps["release_agent_reservation"]
    supersede_task = deps["supersede_task"]
    validate_integration_for_execution = deps["validate_integration_for_execution"]
    validate_onto_for_execution = deps["validate_onto_for_execution"]
    workflow_config_for = deps["workflow_config_for"]
    from herdr.task_cli_launch import prepare_launch_context

    data, node_id, run_id, project, raw_policy, agent_policy, exec_fields = (
        prepare_launch_context(
            args,
            load_tasks=load_tasks,
            project_for_workflow=project_for_workflow,
            execution_fields_from_workflow=execution_fields_from_workflow,
            new_run_id=new_run_id,
        )
    )
    try:
        from herdr.workflow import validate_artifact_contract
        from herdr.task_resources import find_dispatch_task, begin_launch_intent

        store = _get_store()
        definition = _capacity_definition(project)
        from herdr.business_gate import business_gate_blockers

        authority, pinned, owned_tasks = store.read_workflow_snapshot(args.workflow_id)
        missing_business = business_gate_blockers(
            store, authority, pinned or definition, owned_tasks, node_id
        )
        if missing_business:
            raise ValueError(
                "business acceptance unavailable: " + ", ".join(missing_business)
            )
        node = next(
            (n for n in definition.get("nodes", []) if n.get("id") == node_id), {}
        )
        if node.get("delivery_contract") is not None:
            from herdr.task_delivery import validate_contract

            validate_contract(node["delivery_contract"])
        configured_mode = node.get("artifact_mode")
        requested_mode = getattr(args, "artifact_mode", None)
        if configured_mode and requested_mode and (configured_mode != requested_mode):
            raise ValueError(
                "artifact_mode override conflicts with node delivery contract"
            )
        args.artifact_mode = configured_mode or requested_mode
        validate_artifact_contract(
            args.artifact_mode,
            args.integration_mode,
            node_id=node_id,
            task_type=args.task_type,
        )
        predecessor = (
            store.get_task(args.supersedes)
            if getattr(args, "supersedes", None)
            else None
        )
        roles = (node.get("agent_policy") or {}).get("roles") or []
        requested_role = getattr(args, "dispatch_role", None)
        if len(roles) > 1 and (not requested_role) and (not predecessor):
            raise ValueError("parallel role node requires explicit --dispatch-role")
        args.dispatch_role = (
            requested_role or (predecessor or {}).get("dispatch_role") or "worker"
        )
        args.dispatch_round = getattr(args, "dispatch_round", None) or (
            int(predecessor.get("dispatch_round") or 1) + 1 if predecessor else 1
        )
        _validate_supersedes_for_launch(args, node_id, store)
        clone_root = (
            Path(
                os.environ.get("HERDR_CLONES_DIR")
                or Path.home() / ".herdr-controller/clones"
            )
            .expanduser()
            .resolve()
        )
        if (
            Path(args.task_id).name != args.task_id
            or args.task_id in {".", ".."}
            or len(args.task_id) > 128
        ):
            raise ValueError("task_id must be a bounded single workspace name")
        planned_clone = clone_root / args.task_id
        from herdr.domain import get_domain_adapter

        _domain_adapter = get_domain_adapter(
            domain=getattr(args, "domain", None) or node.get("domain"),
            task_type=getattr(args, "task_type", None) or node.get("default_task_type"),
        )
        if node.get("delivery_contract"):
            node["delivery_contract"] = _domain_adapter.prepare_delivery_contract(
                node["delivery_contract"],
                {
                    "task_id": args.task_id,
                    "task_type": getattr(args, "task_type", None),
                    "node_id": node_id,
                    "branch": getattr(args, "onto", None),
                },
            )
        from herdr.task_delivery import validate_fix_retrospective

        validate_fix_retrospective(
            task_id=args.task_id,
            task_type=getattr(args, "task_type", None),
            node_id=node_id,
            contract=node.get("delivery_contract"),
            integration_mode=getattr(args, "integration_mode", None)
            or node.get("integration_mode")
            or node.get("default_integration_mode"),
        )
        candidate = str(getattr(args, "candidate_sha", None) or "").strip()
        onto = getattr(args, "onto", None)
        committed_recovery = bool(
            predecessor and predecessor.get("status") == "committed"
        )
        if committed_recovery:
            if candidate != predecessor.get("commit"):
                raise ValueError(
                    "Committed recovery requires exact predecessor --candidate-sha"
                )
            if onto and onto == predecessor.get("branch"):
                raise ValueError(
                    "Committed recovery cannot use predecessor branch as --onto"
                )
            workflow_execution = (store.get_workflow(args.workflow_id) or {}).get(
                "execution_id"
            )
            if (
                not workflow_execution
                or getattr(args, "execution_id", None) != workflow_execution
                or (
                    predecessor.get("execution_id")
                    and predecessor["execution_id"] != workflow_execution
                )
            ):
                raise ValueError(
                    "Committed recovery requires authoritative workflow execution identity"
                )
        if onto and (not candidate) and (exec_fields["execution_mode"] == "git"):
            source = str(
                Path(project.get("project_root") or args.source).expanduser().resolve()
            )
            remote = subprocess.run(
                [
                    "git",
                    "-C",
                    source,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/origin/{onto}",
                ],
                capture_output=True,
            )
            local = subprocess.run(
                [
                    "git",
                    "-C",
                    source,
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{onto}^{{commit}}",
                ],
                text=True,
                capture_output=True,
            )
            if remote.returncode and local.returncode == 0:
                raise ValueError(
                    f"Local-only --onto {onto} requires --candidate-sha {local.stdout.strip()}; read and verify this frozen commit before relaunching"
                )
        duplicate = find_dispatch_task(
            store,
            args.workflow_id,
            node_id,
            args.dispatch_role,
            candidate,
            args.dispatch_round,
        )
        if duplicate:
            if (
                getattr(args, "dispatch_operation_id", None) is not None
                and duplicate.get("dispatch_operation_id") != args.dispatch_operation_id
            ):
                raise ValueError("existing task belongs to another dispatch operation")
            if committed_recovery:
                _finish_committed_recovery_launch(
                    store, duplicate, predecessor, args.supersede_reason
                )
            print(
                f"[DISPATCH DUPLICATE] proposed={args.task_id} existing={duplicate['task_id']}"
            )
            return
        if find_task(data, args.task_id):
            raise ValueError(
                f"Task ID already exists with another dispatch identity: {args.task_id}"
            )
        validate_integration_for_execution(
            exec_fields["execution_mode"], args.integration_mode
        )
        validate_onto_for_execution(
            exec_fields["execution_mode"], getattr(args, "onto", None)
        )
    except ValueError as exc:
        print(str(exc))
        sys.exit(2)
    if getattr(args, "mandatory_experience_rules", False):
        try:
            _prefetched_experience_suffix, _prefetched_experience_refs = (
                _mandatory_experience_gate(args, node_id)
            )
        except Exception:
            sys.exit(2)
    else:
        _prefetched_experience_suffix = None
        _prefetched_experience_refs = None
    _check_launch_capacity(args, project, data.get("tasks", []), node_id)
    try:
        claim = begin_launch_intent(
            store,
            workflow_id=args.workflow_id,
            node_id=node_id,
            role=args.dispatch_role,
            candidate_sha=candidate,
            dispatch_round=args.dispatch_round,
            task_id=args.task_id,
            supersedes=getattr(args, "supersedes", None),
            dispatch_operation_id=getattr(args, "dispatch_operation_id", None),
            dispatch_attempt=getattr(args, "dispatch_attempt", None),
            run_id=run_id,
            execution_id=getattr(args, "execution_id", None),
        )
    except ValueError as exc:
        print(f"[DISPATCH REJECTED] {exc}")
        sys.exit(2)
    if claim["status"] == "recovery_required":
        from herdr.task_resources import recover_launch_resources

        recovered = recover_launch_resources(store, claim["intent"])
        if recovered["status"] == "resources_absent":
            release_agent_reservation(claim["intent"]["task_id"])
            claim = begin_launch_intent(
                store,
                workflow_id=args.workflow_id,
                node_id=node_id,
                role=args.dispatch_role,
                candidate_sha=candidate,
                dispatch_round=args.dispatch_round,
                task_id=args.task_id,
                supersedes=getattr(args, "supersedes", None),
                dispatch_operation_id=getattr(args, "dispatch_operation_id", None),
                dispatch_attempt=getattr(args, "dispatch_attempt", None),
                run_id=run_id,
                execution_id=getattr(args, "execution_id", None),
            )
    if claim["status"] != "claimed":
        if claim["status"] == "duplicate":
            if committed_recovery:
                _finish_committed_recovery_launch(
                    store, claim["task"], predecessor, args.supersede_reason
                )
            print(
                f"[DISPATCH DUPLICATE] proposed={args.task_id} existing={claim['task']['task_id']}"
            )
            return
        print(
            f"[DISPATCH {claim['status'].upper()}] task={claim['intent']['task_id']}; reconcile intent-owned resources before retrying"
        )
        sys.exit(75)
    launch_intent = claim["intent"]
    args._launch_intent = launch_intent
    from herdr.task_resources import record_launch_resources

    launch_intent = record_launch_resources(
        store,
        launch_intent,
        {"planned_clone_path": str(planned_clone), "run_id": run_id},
    )
    source_repo = os.path.realpath(
        os.path.expanduser(project.get("project_root") or args.source)
    )
    base_branch = project.get("base_branch") or "dev"
    onto_branch = getattr(args, "onto", None)
    if onto_branch:
        try:
            ensure_branch_available(
                onto_branch, data.get("tasks", []), task_id=args.task_id
            )
        except BranchOwnershipError as exc:
            print(f"[BRANCH OWNERSHIP ERROR] {exc}")
            sys.exit(2)
        try:
            pinned_local = pinned_local_onto_matches(
                source_repo, onto_branch, getattr(args, "candidate_sha", None)
            )
        except RuntimeError as exc:
            print(str(exc))
            sys.exit(2)
        if not pinned_local:
            fetch = subprocess.run(
                ["git", "-C", source_repo, "fetch", "origin", onto_branch],
                text=True,
                capture_output=True,
            )
            remote_ref = subprocess.run(
                [
                    "git",
                    "-C",
                    source_repo,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"refs/remotes/origin/{onto_branch}",
                ],
                text=True,
                capture_output=True,
            )
            if fetch.returncode != 0 or remote_ref.returncode != 0:
                print(
                    f"Onto branch not found on origin: {onto_branch}\n{fetch.stderr.strip() or fetch.stdout.strip()}"
                )
                sys.exit(2)
    from herdr.task_cli_launch import resolve_launch_agent

    resolved_agent, requested_agent = resolve_launch_agent(
        args,
        node_id,
        store,
        choose_agent=choose_agent,
        workflow_config_for=workflow_config_for,
        is_isolation_rejection=_is_isolation_rejection,
        record_router_failure_task=_record_router_failure_task,
        release_agent_reservation=release_agent_reservation,
    )
    args._launch_reservation_acquired = True
    _preflight_delivery_identity(
        args.workflow_id,
        node_id,
        args.task_id,
        claim=getattr(args, "candidate_sha", None),
    )
    _preflight_baseline_freshness(args, source_repo, base_branch)
    args._launch_resources_started = True
    stage_config = ensure_stage_topology(args.workflow_id, node_id)
    workspace_id = stage_config["workspace_id"]
    parent_pane = stage_config["anchor_pane_id"]
    prebuilt_pane = acquire_pane_for_task(args.workflow_id, node_id, resolved_agent)
    if args.integration_mode == "git":
        source = os.path.realpath(os.path.expanduser(args.source))
        tracked_status = subprocess.check_output(
            ["git", "-C", source, "status", "--porcelain", "--untracked-files=no"],
            text=True,
        ).strip()
        if tracked_status:
            try:
                from herdr import repo_hygiene as _pre_hygiene

                _warn = _pre_hygiene.launch_precheck_message(
                    porcelain=tracked_status,
                    task_id=args.task_id,
                    integration_mode=args.integration_mode,
                )
            except (ImportError, AttributeError, ValueError):
                _warn = None
            if _warn:
                print(_warn)
            else:
                print(
                    "[PRECHECK WARNING] source has tracked changes; launch continues, integrate will gate with exit 5."
                )
            print(tracked_status)
    from herdr.task_cli_launch_worker import run_launch_worker

    worker_result, launch_intent = run_launch_worker(
        args,
        node_id,
        run_id,
        store,
        launch_intent,
        planned_clone,
        resolved_agent,
        exec_fields,
        base_branch,
        onto_branch,
        parent_pane,
        prebuilt_pane,
        HERDR_ROOT,
        run_worker=subprocess.run,
        get_store=_get_store,
    )
    clone_path = worker_result["clone"]
    branch = worker_result["branch"]
    try:
        ensure_branch_available(branch, data.get("tasks", []), task_id=args.task_id)
    except BranchOwnershipError as exc:
        print(f"[BRANCH OWNERSHIP ERROR] {exc}")
        sys.exit(2)
    baseline_untracked = worker_result.get("baseline_untracked", [])
    baseline_fingerprint = worker_result.get(
        "baseline_fingerprint", {"tracked": {}, "untracked": {}}
    )
    baseline_commit = worker_result.get("baseline_commit")
    recorded_onto = worker_result.get("onto_branch", onto_branch)
    pane_id = worker_result["pane_id"]
    pane_source = worker_result.get("pane_source", "dynamic")
    from herdr.task_cli_launch_preflight import validate_allocated_launch_baselines

    validate_allocated_launch_baselines(
        args,
        node_id,
        baseline_commit,
        source_repo,
        onto_branch,
        clone_path=clone_path,
        pane_id=pane_id,
        pane_source=pane_source,
        validate_test_review=_validate_test_review_baseline,
        validate_selective=_validate_selective_replacement_baseline,
        reclaim=_reclaim_unregistered_launch_resources,
        get_store=_get_store,
    )
    print(f"[WORKER] clone={clone_path}")
    print(f"[WORKER] pane={pane_id}")
    print(f"[WORKER] pane_source={pane_source}")
    print(f"[WORKER] agent={resolved_agent} requested={requested_agent}")
    _loop_refusal = auto_init_task_loop(
        clone_path,
        args.goal,
        args.acceptance,
        test_cmd=getattr(args, "test_cmd", None),
        lint_cmd=getattr(args, "lint_cmd", None),
        repro_cmd=getattr(args, "repro_cmd", None),
        node=node_id,
        task_type=args.task_type,
    )
    if _loop_refusal:
        _refuse_loop_launch(
            args,
            _loop_refusal,
            clone_path=clone_path,
            pane_id=pane_id,
            pane_source=pane_source,
        )
    data = load_tasks()
    now = _now()
    from herdr.task_cli_launch_registration import (
        LaunchRegistration,
        register_launched_task,
    )

    registration = LaunchRegistration(
        args=args,
        now=now,
        node=node,
        node_id=node_id,
        run_id=run_id,
        project=project,
        exec_fields=exec_fields,
        source_repo=source_repo,
        base_branch=base_branch,
        stage_config=stage_config,
        workspace_id=workspace_id,
        pane_id=pane_id,
        pane_source=pane_source,
        resolved_agent=resolved_agent,
        requested_agent=requested_agent,
        clone_path=clone_path,
        branch=branch,
        baseline_commit=baseline_commit,
        onto_branch=onto_branch,
        baseline_untracked=baseline_untracked,
        baseline_fingerprint=baseline_fingerprint,
        recorded_onto=recorded_onto,
        launch_intent=launch_intent,
        worker_result=worker_result,
        predecessor=predecessor,
        committed_recovery=committed_recovery,
    )
    task = register_launched_task(
        registration,
        store,
        apply_execution_identity=_apply_execution_identity,
        init_status_history=_init_status_history,
        is_gate_task=_is_gate_task,
        trajectory_ledger_factory=lambda _path: TrajectoryLedger(
            getattr(_get_store(), "db_path", None)
        ),
        record_trajectory_event=record_trajectory_event_best_effort,
    )
    release_agent_reservation(args.task_id)
    print(f"[REGISTERED] {args.task_id}")
    dispatch_task(
        args.task_id,
        args.prompt,
        prefetched_experience_suffix=_prefetched_experience_suffix,
        prefetched_experience_refs=_prefetched_experience_refs,
    )
    if getattr(args, "supersedes", None):
        if committed_recovery:
            _finish_committed_recovery_launch(
                store, store.get_task(args.task_id), predecessor, args.supersede_reason
            )
        else:
            supersede_task(
                args.supersedes,
                new_task_id=args.task_id,
                reason=args.supersede_reason,
                allow_new_run=True,
            )
