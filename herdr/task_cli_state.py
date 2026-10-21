"""State-store selection and task projection helpers for the CLI."""

import os
from pathlib import Path


def state_db_path(
    *, environ, tasks_file, workflow_file, workflows_file, default_workflow_file
):
    if environ.get("HERDR_STATE_DB"):
        return Path(environ["HERDR_STATE_DB"])
    task_path = tasks_file or environ.get("TASKS_FILE")
    if task_path:
        path = Path(task_path)
        db_path = (
            path.parent / "state.db"
            if path.name == "tasks.json"
            else path.with_suffix(".db")
        )
        if db_path.parent.exists():
            return db_path
    legacy_workflow_file = workflow_file
    if legacy_workflow_file == default_workflow_file and not environ.get(
        "WORKFLOW_FILE"
    ):
        legacy_workflow_file = None
    selected_workflow_file = (
        legacy_workflow_file
        or workflows_file
        or environ.get("WORKFLOW_FILE")
        or environ.get("WORKFLOWS_FILE")
    )
    if selected_workflow_file:
        path = Path(selected_workflow_file)
        db_path = (
            path.parent / "state.db"
            if path.name in ("workflows.json", "workflow.json")
            else path.with_suffix(".db")
        )
        if db_path.parent.exists():
            return db_path
    return None


def get_store(*, state_db_path, get_state_store):
    db_path = state_db_path()
    return (
        get_state_store(db_path=db_path) if db_path is not None else get_state_store()
    )


def load_tasks(*, get_store):
    return {"tasks": get_store().list_tasks()}


def save_tasks(data, *, get_store, sync_tasks_projection, tasks_file, fail_if_exists=False):
    store = get_store()
    for task in data.get("tasks", []):
        if task.get("task_id"):
            store.save_task(task, fail_if_exists=fail_if_exists)
    task_path = tasks_file or os.environ.get("TASKS_FILE")
    sync_tasks_projection(
        store=store, tasks_file=Path(task_path) if task_path else None
    )


def find_task(data, task_id):
    for task in data["tasks"]:
        if task["task_id"] == task_id:
            return task
    return None
