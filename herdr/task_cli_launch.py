"""Launch-side CLI operations for task evaluation setup and refusal recovery."""

import json
import sqlite3
import sys
from pathlib import Path


def launch_task(args, *, deps):
    """Serialize launch admission and settle any rejected reservation."""
    workflow_launch_lock = deps["workflow_launch_lock"]
    NodeLaunchBusy = deps["NodeLaunchBusy"]
    record_launch_rejection = deps["record_launch_rejection"]
    get_store = deps["get_store"]
    launch = deps["launch"]
    source = getattr(args, "source", None)
    entered = False

    def run_launch():
        nonlocal entered
        entered = True
        try:
            return launch(args)
        except BaseException:
            settled = record_launch_rejection(
                get_store(), args, reason="launch_preallocation_rejected",
                resources_absent=not getattr(args, "_launch_resources_started", False),
            )
            if settled and getattr(args, "_launch_reservation_acquired", False):
                deps["release_agent_reservation"](args.task_id)
            raise

    try:
        with workflow_launch_lock(get_store().db_path, args.workflow_id):
            if not source:
                return run_launch()
            with deps["GitOperationLock"](source):
                deps["ensure_no_git_processes"](source)
                return run_launch()
    except (deps["GitOperationBusyError"], deps["GitProcessActiveError"], NodeLaunchBusy) as exc:
        if not entered:
            record_launch_rejection(
                get_store(), args, reason="launch_lock_busy", resources_absent=True
            )
        print(f"[LAUNCH BUSY] {exc}")
        sys.exit(75)


def prepare_launch_context(args, *, load_tasks, project_for_workflow,
                           execution_fields_from_workflow, new_run_id):
    """Resolve launch identity before any resource or durable state is created."""
    raw_policy = getattr(args, "agent_policy", None)
    if raw_policy:
        try:
            policy = json.loads(raw_policy)
        except ValueError as exc:
            print(f"Invalid --agent-policy JSON: {exc}")
            sys.exit(2)
        if policy is not None and not isinstance(policy, dict):
            print("--agent-policy must be a JSON object")
            sys.exit(2)

    data = load_tasks()
    node_id = getattr(args, "node", None) or getattr(args, "stage", None)
    if not node_id:
        print("Error: either --node or --stage is required")
        sys.exit(1)
    run_id = getattr(args, "run_id", None) or new_run_id()
    args.run_id = run_id

    # Pane topology stays deferred until routing accepts the task.
    project = project_for_workflow(args.workflow_id)
    if not project:
        print(f"[WORKFLOW NOT FOUND] {args.workflow_id}; refusing launch")
        sys.exit(2)

    execution_id = project.get('execution_id')
    if execution_id:
        requested_execution = getattr(args, 'execution_id', None)
        if requested_execution and requested_execution != execution_id:
            print('[EXECUTION IDENTITY REJECTED] workflow generation mismatch', file=sys.stderr)
            sys.exit(2)
        args.execution_id = execution_id

    return (data, node_id, run_id, project, raw_policy,
            policy if raw_policy else None,
            execution_fields_from_workflow(project))


