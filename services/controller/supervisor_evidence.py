"""Check intervention execution evidence against the owning task and run.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _verification_snapshot(ports, clone_path):
    """Return the version of EVAL_DONE visible at a dispatch boundary."""
    if not clone_path:
        return None
    path = ports.Path(clone_path) / ".herdr-loop" / "EVAL_DONE.json"
    try:
        raw = path.read_bytes()
        snapshot = ports.json.loads(raw.decode("utf-8"))
        return {
            "sha256": ports.hashlib.sha256(raw).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
            "iteration": snapshot.get("iteration"),
            "completed_at": snapshot.get("completed_at"),
        }
    except Exception:
        return None

def _verification_evidence_is_new(ports, dispatch, test_evidence):
    """Reject the exact EVAL_DONE snapshot that predates VERIFY dispatch."""
    payload = dispatch.get("payload") or {}
    baseline = payload.get("evidence_baseline")
    started_at = float(payload.get("dispatch_started_at") or dispatch.get("timestamp") or 0)
    current_sha = test_evidence.get("snapshot_sha256")
    if not current_sha:
        return False
    if baseline and current_sha == baseline.get("sha256"):
        return False
    completed_at = test_evidence.get("snapshot_completed_at")
    if started_at:
        if completed_at is None:
            return False
        try:
            if float(completed_at) <= started_at:
                return False
        except (TypeError, ValueError):
            return False
    return True

def _intervention_execution_evidence(ports, task, intervention, store):
    """Find durable evidence for this exact Intervention, never by status alone."""
    intervention_id = intervention.get("intervention_id")
    action = intervention.get("action")
    task_id = intervention.get("task_id")
    run_id = intervention.get("run_id")
    fresh = store.get_task(task_id) if store is not None else None
    fresh = fresh or task
    if store is None:
        return None
    if action == "RETRY":
        for event in store.list_events(
            task_id=task_id, event_type="retry_dispatched", limit=200, desc=True,
        ):
            payload = event.get("payload") or {}
            if (
                event.get("run_id") == run_id
                and payload.get("intervention_id") == intervention_id
                and payload.get("action") == action
            ):
                return {
                    "action": action,
                    "previous_status": fresh.get("status"),
                    "new_status": fresh.get("status"),
                    "attempt_count": payload.get("attempt_count"),
                    "retry_dispatched": True,
                    "already_applied": True,
                    "execution_evidence": True,
                }
        return None
    for event in store.list_events(task_id=task_id, event_type="verification_dispatched", limit=200, desc=True):
        payload = event.get("payload") or {}
        if (
            event.get("run_id") == run_id
            and payload.get("intervention_id") == intervention_id
            and payload.get("action") == action
        ):
            return {
                "action": action,
                "previous_status": fresh.get("status"),
                "new_status": fresh.get("status"),
                "verification_dispatched": True,
                "verification_requested": True,
                "verification_pending": True,
                "already_applied": True,
                "execution_evidence": True,
            }
    return None

def _latest_verification_dispatch(ports, task, store=None):
    from herdr.completion import latest_verification_dispatch
    return latest_verification_dispatch(task, store or ports._get_store())

def _latest_action_dispatch_intent(ports, task, store, action, intervention_id=None):
    store = store or ports._get_store()
    from herdr.trajectory import run_id_for_task
    event_type = {
        "VERIFY": "verification_dispatch_intent",
        "RETRY": "retry_dispatch_intent",
    }.get(action, f"{action.lower()}_dispatch_intent")
    rows = store.list_events(
        task_id=task.get("task_id"),
        event_type=event_type,
        source="supervisor", limit=100, desc=True,
    )
    expected_run = run_id_for_task(task)
    for event in rows:
        payload = event.get("payload") or {}
        if (
            event.get("run_id") == expected_run
            and payload.get("action") == action
            and (intervention_id is None or payload.get("intervention_id") == intervention_id)
        ):
            return event
    return None

def _latest_verification_dispatch_intent(ports, task, store=None, intervention_id=None):
    return ports._latest_action_dispatch_intent(task, store, "VERIFY", intervention_id)

def _latest_retry_dispatch(ports, task, store=None):
    from herdr.completion import latest_retry_dispatch
    return latest_retry_dispatch(task, store or ports._get_store())

def _retry_execution_complete_for_rework(ports, task, store=None):
    from herdr.completion import retry_execution_complete_for_rework
    return retry_execution_complete_for_rework(task, store or ports._get_store())

def _verification_execution_complete_for_rework(ports, task, store=None):
    from herdr.completion import verification_execution_complete_for_rework
    return verification_execution_complete_for_rework(task, store or ports._get_store())


DEPENDENCIES = {'_verification_snapshot': ('Path', 'hashlib', 'json'),
 '_verification_evidence_is_new': (),
 '_intervention_execution_evidence': (),
 '_latest_verification_dispatch': ('_get_store',),
 '_latest_action_dispatch_intent': ('_get_store',),
 '_latest_verification_dispatch_intent': ('_latest_action_dispatch_intent',),
 '_latest_retry_dispatch': ('_get_store',),
 '_retry_execution_complete_for_rework': ('_get_store',),
 '_verification_execution_complete_for_rework': ('_get_store',)}

__all__ = tuple(DEPENDENCIES)
