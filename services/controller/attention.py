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
    return ports._attention_store.clear(key)

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
 'attention_clear': ('_attention_store',),
 'attention_blocks_retry': ('_attention_store', 'liveness'),
 'attention_throttle': ('_attention_store', 'liveness'),
 'notify_attention': (),
 '_blocked_sla_key': ()}

__all__ = tuple(DEPENDENCIES)
