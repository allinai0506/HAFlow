"""Start a task worker and persist resource ownership evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path


def run_launch_worker(args, node_id, run_id, store, launch_intent, planned_clone,
                      resolved_agent, exec_fields, base_branch, onto_branch,
                      parent_pane, prebuilt_pane, herdr_root, *, run_worker,
                      get_store):
    """Allocate worker resources, fail closed on uncertain effects, and return its result."""
    try:
        from herdr.workspace_trust import (
            ensure_workspace_trust as ensure_clone_trust,
            preseed_trust_target,
        )
        preseed_path = preseed_trust_target(str(planned_clone), resolved_agent)
        if preseed_path is not None:
            ensure_clone_trust(preseed_path, [resolved_agent])
    except Exception as exc:
        print(f"[WORKSPACE TRUST WARN] pre-seed failed for {args.task_id}: {exc}")

    worker_script = Path(herdr_root) / "services" / "herdr-worker.py"
    if not worker_script.exists():
        worker_script = Path(os.path.expanduser("~/HAFlow/services/herdr-worker.py"))

    worker_cmd = [
        str(worker_script), "--task-id", args.task_id, "--source", args.source,
        "--agent", resolved_agent, "--task-type", args.task_type,
    ]
    if exec_fields["execution_mode"] == "context":
        worker_cmd += [
            "--execution-mode", "context", "--context-json",
            json.dumps(exec_fields.get("context") or {}),
        ]
    else:
        worker_cmd += ["--base-branch", base_branch]

    try:
        from herdr import workflow_docs
        worker_cmd += ["--shared-docs", str(workflow_docs.workflow_docs_dir(args.workflow_id))]
    except Exception:
        pass
    if onto_branch:
        worker_cmd += ["--onto", onto_branch]
    if getattr(args, "candidate_sha", None):
        worker_cmd += ["--candidate-sha", args.candidate_sha]
    if prebuilt_pane:
        worker_cmd += ["--pane-id", prebuilt_pane]
    else:
        worker_cmd += ["--parent-pane", parent_pane]

    try:
        worker = run_worker(
            worker_cmd + ["--run-id", run_id, "--launch-intent-id", launch_intent["intent_id"]],
            text=True, capture_output=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        store.record_event(
            "launch_recovery_required",
            {
                "task_id": args.task_id, "run_id": run_id,
                "side_effects": "unknown", "failure_type": "worker_timeout",
                "recovery_required": True, "intent_id": launch_intent["intent_id"],
            },
            workflow_id=args.workflow_id, node_id=node_id, task_id=args.task_id,
            run_id=run_id, source="herdr-task",
        )
        print("[WORKER TIMEOUT] resource effects unknown; reconcile launch intent before retry")
        sys.exit(75)

    if worker.returncode != 0:
        _record_worker_failure_health(store, args, resolved_agent, worker)
        _record_worker_failure_intent(store, get_store, args, node_id, run_id,
                                      launch_intent, worker.stderr)
        print("Worker failed:", worker.stderr.strip() or worker.stdout.strip())
        sys.exit(1)

    worker_result = None
    for line in worker.stdout.splitlines():
        if line.startswith("HERDR_WORKER_RESULT="):
            worker_result = json.loads(line.split("=", 1)[1])
            break
    if not worker_result:
        print("Worker result not found")
        print(worker.stdout)
        sys.exit(1)

    from herdr.task_resources import record_launch_resources
    launch_intent = record_launch_resources(store, launch_intent, {
        "clone_path": worker_result.get("clone"),
        "pane_id": worker_result.get("pane_id"),
        "pane_source": worker_result.get("pane_source", "dynamic"),
        "run_id": run_id,
        "agent_session_id": worker_result.get("agent_session_id"),
    })
    return worker_result, launch_intent


def _record_worker_failure_health(store, args, resolved_agent, worker):
    try:
        from herdr.workspace_trust import (
            is_trust_required_failure,
            merge_trust_failure_unhealthy,
        )
        worker_output = (worker.stderr or "") + "\n" + (worker.stdout or "")
        if is_trust_required_failure(worker_output):
            workflow = store.get_workflow(args.workflow_id) or {}
            unhealthy, healthy = merge_trust_failure_unhealthy(workflow, resolved_agent)
            store.update_workflow_metadata(args.workflow_id, {
                "healthy_agents": healthy, "unhealthy_agents": unhealthy,
            })
            try:
                from herdr.state_store import sync_workflows_projection
                sync_workflows_projection(store=store)
            except Exception:
                pass
            print(f"[ROUTER HEALTH] {resolved_agent} marked TRUST_REQUIRED; auto retry will pick another agent")
    except Exception as exc:
        print(f"[ROUTER HEALTH WARN] trust failure write-back failed: {exc}")


def _record_worker_failure_intent(store, get_store, args, node_id, run_id,
                                  launch_intent, stderr):
    for line in stderr.splitlines():
        if not line.startswith("HERDR_WORKER_FAILURE="):
            continue
        try:
            failure = json.loads(line.split("=", 1)[1])
            if failure.get("task_id") != args.task_id or failure.get("run_id") != run_id:
                continue
            if (failure.get("recovery_required") is False
                    and failure.get("disposition") in ("rolled_back", "pane_reclaimed")):
                from herdr.task_resources import abort_launch_intent
                abort_launch_intent(store, launch_intent,
                                   reason=failure.get("failure_type", "worker_startup_failed"))
            else:
                from herdr.task_resources import record_launch_resources
                record_launch_resources(store, launch_intent, {
                    "pane_id": failure.get("pane_id"), "clone_path": failure.get("clone"),
                    "run_id": run_id, "agent_session_id": failure.get("agent_session_id"),
                    "agent_name": failure.get("agent_name"), "ownership": "unknown",
                    "recovery_required": True, "allocation_failed": True,
                })
                get_store().record_event(
                    "launch_recovery_required", failure,
                    workflow_id=args.workflow_id, node_id=node_id,
                    task_id=args.task_id, run_id=run_id, source="herdr-task",
                )
        except (ValueError, TypeError):
            pass
