"""Gate and redeliver task completion notifications through existing supervision.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def emit_done_if_allowed(ports, task, report_text=None):
    """The single gateway for agent_done -> coordinator done.

    Every Controller path that would announce a task as done goes through
    the supervisor checkpoint first; an enforced intervention blocks the
    default flow (the action handler owns the next step instead). When the
    checkpoint is rate-gated/skipped, a still-pending enforced intervention
    from the events ledger keeps blocking redelivery until a newer decision
    or a new agent_done transition supersedes it.

    The terminal Trajectory Observer checkpoint also lives here: listener,
    recovery and registry-redelivery paths all funnel through this gateway,
    and the observation must be queued before the task can advance.
    """
    observer_complete = ports.threading.Event()
    supervisor_complete = ports.threading.Event()
    store = ports._get_store()
    try:
        from herdr.trajectory import run_id_for_task
        if hasattr(store, "list_interventions") and ports._supervisor_action_recovery_enabled():
            recovered = ports.recover_pending_interventions(
                store=store, run_id=run_id_for_task(task), task_id=task.get("task_id"),
            )
            if recovered:
                return False
    except Exception as exc:
        print(f"[INTERVENTION RECOVERY UNAVAILABLE] task={task.get('task_id')}: {type(exc).__name__}")
        return False
    ports._observer_terminal_checkpoint(task, completion_event=observer_complete)
    ports._schedule_context_compact(
        task, wait_for=observer_complete, supervisor_done=supervisor_complete,
    )
    try:
        checkpoint = ports.supervisor_checkpoint(task, "agent_done", report_text=report_text)
        if checkpoint is None:
            pending = None
            if ports.supervisor_harness is not None:
                try:
                    pending = ports.supervisor_harness.pending_intervention(task, store)
                except Exception as exc:
                    if hasattr(store, "list_interventions"):
                        print(
                            f"[INTERVENTION RECOVERY UNAVAILABLE] "
                            f"task={task.get('task_id')}: {type(exc).__name__}"
                        )
                        return False
                    pending = None
            if pending:
                ports._supervisor_log_pending(task, pending)
                return False
            ports.enqueue_coordinator_event(task, "done")
            return True
        if not ports.supervisor_continue_flow(checkpoint):
            return False
        ports.enqueue_coordinator_event(task, "done")
        return True
    finally:
        supervisor_complete.set()

def redeliver_done_event(ports, task, now=None):
    """Registry-watcher done redelivery: supervisor-gated and throttled.

    This is a primary production path (sentinel-driven completions only reach
    the coordinator through here), so it must use the same gateway as
    handle_event; an enforced intervention keeps the done event withheld.
    """
    task_id = task.get("task_id")
    key = f"{task_id}:done"
    now = ports.time.time() if now is None else now
    with ports.lock:
        if key in ports.queued_events:
            return False
    if ports.attention_blocks_retry(key, now):
        return False
    print(
        f"[REGISTRY WATCHER] "
        f"task={task_id} "
        f"status=agent_done -> supervisor-gated done event"
    )
    if not ports.emit_done_if_allowed(task):
        return False
    if ports.attention_get(key):
        ports.attention_throttle(key, now=now)
    return True


DEPENDENCIES = {'emit_done_if_allowed': ('_get_store', '_observer_terminal_checkpoint',
                          '_schedule_context_compact', '_supervisor_action_recovery_enabled',
                          '_supervisor_log_pending', 'enqueue_coordinator_event',
                          'recover_pending_interventions', 'supervisor_checkpoint',
                          'supervisor_continue_flow', 'supervisor_harness', 'threading'),
 'redeliver_done_event': ('attention_blocks_retry', 'attention_get', 'attention_throttle',
                          'emit_done_if_allowed', 'lock', 'queued_events', 'time')}

__all__ = tuple(DEPENDENCIES)
