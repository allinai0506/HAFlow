"""Task lookup, registration and workflow-node resolution CLI helpers."""

import json
import sqlite3
import sys
from pathlib import Path


def load_workflow(workflow_id=None, *, deps):
    get_state_store = deps["get_state_store"]
    workflow_config_for = deps["workflow_config_for"]
    normalize_workflow = deps["normalize_workflow"]
    WORKFLOW_FILE = deps["workflow_file"]
    if workflow_id:
        try:
            store = get_state_store()
            wf = store.get_workflow(workflow_id)
            if wf:
                if wf.get("workflow_file"):
                    try:
                        config = json.loads(
                            Path(wf["workflow_file"]).expanduser().read_text()
                        )
                        if isinstance(config, dict) and config:
                            from herdr.projects import (
                                validate_required_task_scope,
                                _with_node_runtime,
                            )

                            return _with_node_runtime(
                                validate_required_task_scope(
                                    normalize_workflow(config), workflow_id, store=store
                                ),
                                wf,
                            )
                    except OSError:
                        pass
                return normalize_workflow(wf)
        except (ValueError, TypeError):
            raise
        except Exception:
            pass
        workflow = workflow_config_for(workflow_id)
        if workflow:
            return workflow
    with open(WORKFLOW_FILE, "r", encoding="utf-8") as f:
        return normalize_workflow(json.load(f))


def safe_workflow(workflow_id, *, load_workflow):
    """Return a workflow and display label, failing closed on invalid config."""
    if not workflow_id:
        return None, "unknown-workflow"
    try:
        workflow = load_workflow(workflow_id)
        return workflow, workflow.get("name") or workflow_id
    except Exception:
        return {"configuration_error": "workflow_config_invalid"}, workflow_id


def resolve_node(node_id, workflow_id=None, *, deps):
    ensure_node_runtime = deps["ensure_node_runtime"]
    load_workflow = deps["load_workflow"]
    find_node = deps["find_node"]
    if workflow_id:
        try:
            runtime = ensure_node_runtime(workflow_id, node_id)
            wf = load_workflow(workflow_id)
            next_stage = None
            for s in wf.get("stages", []):
                if (s.get("key") or s.get("id")) == node_id:
                    next_stage = s.get("next")
                    break
            return {
                "workspace_id": runtime["workspace_id"],
                "node_id": runtime["node_id"],
                "stage_key": runtime["node_id"],
                "stage_label": runtime["node_label"],
                "node_label": runtime["node_label"],
                "tab_id": runtime["tab_id"],
                "anchor_pane_id": runtime["anchor_pane_id"],
                "depends_on": runtime.get("depends_on", []),
                "next": next_stage,
                "agent_policy": runtime.get("agent_policy", {}),
                "purpose": runtime.get("purpose", ""),
                "default_integration_mode": runtime.get(
                    "default_integration_mode", "none"
                ),
                "default_task_type": runtime.get("default_task_type", "docs"),
                "required_outputs": runtime.get("required_outputs", []),
                "rules": runtime.get("rules", []),
            }
        except Exception:
            pass
    workflow = load_workflow(workflow_id)
    node = find_node(workflow, node_id)
    if node:
        anchor = node.get("anchor_pane_id")
        next_stage = None
        for s in workflow.get("stages", []):
            if (s.get("key") or s.get("id")) == node_id:
                next_stage = s.get("next")
                break
        return {
            "workspace_id": workflow.get("workspace_id", ""),
            "node_id": node["id"],
            "stage_key": node["id"],
            "stage_label": node["label"],
            "node_label": node["label"],
            "tab_id": node.get("tab_id"),
            "anchor_pane_id": anchor,
            "depends_on": node.get("depends_on", []),
            "agent_policy": node.get("agent_policy", {}),
            "next": next_stage,
        }
    for stage in workflow.get("stages", []):
        if stage.get("key") == node_id or stage.get("id") == node_id:
            return {
                "workspace_id": workflow.get("workspace_id", ""),
                "node_id": stage.get("key") or stage.get("id"),
                "stage_key": stage.get("key") or stage.get("id"),
                "stage_label": stage.get("label", ""),
                "node_label": stage.get("label", ""),
                "tab_id": stage.get("tab_id"),
                "anchor_pane_id": stage.get("anchor_pane_id"),
                "next": stage.get("next"),
            }
    raise RuntimeError(f"Unknown workflow node/stage: {node_id}")


