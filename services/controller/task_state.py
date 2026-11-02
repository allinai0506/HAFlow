"""Apply task status transitions and completion/blocker observations through the kernel.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def set_task_status(ports,
    task_id,
    status,
    *,
    expected_status=None,
    expected_version=None,
    source="herdr-controller",
    metadata=None,
):
    """Set a task status, optionally through the atomic StateStore gateway."""
    if expected_status is not None or expected_version is not None:
        from herdr import kernel

        try:
            result = kernel.transition_task(
                task_id=task_id,
                to_status=status,
                reason=f"{source}:{status}",
                source=source,
                metadata=metadata or {},
                expected_status=expected_status,
                expected_version=expected_version,
                store=ports._get_store(),
            )
        except ValueError as exc:
            # Shared transition-gateway refusals (e.g. the wrapup
            # review-closure invariant) fail this transition closed instead
            # of crashing the sweep: the task keeps its current status.
            print(f"[STATE TRANSITION REFUSED] {task_id}: {exc}")
            return False
        if not result.get("accepted", True):
            print(
                f"[STATE CAS REJECTED] {task_id}: "
                f"{result.get('reason', 'cas_mismatch')}"
            )
            return False
        return True

    result = ports.subprocess.run(
        [
            ports.TASK_MANAGER,
            "set",
            task_id,
            status
        ],
        text=True,
        capture_output=True
    )

    if result.returncode != 0:
        print(
            f"[STATE ERROR] {task_id}: "
            f"{result.stdout.strip() or result.stderr.strip()}"
        )
        return False

    print(
        f"[STATE] "
        f"{result.stdout.strip()}"
    )

    return True

def _task_version(ports, task):
    try:
        version = int(task.get("version") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    return version if version > 0 else None

def _set_observed_status(ports, task, status, reason):
    """Transition from a Controller snapshot without a get/rewrite TOCTOU."""
    task = task or {}
    if ports.os.environ.get("HERDR_CONTROLLER_TEST") or ports._task_version(task) is None:
        return ports.set_task_status(task.get("task_id"), status)
    metadata = {"controller_reason": reason}
    if status == "failed":
        metadata["failure_reason"] = reason
    return ports.set_task_status(
        task.get("task_id"),
        status,
        expected_status=task.get("status"),
        expected_version=ports._task_version(task),
        source="herdr-controller",
        metadata=metadata,
    )

def _completion_elapsed(ports, task, now):
    try:
        started = float(
            (task or {}).get("started_at")
            or (task or {}).get("created_at")
            or now
        )
    except (TypeError, ValueError):
        started = float(now)
    return float(now) - started

def process_completion_observation(ports, task, now=None):
    """Controller-only completion arbitration for a Sentinel observation.

    Sentinel only records pane samples.  This function is the sole owner of
    the resulting ``working/dispatched -> agent_done`` transition and always
    supplies the observed status/version to the StateStore CAS.
    """
    task = task or {}
    if task.get("completion_protocol") == "receipt-v1" or not task.get("task_id"):
        return False
    from herdr.completion_controller import process_observation

    return process_observation(
        task,
        store=ports._get_store(),
        enqueue_coordinator_event=ports.enqueue_coordinator_event,
        task_version=ports._task_version,
        completion_elapsed=ports._completion_elapsed,
        now=now,
    )

def process_structured_completions(ports, now=None):
    """Advance a bounded scheduling cursor even when execution gates reject."""
    from herdr.completion_receipt import consume_completion_receipt, pending_completion_task_ids
    store = ports._get_store()
    key = str(ports.Path(store.db_path).resolve())
    if key not in ports._COMPLETION_SCHEDULING_CURSORS and len(ports._COMPLETION_SCHEDULING_CURSORS) >= 64:
        ports._COMPLETION_SCHEDULING_CURSORS.pop(next(iter(ports._COMPLETION_SCHEDULING_CURSORS)))
    cursor = ports._COMPLETION_SCHEDULING_CURSORS.get(key, 0)
    rows = pending_completion_task_ids(store, now=now, after_rowid=cursor, with_rows=True)
    if not rows and cursor:
        rows = pending_completion_task_ids(store, now=now, after_rowid=0, with_rows=True)
        ports._COMPLETION_SCHEDULING_CURSORS[key] = 0
    accepted = 0
    for task_id, rowid in rows:
        ports._COMPLETION_SCHEDULING_CURSORS[key] = rowid
        accepted += bool(consume_completion_receipt(task_id, store, now=now).get('accepted'))
    return accepted

def process_all_completion_observations(ports, now=None):
    """Process durable Sentinel samples for all active tasks."""
    processed = 0
    for task in ports.load_tasks():
        if task.get("status") not in {"dispatched", "working"}:
            continue
        if ports.process_completion_observation(task, now=now):
            processed += 1
    return processed

def process_blocked_observations(ports):
    """Apply one Controller-owned CAS for each fresh Sentinel blocker sample.

    A sample recorded before a ``set-status`` / human reopen / other Controller
    write is stale by construction: its ``observed_version`` can never match
    again.  Re-attempting that CAS on every sweep burns the transition budget
    and grows the events ledger without bound (observed: 238 identical
    rejections over 25 minutes on one task) while the task makes no progress.
    A stale sample is therefore skipped silently until Sentinel records a
    fresh one; rejections that are *not* explained by staleness are still
    recorded, but deduplicated per (task, expected_version).
    """
    from herdr import kernel
    from herdr.completion import observation_is_current
    from herdr.completion_controller import process_blocker_observations

    return process_blocker_observations(
        store=ports._get_store(),
        tasks=ports.load_tasks(),
        stale_state=ports._blocked_observation_stale,
        rejected_state=ports._blocked_observation_rejected,
        task_version=ports._task_version,
        enqueue_coordinator_event=ports.enqueue_coordinator_event,
        transition_task=kernel.transition_task,
        observation_is_current=observation_is_current,
    )

def _completion_marker_snapshot(ports, task):
    """Read the task-owned completion marker without treating idle as done."""
    task = task or {}
    pane_id = task.get("pane_id")
    task_id = task.get("task_id")
    if not pane_id or not task_id:
        return False, ""
    try:
        result = ports.subprocess.run(
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, ports.subprocess.SubprocessError):
        return False, ""
    screen = (result.stdout or "") + (result.stderr or "")
    from herdr.completion import marker_present as _marker_present

    return _marker_present(screen, task_id), screen

def _record_completion_sample(ports,
    task,
    marker_present,
    now=None,
    agent_status="idle",
):
    """Persist one Controller/Sentinel sample and arbitrate it atomically."""
    task = task or {}
    task_id = task.get("task_id")
    if not task_id:
        return False
    now = ports.time.time() if now is None else float(now)
    try:
        ports._get_store().observe_completion(
            task_id,
            marker_present=bool(marker_present),
            agent_status=agent_status,
            observed_at=now,
        )
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False
    return ports.process_completion_observation(task, now=now)


DEPENDENCIES = {'set_task_status': ('TASK_MANAGER', '_get_store', 'subprocess'),
 '_task_version': (),
 '_set_observed_status': ('_task_version', 'os', 'set_task_status'),
 '_completion_elapsed': (),
 'process_completion_observation': ('_completion_elapsed', '_get_store', '_task_version',
                                    'enqueue_coordinator_event'),
 'process_structured_completions': ('Path', '_COMPLETION_SCHEDULING_CURSORS', '_get_store'),
 'process_all_completion_observations': ('load_tasks', 'process_completion_observation'),
 'process_blocked_observations': ('_blocked_observation_rejected', '_blocked_observation_stale',
                                  '_get_store', '_task_version', 'enqueue_coordinator_event',
                                  'load_tasks'),
 '_completion_marker_snapshot': ('subprocess',),
 '_record_completion_sample': ('_get_store', 'process_completion_observation', 'time')}

__all__ = tuple(DEPENDENCIES)