def resolve_launch_agent(args, node_id, store, *, choose_agent, workflow_config_for,
                         is_isolation_rejection, record_router_failure_task,
                         release_agent_reservation):
    """Resolve an eligible Agent and preserve launch rejection lifecycle behavior."""
    requested_agent = getattr(args, "agent", None) or "auto"
    opt_out = bool(getattr(args, "allow_reuse", False))
    reason = str(getattr(args, "reuse_reason", "") or "").strip()
    if opt_out and not reason:
        print(
            "Isolation opt-out requires --reuse-reason "
            "(R9: reason field mandatory, no approval needed)"
        )
        sys.exit(2)

    def route():
        return choose_agent(
            args.workflow_id,
            node_id,
            args.task_type,
            requested_agent,
            reservation_key=args.task_id,
            run_id=getattr(args, "run_id", None),
            dispatch_role=args.dispatch_role,
        )

    if opt_out:
        def patched_config(workflow_id, _original=workflow_config_for,
                           _node=node_id, _reason=reason):
            config = _original(workflow_id)
            if not config or workflow_id != args.workflow_id:
                return config
            try:
                import copy
                config = copy.deepcopy(config)
                for node in config.get("nodes", []):
                    if node.get("id") == _node:
                        policy = dict(node.get("agent_policy") or {})
                        policy["allow_reuse_implementation_agents"] = True
                        policy["reuse_reason"] = _reason
                        node["agent_policy"] = policy
            except (TypeError, AttributeError, ValueError):
                pass
            return config

        import herdr.agent_router as router
        import herdr.projects as projects
        original_router_config = router.workflow_config_for
        original_project_config = projects.workflow_config_for
        router.workflow_config_for = patched_config
        projects.workflow_config_for = patched_config
        try:
            try:
                resolved_agent = route()
                return resolved_agent, requested_agent
            except RuntimeError as exc:
                if is_isolation_rejection(exc):
                    try:
                        record_router_failure_task(args, node_id, exc)
                    except RuntimeError as persist_exc:
                        print(f"[ROUTER LIFECYCLE ERROR] {persist_exc}")
                        sys.exit(2)
                    print(f"[ROUTER OPT-OUT REJECTED] {exc}")
                    sys.exit(2)
                _release_after_router_rejection(args, node_id, store, exc,
                                                release_agent_reservation,
                                                record_unexpected=False,
                                                record_router_failure_task=record_router_failure_task)
        finally:
            projects.workflow_config_for = original_project_config
            router.workflow_config_for = original_router_config
    else:
        try:
            resolved_agent = route()
            return resolved_agent, requested_agent
        except RuntimeError as exc:
            if is_isolation_rejection(exc):
                try:
                    record_router_failure_task(args, node_id, exc)
                except RuntimeError as persist_exc:
                    print(f"[ROUTER LIFECYCLE ERROR] {persist_exc}")
                    sys.exit(2)
                print(f"[ROUTER REJECTED] {exc}")
                print(
                    "Isolation is fail-closed. Re-launch with explicit "
                    "--allow-reuse-implementation-agents --reuse-reason '<why>' "
                    "to opt out (the opt-out audit must persist)."
                )
                sys.exit(2)
            _release_after_router_rejection(args, node_id, store, exc,
                                            release_agent_reservation,
                                            record_unexpected=True,
                                            record_router_failure_task=record_router_failure_task)


def _release_after_router_rejection(args, node_id, store, error, release_agent_reservation, *,
                                    record_unexpected, record_router_failure_task):
    intent = getattr(args, "_launch_intent", None)
    if intent:
        try:
            from herdr.task_resources import abort_launch_intent
            abort_launch_intent(store, intent, reason="router_rejected")
        except Exception:
            pass
    try:
        release_agent_reservation(args.task_id)
    except Exception:
        pass
    if record_unexpected:
        from herdr.agent_router import RouterHealthRejection, RouterPolicyRejection
        if not isinstance(error, (RouterHealthRejection, RouterPolicyRejection)):
            record_router_failure_task(args, node_id, error)
    print(f"[ROUTER REJECTED] {error}")
    if "preflight" in str(error).lower():
        print("Run './bin/herdr-deep-preflight --deep' to inspect agent connectivity and credentials.")
    sys.exit(2)


