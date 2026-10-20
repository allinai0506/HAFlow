import threading

import pytest

from herdr.coordinator_runtime import CoordinatorDispatchRuntime, worker_loop


def test_runtime_reuses_locks_per_workflow_and_separates_workflows():
    runtime = CoordinatorDispatchRuntime(max_workers=1)
    try:
        first = runtime.workflow_lock("workflow-a")
        assert runtime.workflow_lock("workflow-a") is first
        assert runtime.workflow_lock("workflow-b") is not first
        assert first.acquire(blocking=False)
        first.release()
    finally:
        runtime.executor.shutdown(wait=True)


class _StopWorker(Exception):
    pass


class _OneItemQueue:
    def __init__(self, items):
        self.items = list(items)
        self.done = 0

    def get(self):
        if not self.items:
            raise _StopWorker
        return self.items.pop(0)

    def task_done(self):
        self.done += 1


class _InlineExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))
        return fn(*args)


def test_worker_routes_items_with_the_same_per_workflow_lock():
    work_queue = _OneItemQueue([
        {"task_id": "task-1"},
        {"task_id": "task-2"},
        {"task_id": "task-3"},
    ])
    executor = _InlineExecutor()
    runtime = CoordinatorDispatchRuntime(max_workers=1)
    routed = []

    def process(item, lock):
        routed.append((item, lock))

    try:
        with pytest.raises(_StopWorker):
            worker_loop(
                work_queue,
                get_task=lambda task_id: {
                    "task_id": task_id,
                    "workflow_id": "workflow-b" if task_id == "task-3" else "workflow-a",
                },
                executor=executor,
                workflow_dispatch_lock=runtime.workflow_lock,
                process_item=process,
            )

        assert work_queue.done == 3
        assert len(executor.submitted) == 3
        assert [item[0]["task_id"] for item in routed] == ["task-1", "task-2", "task-3"]
        assert routed[0][1] is routed[1][1]
        assert routed[2][1] is runtime.workflow_lock("workflow-b")
        assert routed[2][1] is not routed[0][1]
    finally:
        runtime.executor.shutdown(wait=True)
