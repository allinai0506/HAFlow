"""Workflow administration CLI operations."""

import json


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