def auto_init_task_loop(clone_path, goal, acceptance_list, test_cmd=None,
                        lint_cmd=None, repro_cmd=None, node=None, task_type=None):
    """Initialize the evaluator contract for a newly launched task clone."""
    if not test_cmd:
        if task_type in {"docs"}:
            return
        if node and any(key in str(node).lower() for key in ("review", "design", "doc", "wrapup")):
            return

    try:
        from herdr.evaluator import init_loop
    except Exception:
        return

    acceptance = "\n".join(f"- {item}" for item in acceptance_list) if acceptance_list else ""
    clone = Path(clone_path)
    test_mode = "run"
    if not test_cmd:
        from herdr.evaluator import select_default_test_command

        markers = {
            "package_json": (clone / "package.json").exists(),
            "pytest_ini": (clone / "pytest.ini").exists(),
            "tests_dir": (clone / "tests").is_dir(),
            "cargo_toml": (clone / "Cargo.toml").exists(),
            "pom_xml": (clone / "pom.xml").exists(),
            "build_gradle": (clone / "build.gradle").exists()
            or (clone / "build.gradle.kts").exists(),
        }
        decision = select_default_test_command(markers)
        if "refused" in decision:
            return decision
        test_cmd = decision["command"]
        if test_cmd == "pytest":
            # INV-EVAL-04: the default must fit its own evaluation budget.
            # A cheap collection probe (no test execution) decides whether a
            # CI-scale suite switches the default to a labelled collection
            # smoke; the probe is advisory and any failure keeps the default.
            from herdr.evaluator import apply_suite_scale_policy, probe_suite_size

            policy = apply_suite_scale_policy(test_cmd, probe_suite_size(clone))
            test_cmd = policy["command"]
            test_mode = policy["mode"]

    if not lint_cmd:
        if (clone / "package.json").exists():
            try:
                package = json.loads((clone / "package.json").read_text(encoding="utf-8"))
                if "lint" in package.get("scripts", {}):
                    lint_cmd = "npm run lint"
            except Exception:
                pass
        elif (clone / "pyproject.toml").exists():
            lint_cmd = "ruff check --output-format json ."

    try:
        init_loop(
            target_dir=clone,
            goal=goal,
            acceptance=acceptance,
            test_cmd=test_cmd or "pytest",
            lint_cmd=lint_cmd or "true",
            repro_cmd=repro_cmd or "",
            capture_baseline=True,
            test_mode=test_mode,
        )
    except TimeoutError as exc:
        print(f"[HERDR_LOOP_WARN] Failed to snapshot lint baseline: {exc}")
        return
    except Exception as exc:
        print(f"[HERDR_LOOP_WARN] Failed to auto-init loop: {exc}")
        return


def refuse_loop_launch(args, refusal, *, clone_path, pane_id, pane_source,
                       reclaim_resources, get_store):
    """Reclaim unregistered launch resources and persist a recoverable refusal."""
    reclaim_resources(
        args,
        clone_path=clone_path,
        pane_id=pane_id,
        pane_source=pane_source,
    )
    try:
        get_store().record_event(
            "test_cmd_refused",
            {
                "refusal_code": refusal.get("refused"),
                "detail": str(refusal.get("detail", ""))[:300],
                "actionable": True,
                "remediation": "relaunch with an explicit --test-cmd for the task's stack",
            },
            workflow_id=getattr(args, "workflow_id", None),
            node_id=getattr(args, "node", None),
            source="herdr-task",
        )
    except (OSError, ValueError, RuntimeError, AttributeError, sqlite3.Error) as exc:
        print(f"[TEST CMD REFUSED WARN] could not persist refusal event: {exc}",
              file=sys.stderr)
    print(
        f"[TEST CMD REFUSED] {refusal.get('refused')}: "
        f"{refusal.get('detail', '')}"
    )
    sys.exit(2)


