"""Concurrency resources and queue fan-out for coordinator work items."""

import threading
from concurrent.futures import ThreadPoolExecutor


class CoordinatorDispatchRuntime:
    """Own the bounded executor and per-workflow serialization locks."""

    def __init__(self, max_workers=16):
        self.workflow_locks = {}
        self.lock_creation_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="coord-worker",
        )

    def workflow_lock(self, workflow_id):
        """Return the stable process-local dispatch lock for one workflow."""
        with self.lock_creation_lock:
            if workflow_id not in self.workflow_locks:
                self.workflow_locks[workflow_id] = threading.Lock()
            return self.workflow_locks[workflow_id]


def dispatch_item(
    item,
    *,
    get_task,
    executor,
    workflow_dispatch_lock,
    process_item,
):
    """Resolve the workflow and submit the item under its serialization lock."""
    workflow_id = item.get("workflow_id")
    if not workflow_id and "task_id" in item:
        task = get_task(item["task_id"])
        workflow_id = (task or {}).get("workflow_id", "unknown")
    wf_lock = workflow_dispatch_lock(workflow_id or "unknown")
    executor.submit(process_item, item, wf_lock)


def worker_loop(
    coordinator_queue,
    *,
    get_task,
    executor,
    workflow_dispatch_lock,
    process_item,
):
    """Consume queue items, always marking each dequeued item complete."""
    while True:
        item = coordinator_queue.get()
        try:
            dispatch_item(
                item,
                get_task=get_task,
                executor=executor,
                workflow_dispatch_lock=workflow_dispatch_lock,
                process_item=process_item,
            )
        finally:
            coordinator_queue.task_done()
