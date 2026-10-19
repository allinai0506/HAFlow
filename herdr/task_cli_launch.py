"""Launch-side CLI operations for task evaluation setup and refusal recovery."""

import json
import sqlite3
import sys
from pathlib import Path


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

    if not lint_cmd:
        if (clone / "package.json").exists():
            try:
                package = json.loads((clone / "package.json").read_text(encoding="utf-8"))
                if "lint" in package.get("scripts", {}):
                    lint_cmd = "npm run lint"
            except Exception:
                pass
        elif (clone / "pyproject.toml").exists():
            lint_cmd = "ruff check ."

    try:
        init_loop(
            target_dir=clone,
            goal=goal,
            acceptance=acceptance,
            test_cmd=test_cmd or "pytest",
            lint_cmd=lint_cmd or "true",
            repro_cmd=repro_cmd or "",
            capture_baseline=True,
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
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "run_id": getattr(args, "run_id", None) or new_run_id(),
        "node": node_id,
        "stage": node_id,
        "agent": getattr(args, "agent", None) or "auto",
        "status": "failed",
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
            "to": "failed",
            "reason": reason,
            "source": "herdr-task",
            "timestamp": timestamp,
            "detail": detail,
        }],
    }
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
    if launch_intent and launch_intent.get("supersedes"):
        task["supersedes"] = launch_intent["supersedes"]
    if launch_intent and launch_intent.get("dispatch_operation_id") is not None:
        task.update(
            dispatch_operation_id=launch_intent["dispatch_operation_id"],
            execution_id=launch_intent["execution_id"],
        )
    store.save_task(task)
    if launch_intent:
        from herdr.task_resources import finish_launch_intent
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