def add_task(args, *, deps):
    load_tasks = deps["load_tasks"]
    find_task = deps["find_task"]
    new_run_id = deps["new_run_id"]
    _now = deps["now"]
    _init_status_history = deps["init_status_history"]
    save_tasks = deps["save_tasks"]
    _get_store = deps["get_store"]
    TrajectoryLedger = deps["trajectory_ledger"]
    record_trajectory_event_best_effort = deps["record_trajectory_event"]
    data = load_tasks()
    store = _get_store()
    if find_task(data, args.task_id) or (store and store.get_task(args.task_id)):
        print(f"Task already exists: {args.task_id}")
        sys.exit(1)
    node_id = getattr(args, "node", None) or getattr(args, "stage", None)
    if not node_id:
        print("Error: either --node or --stage is required")
        sys.exit(1)
    task = {
        "task_id": args.task_id,
        "workflow_id": args.workflow_id,
        "run_id": new_run_id(),
        "node": node_id,
        "stage": node_id,
        "workspace_id": args.workspace,
        "pane_id": args.pane,
        "agent": args.agent,
        "clone_path": args.clone,
        "integration_mode": args.integration_mode,
        "status": "pending",
        "goal": args.goal,
        "acceptance_criteria": args.acceptance,
    }
    if getattr(args, "customer", None):
        task["customer"] = str(args.customer).strip()
    if getattr(args, "business_task_type", None):
        task["business_task_type"] = str(args.business_task_type).strip()
    if getattr(args, "mandatory_experience_rules", False):
        task["mandatory_experience_rules"] = True
    now = _now()
    _init_status_history(task, "pending", now)
    data["tasks"].append(task)
    # Save only the newly created task with atomic duplicate rejection (SC-017 & Codex P1)
    try:
        from herdr.state_db import TaskAlreadyExistsError
    except ImportError:
        TaskAlreadyExistsError = RuntimeError
    try:
        save_tasks({"tasks": [task]}, fail_if_exists=True)
    except (TaskAlreadyExistsError, sqlite3.IntegrityError):
        print(f"Task already exists: {args.task_id}")
        sys.exit(1)
    trajectory_ledger = TrajectoryLedger(getattr(_get_store(), "db_path", None))
    record_trajectory_event_best_effort(
        task, "run_started", ledger=trajectory_ledger, status="pending"
    )
    record_trajectory_event_best_effort(
        task, "task_started", ledger=trajectory_ledger, status="pending"
    )
    print(f"Task registered: {args.task_id}")


def get_task(task_id, *, deps):
    load_tasks = deps["load_tasks"]
    find_task = deps["find_task"]
    data = load_tasks()
    task = find_task(data, task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)
    print(json.dumps(task, ensure_ascii=False, indent=2))


def list_tasks(workflow_id=None, *, deps):
    load_tasks = deps["load_tasks"]
    data = load_tasks()
    tasks = data["tasks"]
    if workflow_id:
        tasks = [task for task in tasks if task.get("workflow_id") == workflow_id]
    print(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2))


def check_zombie_obligations(data=None, *, deps):
    load_tasks = deps["load_tasks"]
    if data is None:
        data = load_tasks()
    tasks = data.get("tasks", []) if isinstance(data, dict) else data or []

    terminal_statuses = {"completed", "failed", "halted", "closed", "abandoned"}
    workflows_data = data.get("workflows", []) if isinstance(data, dict) else []
    workflow_status_map = {
        w.get("workflow_id"): w.get("status")
        for w in workflows_data
        if isinstance(w, dict) and w.get("workflow_id")
    }
    get_store = deps.get("get_store")
    if get_store:
        try:
            store = get_store()
            if store:
                for w in store.list_workflows():
                    if isinstance(w, dict) and w.get("workflow_id"):
                        workflow_status_map[w["workflow_id"]] = w.get("status")
        except Exception:
            pass

    superseded_targets = {
        t.get("supersedes")
        for t in tasks
        if isinstance(t, dict) and t.get("supersedes")
    }

    zombies = []
    for t in tasks:
        if isinstance(t, dict):
            task_id = t.get("task_id")
            if t.get("superseded_by") or (task_id and task_id in superseded_targets):
                continue
            if t.get("status") != "superseded" or not t.get("replacement_pending"):
                continue
            wf_id = t.get("workflow_id")
            if wf_id and workflow_status_map.get(wf_id) in terminal_statuses:
                continue
            zombies.append(t)
    return zombies