def record_router_failure_task(args, node_id, error, *, get_store, new_run_id,
                               now, release_agent_reservation, reason="router_isolation_rejected",
                               failure_code=None, remediation=None):
    """Persist the failed task and audit record for a rejected Agent route."""
    timestamp = now()
    task_id = getattr(args, "task_id", "")
    workflow_id = getattr(args, "workflow_id", "")
    detail = str(error)[:1000]
    launch_refused = reason == "baseline_stale"
    status = "superseded" if launch_refused else "failed"
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "run_id": getattr(args, "run_id", None) or new_run_id(),
        "node": node_id,
        "stage": node_id,
        "agent": getattr(args, "agent", None) or "auto",
        "status": status,
        # 半写身份是"砖死行"的根因(wf-haflow-1022-01 r5/r8):行带 run_id 不带
        # execution_id,而持久化 launch_intent 正相反,身份护栏随后拒绝该行的
        # 一切保存(含 supersede),节点完成判定被永久阻塞。失败登记行必须
        # 与其意图同样身份完整。
        "execution_id": getattr(args, "execution_id", None),
        "blocker": detail if not remediation or remediation in detail else f"{detail} {remediation}"[:2000],
        "pane_id": "",
        "clone_path": "",
        "integration_mode": getattr(args, "integration_mode", "none"),
        "goal": getattr(args, "goal", ""),
        "acceptance_criteria": list(getattr(args, "acceptance", []) or []),
        "failure_reason": reason,
        "failure_detail": detail,
        "actionable": True,
        "pane_dispatched": False,
        "created_at": timestamp,
        "updated_at": timestamp,
        "status_history": [{
            "from": "pending",
            "to": status,
            "reason": reason,
            "source": "herdr-task",
            "timestamp": timestamp,
            "detail": detail,
        }],
    }
    if launch_refused:
        task.update(launch_not_started=True, replacement_pending=True)
    store = get_store()
    launch_intent = getattr(args, "_launch_intent", None)
    if launch_intent:
        task.update(
            dispatch_role=launch_intent["role"],
            dispatch_round=launch_intent["dispatch_round"],
            candidate_sha=launch_intent["candidate_sha"],
            artifact_mode=getattr(args, "artifact_mode", None),
            launch_intent_id=launch_intent["intent_id"],
        )
        if launch_intent.get("execution_id"):
            # 意图是身份权威:行必须与之一致,否则任何后续保存都会被
            # 登记身份护栏拒绝(半写身份砖死行)。
            task["execution_id"] = launch_intent["execution_id"]
    if launch_intent and launch_intent.get("supersedes"):
        task["supersedes"] = launch_intent["supersedes"]
    if launch_intent and launch_intent.get("dispatch_operation_id") is not None:
        task.update(
            dispatch_operation_id=launch_intent["dispatch_operation_id"],
            execution_id=launch_intent["execution_id"],
        )
    store.save_task(task)
    if launch_intent:
        from herdr.task_resources import abort_launch_intent, finish_launch_intent
        if launch_refused:
            abort_launch_intent(store, launch_intent, reason=reason)
        else:
            finish_launch_intent(store, launch_intent)
    try:
        release_agent_reservation(task_id)
    except (OSError, ValueError, RuntimeError, AttributeError):
        pass

    payload = {
        "workflow_id": workflow_id,
        "run_id": task.get("run_id"),
        "task_id": task_id,
        "error": detail,
        "stage": node_id,
        "result": "failed",
        "failure_code": failure_code or (
            "router_opt_out_audit_failed" if "audit" in detail.lower() else "empty_review_pool"
        ),
        "actionable": True,
        "pane_dispatched": False,
        "remediation": remediation or (
            "add an eligible Agent or explicitly opt out with "
            "--allow-reuse-implementation-agents --reuse-reason"
        ),
    }
    lifecycle_errors = []
    try:
        store.record_event(
            "router_isolation_rejected",
            payload,
            workflow_id=workflow_id,
            node_id=node_id,
            task_id=task_id,
            run_id=task.get("run_id"),
            source="herdr-task",
        )
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        lifecycle_errors.append(f"event: {exc}")
        print(f"[ROUTER AUDIT WRITE ERROR] {exc}")
    try:
        store.update_workflow_metadata(workflow_id, {"last_dispatch_failure": payload})
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        lifecycle_errors.append(f"workflow: {exc}")
    if lifecycle_errors:
        raise RuntimeError(
            "router failure lifecycle persistence failed: " + "; ".join(lifecycle_errors)
        )
    return task
