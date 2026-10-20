"""Controller arbitration for durable Sentinel completion observations.

This module owns the acceptance side effects around the StateStore CAS. The
process-level Controller supplies its current store, coordinator publisher, and
task-version/time adapters so existing ownership and compatibility stay local.
"""

import time


_QUIET_REJECTIONS = {
    "completion_observation_missing",
    "completion_marker_absent",
    "completion_marker_vanished",
    "completion_first_sample_missing",
    "completion_confirmation_missing",
    "completion_sample_interval_short",
    "agent_not_idle",
    "completion_elapsed_short",
    "completion_status_inactive",
    "completion_epoch_unstable",
    "observation_epoch_stale",
    "completion_observation_stale",
}


def process_observation(
    task,
    *,
    store,
    enqueue_coordinator_event,
    task_version,
    completion_elapsed,
    now=None,
):
    """Arbitrate one Sentinel sample without weakening the atomic CAS."""
    task = task or {}
    if task.get("completion_protocol") == "receipt-v1":
        return False
    task_id = task.get("task_id")
    if not task_id:
        return False

    now = time.time() if now is None else float(now)
    try:
        observation_result = store.compare_and_set_completion_transition(
            task_id,
            reason="completion_sentinel",
            source="herdr-controller",
            metadata={"sentinel_reason": "completion_sentinel"},
            expected_status=task.get("status"),
            expected_version=task_version(task),
            now=now,
        )
    except (AttributeError, NotImplementedError):
        # A store without the atomic observation gateway is unsupported; never
        # fall back to a weaker status-only compare-and-set.
        return False
    observation = observation_result.get("observation") or {}
    if not observation_result.get("accepted", True):
        if observation_result.get("reason") not in _QUIET_REJECTIONS:
            try:
                store.record_event(
                    "completion_sentinel_cas_rejected",
                    {
                        "task_id": task_id,
                        "reason": observation_result.get("reason"),
                        "expected_status": observation.get("observed_status"),
                        "expected_version": observation.get("observed_version"),
                        "authoritative": observation_result.get("current") or {},
                    },
                    workflow_id=task.get("workflow_id"),
                    node_id=task.get("node") or task.get("stage"),
                    task_id=task_id,
                    source="herdr-controller",
                )
            except (OSError, RuntimeError, ValueError, AttributeError):
                pass
        return False

    try:
        store.record_event(
            "completion_sentinel_accepted",
            {
                "task_id": task_id,
                "elapsed_seconds": completion_elapsed(task, now),
                "confirmations": observation.get("consecutive_samples"),
                "observed_version": observation.get("observed_version"),
            },
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task_id,
            source="herdr-controller",
        )
    except (OSError, RuntimeError, ValueError, AttributeError):
        pass
    enqueue_coordinator_event(observation_result.get("task") or task, "done")
    return True


def process_blocker_observations(
    *,
    store,
    tasks,
    stale_state,
    rejected_state,
    task_version,
    enqueue_coordinator_event,
    transition_task,
    observation_is_current,
):
    """Apply one CAS for each fresh Sentinel blocker sample."""
    processed = 0
    for task in tasks:
        if task.get("status") not in {"dispatched", "working", "rework"}:
            stale_state.pop(task.get("task_id"), None)
            rejected_state.pop(task.get("task_id"), None)
            continue
        try:
            events = store.list_events(
                task_id=task.get("task_id"),
                event_type="blocked_marker_observed",
                limit=1,
                desc=True,
            )
        except (AttributeError, OSError, RuntimeError, ValueError):
            continue
        if not events:
            continue
        payload = events[0].get("payload") or {}
        expected_version = payload.get("observed_version")
        expected_status = payload.get("observed_status") or task.get("status")

        if not observation_is_current(
            payload,
            authoritative_status=task.get("status"),
            authoritative_version=task_version(task),
        ):
            task_id = task["task_id"]
            if stale_state.get(task_id) != expected_version:
                stale_state[task_id] = expected_version
                print(
                    f"[BLOCKED OBSERVATION STALE] "
                    f"task={task_id} "
                    f"observed_version={expected_version} "
                    f"authoritative_version={task_version(task)} -> "
                    f"awaiting fresh Sentinel sample"
                )
            continue
        stale_state.pop(task.get("task_id"), None)

        result = transition_task(
            task_id=task["task_id"],
            to_status="blocked",
            reason="inner_loop_exhausted",
            source="herdr-controller",
            metadata={"sentinel_reason": "inner_loop_exhausted"},
            expected_status=expected_status,
            expected_version=expected_version,
            store=store,
        )
        if not result.get("accepted", True):
            task_id = task["task_id"]
            if rejected_state.get(task_id) != expected_version:
                rejected_state[task_id] = expected_version
                try:
                    store.record_event(
                        "blocked_observation_cas_rejected",
                        {
                            "task_id": task_id,
                            "expected_version": expected_version,
                            "expected_status": expected_status,
                            "authoritative_status": task.get("status"),
                            "authoritative_version": task_version(task),
                            "reason": result.get("reason"),
                        },
                        workflow_id=task.get("workflow_id"),
                        node_id=task.get("node") or task.get("stage"),
                        task_id=task_id,
                        source="herdr-controller",
                    )
                except (OSError, RuntimeError, ValueError, AttributeError):
                    pass
            continue
        rejected_state.pop(task["task_id"], None)
        processed += 1
        fresh = result.get("task") or task
        enqueue_coordinator_event(fresh, "inner_loop_exhausted")
    return processed
