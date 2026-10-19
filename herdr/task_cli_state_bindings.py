"""Bind task/workflow state access to the CLI's selected SQLite namespace."""

import json


def build_state_bindings(namespace):
    def load_workflow(workflow_id=None):
        from herdr.task_cli_tasks import load_workflow as load

        return load(
            workflow_id,
            deps={
                "get_state_store": namespace["get_state_store"],
                "workflow_config_for": namespace["workflow_config_for"],
                "normalize_workflow": namespace["normalize_workflow"],
                "workflow_file": namespace["WORKFLOW_FILE"],
            },
        )

    def resolve_node(node_id, workflow_id=None):
        from herdr.task_cli_tasks import resolve_node as resolve

        return resolve(
            node_id,
            workflow_id,
            deps={
                "ensure_node_runtime": namespace["ensure_node_runtime"],
                "load_workflow": namespace["load_workflow"],
                "find_node": namespace["find_node"],
            },
        )

    def ops_center(args):
        from herdr.ops_center import build_ops_center_payload

        payload = build_ops_center_payload(
            workflow_id=getattr(args, "workflow_id", None),
            include_tasks=getattr(args, "include_tasks", False),
            db_path=namespace["_state_db_path"](),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    def state_db_path():
        from herdr.task_cli_state import state_db_path as resolve

        return resolve(
            environ=namespace["os"].environ,
            tasks_file=namespace.get("TASKS_FILE"),
            workflow_file=namespace.get("WORKFLOW_FILE"),
            workflows_file=namespace.get("WORKFLOWS_FILE"),
            default_workflow_file=namespace["_DEFAULT_WORKFLOW_FILE"],
        )

    def get_store():
        from herdr.task_cli_state import get_store

        return get_store(
            state_db_path=namespace["_state_db_path"],
            get_state_store=namespace["get_state_store"],
        )

    def load_tasks():
        from herdr.task_cli_state import load_tasks as load

        return load(get_store=namespace["_get_store"])

    def save_tasks(data):
        from herdr.task_cli_state import save_tasks as save

        return save(
            data,
            get_store=namespace["_get_store"],
            sync_tasks_projection=namespace["sync_tasks_projection"],
            tasks_file=namespace.get("TASKS_FILE"),
        )

    def find_task(data, task_id):
        from herdr.task_cli_state import find_task as find

        return find(data, task_id)

    return {
        "load_workflow": load_workflow,
        "resolve_node": resolve_node,
        "ops_center": ops_center,
        "_state_db_path": state_db_path,
        "_get_store": get_store,
        "load_tasks": load_tasks,
        "save_tasks": save_tasks,
        "find_task": find_task,
    }
