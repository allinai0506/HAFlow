"""Persist and throttle coordinator attention episodes and user notifications.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def attention_get(ports, key):
    return ports._attention_store.get(key)

def attention_note(ports, key, task, event_type, reason, attempts=None, next_retry_at=None, detail=None):
    """Record/refresh an attention episode for a stalled or undeliverable event."""
    episode = ports._attention_store.get(key) or {}
    now = ports.time.time()
    fields = {
        "task_id": task.get("task_id"),
        "workflow_id": task.get("workflow_id"),
        "event_type": event_type,
        "reason": reason,
        "last_attempt_at": now,
    }
    if task.get("execution_id"):
        fields["execution_id"] = task["execution_id"]
    if attempts is not None:
        fields["attempts"] = int(attempts)
    if next_retry_at is not None:
        fields["next_retry_at"] = float(next_retry_at)
    if detail:
        fields["detail"] = detail
    if not episode.get("first_seen_at"):
        fields["first_seen_at"] = now
    return ports._attention_store.upsert(key, fields)

def attention_clear(ports, key):
    episode = ports._attention_store.get(key) or {}
    if episode.get("reason") == "direct_dispatch_delivery_unknown":
        workflow_id = str(episode.get("workflow_id") or "").strip()
        task_id = str(episode.get("task_id") or "").strip()
        node_id = task_id.split(":", 1)[1] if task_id.startswith("stage_advance:") else ""
        if workflow_id and node_id:
            # Clear the latch first. A crash before the attention clear remains
            # safely blocked and the same recovery action can be retried.
            ports.clear_stage_advance(workflow_id, node_id)
    return ports._attention_store.clear(key)

def mark_stage_advance_notified(ports, workflow_id, target_node_id, *, clear_attention=True):
    key = ports.stage_advance_key(workflow_id, target_node_id)
    with ports.lock:
        state = ports.load_stage_state()
        state[key] = "notified"
        ports.save_stage_state(state)

    if clear_attention:
        try:
            ports.attention_clear(f"{workflow_id}:stage_advance:{target_node_id}")
        except Exception as exc:
            print(
                f"[STAGE ADVANCE ATTENTION CLEAR ERROR] "
                f"workflow={workflow_id} node={target_node_id}: {exc}"
            )

def attention_blocks_retry(ports, key, now=None):
    return ports.liveness.blocks_retry(ports._attention_store, key, now)

def attention_throttle(ports, key, interval=None, now=None):
    ports.liveness.throttle_retry(ports._attention_store, key, interval=interval, now=now)

def notify_attention(ports, title, task, message, reason):
    """Best-effort macOS notification; never let notifier failure break control flow."""
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        url = notifier.build_console_url(
            workflow_id=task.get("workflow_id"), task_id=task.get("task_id")
        )
        return bool(notifier.notify(title, f"{task.get('workflow_id', 'unknown')} · {reason}", message, url=url))
    except Exception as exc:
        print(f"[ATTENTION NOTIFY ERROR] {exc}")
        return False

def _blocked_sla_key(ports, task_id):
    return f"{task_id}:blocked_sla"


DEPENDENCIES = {'attention_get': ('_attention_store',),
 'attention_note': ('_attention_store', 'time'),
 'attention_clear': ('_attention_store', 'clear_stage_advance'),
 'mark_stage_advance_notified': ('attention_clear', 'load_stage_state', 'lock',
                                 'save_stage_state', 'stage_advance_key'),
 'attention_blocks_retry': ('_attention_store', 'liveness'),
 'attention_throttle': ('_attention_store', 'liveness'),
 'notify_attention': (),
 '_blocked_sla_key': ()}

__all__ = tuple(DEPENDENCIES)
