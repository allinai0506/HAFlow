"""Workflow administration CLI operations."""

import subprocess
import json
import os
import sys
from pathlib import Path


def node_status(
    workflow_id,
    node_key,
    *,
    resolve_node,
    load_tasks,
    safe_workflow,
    normalize_workflow_nodes,
    node_delivery_complete,
):
    node = resolve_node(node_key, workflow_id)
    data = load_tasks()
    tasks = [
        task
        for task in data["tasks"]
        if task.get("workflow_id") == workflow_id
        and (task.get("node") == node_key or task.get("stage") == node_key)
    ]
    if not tasks:
        result = {
            "workflow_id": workflow_id,
            "node_id": node_key,
            "node": node_key,
            "stage": node_key,
            "node_label": node["node_label"],
            "stage_label": node["stage_label"],
            "status": "empty",
            "complete": False,
            "depends_on": node.get("depends_on", []),
            "next_stage": node.get("next"),
            "tasks": [],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    active_tasks = [
        task
        for task in tasks
        if task.get("status") != "superseded" and (not task.get("superseded_by"))
    ]
    workflow, _ = safe_workflow(workflow_id)
    cfg_node = next(
        (
            n
            for n in normalize_workflow_nodes(workflow or {})
            if n.get("id") == node_key
        ),
        {},
    )
    complete = node_delivery_complete(tasks, cfg_node.get("required_task_ids"))
    result = {
        "workflow_id": workflow_id,
        "node_id": node_key,
        "node": node_key,
        "stage": node_key,
        "node_label": node["node_label"],
        "stage_label": node["stage_label"],
        "status": "completed" if complete else "in_progress",
        "complete": complete,
        "depends_on": node.get("depends_on", []),
        "next_stage": node.get("next"),
        "tasks": [
            {"task_id": task["task_id"], "status": task["status"]} for task in tasks
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def supersede_task(
    task_id,
    new_task_id=None,
    reason=None,
    allow_new_run=False,
    abandon=False,
    allow_pending=False,
    *,
    load_tasks,
    find_task,
    get_store,
    transitions,
    sync_tasks_projection,
    autosave_clone_wip,
    tasks_file=None,
):
    """Mark a task as superseded (replaced by another task or abandoned).

    Replacement obligations remain until the linked task delivers. Only an
    explicit abandon removes the obligation without a replacement.
    """
    if abandon and (new_task_id or not str(reason or "").strip()):
        print(
            "[SUPERSEDE REJECTED] --abandon requires a reason and cannot be combined with --by"
        )
        sys.exit(2)
    if not new_task_id and (not abandon) and (not allow_pending):
        print(
            "[SUPERSEDE REJECTED] supersede requires either --by <replacement_task_id> or --abandon --reason <reason> to prevent unlinked zombie obligations"
        )
        sys.exit(2)
    data = load_tasks()
    task = find_task(data, task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)
    old_status = task["status"]
    relinking = old_status == "superseded"
    if relinking and (not new_task_id) and (not abandon):
        print(f"Illegal transition: {task_id}: {old_status} -> superseded")
        sys.exit(2)
    if relinking:
        existing = str(task.get("superseded_by") or "").strip()
        if existing:
            if existing == new_task_id:
                print(f"[SUPERSEDE NOOP] {task_id} already superseded by {existing}")
                return
            print(
                f"[SUPERSEDE REJECTED] {task_id} already superseded by {existing}; refusing to repoint to {new_task_id}"
            )
            sys.exit(2)
    elif "superseded" not in transitions.get(old_status, set()):
        print(f"Illegal transition: {task_id}: {old_status} -> superseded")
        sys.exit(2)
    if abandon and task.get("superseded_by"):
        print(
            "[SUPERSEDE REJECTED] linked replacement cannot be abandoned by changing its predecessor"
        )
        sys.exit(2)
    meta = {"replacement_pending": not abandon}
    if abandon:
        meta["abandon_reason"] = reason
    if new_task_id:
        meta["superseded_by"] = new_task_id
        replacement = find_task(data, new_task_id)
        if replacement:
            if replacement.get("workflow_id") != task.get("workflow_id"):
                print(
                    f"[SUPERSEDE REJECTED] replacement {new_task_id} belongs to another workflow"
                )
                sys.exit(2)
            if (task.get("node") or task.get("stage")) != (
                replacement.get("node") or replacement.get("stage")
            ):
                print("[SUPERSEDE REJECTED] replacement belongs to another node")
                sys.exit(2)
            if (
                not relinking
                and (not allow_new_run)
                and task.get("run_id")
                and (replacement.get("run_id") != task.get("run_id"))
            ):
                print(
                    f"[SUPERSEDE REJECTED] replacement {new_task_id} belongs to another run"
                )
                sys.exit(2)
    if reason and (not relinking):
        meta["supersede_reason"] = reason
    store = get_store()
    try:
        from herdr import kernel

        result = kernel.transition_task(
            task_id=task_id,
            to_status="superseded",
            reason=(
                task.get("supersede_reason") or reason
                if relinking
                else reason or "superseded"
            ),
            source="herdr-task",
            metadata=meta,
            store=store,
            expected_status=old_status,
            expected_version=task.get("version"),
        )
        if not result.get("accepted", True):
            print(
                f"[SUPERSEDE CAS REJECTED] {task_id} changed during {('link' if relinking else 'supersede')}",
                file=sys.stderr,
            )
            sys.exit(2)
    except (OSError, RuntimeError, ValueError, AttributeError) as exc:
        print(f"Error superseding task {task_id}: {exc}", file=sys.stderr)
        sys.exit(2)
    t_file = tasks_file or os.environ.get("TASKS_FILE")
    sync_tasks_projection(store=store, tasks_file=Path(t_file) if t_file else None)
    if relinking:
        print(
            f"{task_id}: linked superseded_by={new_task_id} (status unchanged: {old_status})"
        )
        return
    try:
        autosave_clone_wip(task)
    except Exception as exc:
        print(f"[WIP AUTOSAVE WARN] {task_id}: {exc}")
    msg = f"{task_id}: {old_status} -> superseded"
    if new_task_id:
        msg += f" (by {new_task_id})"
    print(msg)


def stage_reset(
    workflow_id, stage=None, *, stage_state_file, path_exists=os.path.exists
):
    """Clear queued/notified stage advance locks for one workflow or stage."""
    if not path_exists(stage_state_file):
        print("stage-state.json not found — nothing to reset.")
        return
    with open(stage_state_file, "r", encoding="utf-8") as handle:
        state = json.load(handle)
    prefix = f"{workflow_id}:"
    removed = []
    for key in list(state.keys()):
        if key.startswith(prefix) and (stage is None or key == f"{prefix}{stage}"):
            del state[key]
            removed.append(key)
    temporary = stage_state_file + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, stage_state_file)
    if removed:
        for key in removed:
            print(f"[RESET] cleared: {key}")
    else:
        target = f"{prefix}{stage}" if stage else f"{prefix}*"
        print(f"[RESET] no entries found for: {target}")


def force_advance(workflow_id, stage=None, *, reset_stage):
    """Clear stage locks so the Controller can re-evaluate workflow progress."""
    reset_stage(workflow_id, stage)
    print(
        f"[ADVANCE] stage-state cleared for workflow={workflow_id}. Controller will re-evaluate within ~2 seconds."
    )


def reopen_workflow(workflow_id, *, get_store, reopen_owned):
    from herdr.workflow_close import workflow_lifecycle_lock

    with workflow_lifecycle_lock(get_store(), workflow_id) as acquired:
        if not acquired:
            print(
                f"[REOPEN BUSY] workflow={workflow_id}: lifecycle operation in progress"
            )
            sys.exit(75)
        return reopen_owned(workflow_id)


def reopen_owned_workflow(
    workflow_id,
    *,
    load_workflow_entry,
    herdr,
    get_store,
    now,
    sync_workflows_projection,
    workflows_file,
    reset_stage,
):
    entry, entries = load_workflow_entry(workflow_id)
    if entry is None:
        print(f"Unknown workflow: {workflow_id}")
        sys.exit(1)
    if entry.get("status") != "completed":
        print(
            f"[REOPEN ABORT] workflow={workflow_id} status={entry.get('status')}: only completed workflows can reopen"
        )
        sys.exit(2)
    coordinator_pane = entry.get("coordinator_pane_id")
    if not coordinator_pane:
        print(
            f"[REOPEN ABORT] workflow={workflow_id}: record has no coordinator_pane_id"
        )
        sys.exit(2)
    listing = herdr("pane", "list")
    alive = False
    if listing.returncode == 0:
        try:
            result = json.loads(listing.stdout).get("result", {})
            panes = result.get("panes", result) if isinstance(result, dict) else result
            alive = any(
                (pane.get("pane_id") == coordinator_pane for pane in panes or [])
            )
        except (json.JSONDecodeError, TypeError):
            alive = False
    if not alive:
        print(
            f"[REOPEN ABORT] coordinator pane {coordinator_pane} is not alive; rebuild the coordinator pane first, then reopen"
        )
        sys.exit(2)
    store = get_store()
    try:
        from herdr import kernel

        kernel.transition_workflow(
            workflow_id=workflow_id,
            to_status="in_progress",
            reason="workflow_reopened",
            source="herdr-task",
            metadata={"suppress_auto_close": True, "reopened_at": now()},
            store=store,
        )
    except Exception as exc:
        print(f"Error reopening workflow {workflow_id}: {exc}", file=sys.stderr)
        sys.exit(2)
    workflow_path = workflows_file or os.environ.get("WORKFLOWS_FILE")
    sync_workflows_projection(
        store=store, wf_file=Path(workflow_path) if workflow_path else None
    )
    reset_stage(workflow_id)
    print(
        f"[WORKFLOW REOPENED] {workflow_id} (suppress_auto_close latch set; clears on first active task)"
    )


def clear_finalize_escalation(task_id, *, deps):
    _get_store = deps["get_store"]
    find_task = deps["find_task"]
    load_tasks = deps["load_tasks"]
    ATTENTION_FILE = deps["attention_file"]
    "H-1: revoke machine-set finalize_escalated so finalize can re-drive.\n\n    Human recovery path after the cause (dirty main repo, flaky gate) is\n    fixed. Clears the flag and reason **plus** the shared\n    ``<task_id>:finalize`` attention episode, so the next controller sweep\n    recomputes retry budget from scratch instead of seeing the stale\n    ``attempts >= FINALIZE_RETRY_MAX`` snapshot and immediately\n    re-escalating. (P1: flag-only clearing could never actually resume\n    a retry_exhausted task.) The controller additionally drops its\n    in-memory exhausted latch once the episode is gone.\n    "
    data = load_tasks()
    task = find_task(data, task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)
    was_escalated = bool(task.get("finalize_escalated"))
    from herdr import kernel
    from herdr import liveness

    try:
        attention_cleared = liveness.EpisodeStore(ATTENTION_FILE).clear(
            f"{task_id}:finalize"
        )
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        print(f"Error clearing attention for {task_id}: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        kernel.update_task_metadata(
            task_id,
            {"finalize_escalated": False, "finalize_escalate_reason": None},
            store=_get_store(),
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        AttributeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Error clearing escalation for {task_id}: {exc}", file=sys.stderr)
        sys.exit(2)
    print(
        f"[ESCALATION CLEARED] {task_id} attention_cleared={bool(attention_cleared)} was_escalated={was_escalated}"
    )


def pause_task(task_id, reason=None, *, deps):
    _get_store = deps["get_store"]
    set_status = deps["set_status"]
    "Pause an in-flight task (dispatched/working/rework)."
    if reason:
        from herdr import kernel

        try:
            kernel.update_task_metadata(
                task_id, {"pause_reason": reason}, store=_get_store()
            )
        except ValueError:
            print(f"Task not found: {task_id}")
            sys.exit(1)
    set_status(task_id, "paused")


def resume_task(task_id, *, deps):
    find_task = deps["find_task"]
    load_tasks = deps["load_tasks"]
    set_status = deps["set_status"]
    "Resume a paused task back to working or rework based on history."
    data = load_tasks()
    task = find_task(data, task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)
    if task.get("status") != "paused":
        print(f"Task {task_id} is not paused (currently: {task.get('status')})")
        sys.exit(2)
    target = "working"
    history = task.get("status_history", [])
    # Restore only the latest pause origin; older pauses must not leak through.
    latest_pause_from = None
    for h in reversed(history):
        if h.get("to") == "paused":
            latest_pause_from = h.get("from")
            break
    if latest_pause_from in ("working", "rework"):
        target = latest_pause_from
    set_status(task_id, target)


def _clear_suppress_auto_close(workflow_id, *, deps):
    _get_store = deps["get_store"]
    'Fix-loop reopen 闩:该 workflow 任一任务进入 ACTIVE 时清除。\n\n    闩存在的意义是封住 reopen 后"旧任务仍全为完成系、周期 sweep\n    立即 auto-close"的自消除窗口;首个 fix task 落盘即为摘除时机,\n    此时 implementation 节点已回归未完成。\n    '
    if not workflow_id:
        return
    store = _get_store()
    entry = store.get_workflow(workflow_id)
    if entry is None or not entry.get("suppress_auto_close"):
        return
    from herdr import kernel

    kernel.update_workflow_metadata(
        workflow_id, {"suppress_auto_close": None}, store=store
    )
    print(f"[AUTO-CLOSE LATCH CLEARED] workflow={workflow_id}")
