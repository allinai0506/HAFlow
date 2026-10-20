"""Coordinator event identity and in-process queue admission policy."""

import hashlib
import json


def blocker_episode(task):
    """Return the persisted blocker identity used to distinguish reopens."""
    history = task.get("status_history") or []
    latest = history[-1] if history and isinstance(history[-1], dict) else None
    identity = {
        "workflow_id": task.get("workflow_id"),
        "run_id": task.get("run_id"),
        "history_length": len(history),
        "transition": latest,
    }
    if latest is None:
        identity["legacy_version"] = task.get("version")
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def enqueue(task, event_type, *, lock, queued_events, coordinator_queue):
    """Admit one coordinator event using the caller's shared queue state."""
    key = f"{task['task_id']}:{event_type}"
    episode = (
        blocker_episode(task)
        if event_type in {"blocked", "inner_loop_exhausted"} else None
    )
    queue_key = f"{key}:{episode}" if episode is not None else key

    with lock:
        if queue_key in queued_events:
            print(f"[QUEUE DUPLICATE SKIPPED] {key}")
            return
        queued_events.add(queue_key)

    coordinator_queue.put(
        {
            "task_id": task["task_id"],
            "event_type": event_type,
            "key": key,
            "queue_key": queue_key,
            "blocker_episode": episode,
        }
    )
    print(f"[QUEUE] task={task['task_id']} event={event_type}")
