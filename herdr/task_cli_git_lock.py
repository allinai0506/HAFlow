"""Serialize task Git operations against clone and optional source repositories."""

import sys
from contextlib import ExitStack
from functools import wraps


def with_git_operation_lock(*, include_source_repo=False, deps):
    load_tasks = deps["load_tasks"]
    find_task = deps["find_task"]
    project_for_workflow = deps["project_for_workflow"]
    GitOperationLock = deps["GitOperationLock"]
    ensure_no_git_processes = deps["ensure_no_git_processes"]
    busy_errors = (deps["GitOperationBusyError"], deps["GitProcessActiveError"])

    def decorate(func):
        @wraps(func)
        def wrapped(task_id, *args, **kwargs):
            task = find_task(load_tasks(), task_id)
            paths = []
            if task and task.get("clone_path"):
                paths.append(task["clone_path"])
            if include_source_repo and task:
                project = project_for_workflow(task.get("workflow_id")) or {}
                source = task.get("source_repo") or project.get("project_root")
                if source:
                    paths.append(source)
            try:
                with ExitStack() as stack:
                    locked_paths = []
                    for path in dict.fromkeys(paths):
                        lock = GitOperationLock(path)
                        lock.acquire()
                        stack.callback(lock.release)
                        locked_paths.append(path)
                    for path in locked_paths:
                        ensure_no_git_processes(path)
                    return func(task_id, *args, **kwargs)
            except busy_errors as exc:
                print(f"[GIT BUSY] task={task_id}: {exc}")
                sys.exit(75)

        return wrapped

    return decorate
