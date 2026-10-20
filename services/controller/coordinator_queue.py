"""Route queued work and serialize execution per workflow.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

import threading

def _workflow_dispatch_lock(ports, workflow_id: str) -> threading.Lock:
    """Return the per-workflow serialization lock (created on first use)."""
    return ports._coordinator_runtime.workflow_lock(workflow_id)

def coordinator_worker(ports):
    """Main dispatcher: reads items from coordinator_queue and fans them out
    to per-workflow executor threads, guaranteeing at-most-one concurrent
    dispatch per workflow without blocking the queue for other workflows.
    """
    from herdr.coordinator_runtime import worker_loop

    return worker_loop(
        ports.coordinator_queue,
        get_task=ports.get_task,
        executor=ports._coordinator_executor,
        workflow_dispatch_lock=ports._workflow_dispatch_lock,
        process_item=ports._process_coordinator_item,
    )

def _process_coordinator_item(ports, item, wf_lock):
    """Process one coordinator queue item inside the executor thread pool,
    serialized per-workflow via wf_lock.
    """
    with wf_lock:
        ports._handle_coordinator_item(item)

def _handle_coordinator_item(ports, item):
    """Route coordinator work without changing event precedence."""
    if item.get("kind") == "workflow_continuation":
        ports.handle_workflow_continuation(item)
        return
    if item.get("kind") == "fix_loop":
        ports._handle_fix_loop_item(item)
        return
    if item.get("kind") == "stage_advance":
        ports._handle_stage_advance_item(item)
        return
    ports._handle_task_event(item)


DEPENDENCIES = {'_workflow_dispatch_lock': ('_coordinator_runtime',),
 'coordinator_worker': ('_coordinator_executor', '_process_coordinator_item',
                        '_workflow_dispatch_lock', 'coordinator_queue', 'get_task'),
 '_process_coordinator_item': ('_handle_coordinator_item',),
 '_handle_coordinator_item': ('_handle_fix_loop_item', '_handle_stage_advance_item',
                              '_handle_task_event', 'handle_workflow_continuation')}

__all__ = tuple(DEPENDENCIES)
