"""Workflow administration CLI operations."""

import json
import os
import sys
from pathlib import Path


def node_status(workflow_id, node_key, *, resolve_node, load_tasks,
                safe_workflow, normalize_workflow_nodes,
                node_delivery_complete):
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
            "tasks": []
        }

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2
            )
        )
        return

    # Superseded tasks do not block node completion — they were replaced by
    # another task whose outcome is the authoritative result for this node.
    active_tasks = [
        task for task in tasks
        if task.get("status") != "superseded" and not task.get("superseded_by")
    ]

    workflow, _ = safe_workflow(workflow_id)
    cfg_node = next((n for n in normalize_workflow_nodes(workflow or {}) if n.get("id") == node_key), {})
    complete = node_delivery_complete(tasks, cfg_node.get("required_task_ids"))

    result = {
        "workflow_id": workflow_id,
        "node_id": node_key,
        "node": node_key,
        "stage": node_key,
        "node_label": node["node_label"],
        "stage_label": node["stage_label"],
        "status": (
            "completed"
            if complete
            else "in_progress"
        ),
        "complete": complete,
        "depends_on": node.get("depends_on", []),
        "next_stage": node.get("next"),
        "tasks": [
            {
                "task_id": task["task_id"],
                "status": task["status"]
            }
            for task in tasks
        ]
    }

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2
        )
    )


def supersede_task(task_id, new_task_id=None, reason=None, allow_new_run=False,
                   abandon=False, allow_pending=False, *, load_tasks, find_task,
                   get_store, transitions, sync_tasks_projection,
                   autosave_clone_wip, tasks_file=None):
    """Mark a task as superseded (replaced by another task or abandoned).

    Replacement obligations remain until the linked task delivers. Only an
    explicit abandon removes the obligation without a replacement.
    """
    if abandon and (new_task_id or not str(reason or "").strip()):
        print("[SUPERSEDE REJECTED] --abandon requires a reason and cannot be combined with --by")
        sys.exit(2)
    if not new_task_id and not abandon and not allow_pending:
        print(
            "[SUPERSEDE REJECTED] supersede requires either --by <replacement_task_id> "
            "or --abandon --reason <reason> to prevent unlinked zombie obligations"
        )
        sys.exit(2)
    data = load_tasks()
    task = find_task(data, task_id)

    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    old_status = task["status"]

    # 补挂：任务已经是 superseded（auto-recover 先作废、direct_dispatch 之后才
    # 按谱系补派 -rN），此刻补上替代者指针。这不是状态转换 —— superseded 必须
    # 保持终态 —— 所以只回填 superseded_by，不动 status、run 与首次作废原因。
    #
    # 不补的后果不是「少个字段」：required_task_ids 的链式解析
    # （scheduler.node_is_complete）断在第一跳，实现节点永远判不出完成，
    # Controller 只会刷 [STAGE ADVANCE WAIT] coordinator=working。
    relinking = old_status == "superseded"
    if relinking and not new_task_id and not abandon:
        print(
            f"Illegal transition: "
            f"{task_id}: {old_status} -> superseded"
        )
        sys.exit(2)
    if relinking:
        existing = str(task.get("superseded_by") or "").strip()
        if existing:
            if existing == new_task_id:
                print(f"[SUPERSEDE NOOP] {task_id} already superseded by {existing}")
                return
            print(
                f"[SUPERSEDE REJECTED] {task_id} already superseded by "
                f"{existing}; refusing to repoint to {new_task_id}"
            )
            sys.exit(2)
    elif "superseded" not in transitions.get(old_status, set()):
        print(
            f"Illegal transition: "
            f"{task_id}: {old_status} -> superseded"
        )
        sys.exit(2)

    if abandon and task.get("superseded_by"):
        print("[SUPERSEDE REJECTED] linked replacement cannot be abandoned by changing its predecessor")
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
                    f"[SUPERSEDE REJECTED] replacement {new_task_id} "
                    "belongs to another workflow"
                )
                sys.exit(2)
            if (task.get("node") or task.get("stage")) != (replacement.get("node") or replacement.get("stage")):
                print("[SUPERSEDE REJECTED] replacement belongs to another node")
                sys.exit(2)
            # 补派按设计就是新执行：controller 保留 --execution-id 并注明
            # "sibling launches keep distinct run_ids"，所以补挂必须允许
            # run 不同，否则存量断链永远补不上。首次作废仍要求同 run。
            if (
                not relinking and not allow_new_run
                and task.get("run_id")
                and replacement.get("run_id") != task.get("run_id")
            ):
                print(
                    f"[SUPERSEDE REJECTED] replacement {new_task_id} "
                    "belongs to another run"
                )
                sys.exit(2)

    if reason and not relinking:
        meta["supersede_reason"] = reason

    store = get_store()
    try:
        from herdr import kernel
        # 补挂是幂等自转移：核心状态机本就允许 old == new
        # （herdr.transitions.validate_task_transition），因此这里只借道
        # metadata 回填指针，status 保持终态不变。
        result = kernel.transition_task(
            task_id=task_id,
            to_status="superseded",
            reason=(task.get("supersede_reason") or reason) if relinking
            else (reason or "superseded"),
            source="herdr-task",
            metadata=meta,
            store=store,
            expected_status=old_status,
            expected_version=task.get("version"),
        )
        if not result.get("accepted", True):
            print(
                f"[SUPERSEDE CAS REJECTED] {task_id} changed during "
                f"{'link' if relinking else 'supersede'}",
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
            f"{task_id}: linked superseded_by={new_task_id} "
            f"(status unchanged: {old_status})"
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
