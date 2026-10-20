"""Persist blocked-SLA action receipts and human escalation notifications.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _notify_blocked_human_upgrade(ports, task, episode_id, active_seconds):
    """Send the independent human escalation notification.

    The dedicated channel is not the notifier's task-status change throttle.
    Return ``True`` only when the channel accepted the message; the caller
    records a durable failure event when it did not.
    """
    try:
        import importlib

        notifier = importlib.import_module("services.herdr-notifier")
        task_id = task.get("task_id", "unknown")
        workflow_id = task.get("workflow_id", "unknown")
        url = notifier.build_console_url(workflow_id=workflow_id, task_id=task_id)
        body = (
            f"Task {task_id} blocked {int(active_seconds)}s "
            f"(episode {episode_id}); the one automatic re-push budget is "
            "exhausted.\n"
            "Human confirmation required; no destructive command was run.\n"
            f"  herdr-task set {task_id} working  # after human guidance\n"
            f"  herdr-task supersede {task_id} --abandon "
            f'--reason "blocked {int(active_seconds)}s, repush budget exhausted"  '
            "# discard branch commits\n"
            f"  herdr-task close-workflow {workflow_id} --force  # direct force close"
        )
        notify_fn = getattr(notifier, "notify_human_upgrade", None)
        if callable(notify_fn):
            delivered = notify_fn(task_id, workflow_id, body, url=url)
        else:
            delivered = notifier.notify(
                "Herdr Factory · 阻塞升级（需人工）",
                f"{workflow_id} · {task_id}",
                body,
                url=url,
            )
        return delivered is not False
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        print(f"[BLOCKED SLA NOTIFY ERROR] {exc}")
        return False

def _record_blocked_event(ports, task, event_type, payload):
    event_payload = dict(payload or {})
    event_payload.setdefault("task_id", task.get("task_id"))
    event_payload.setdefault("run_id", task.get("run_id"))
    try:
        ports._get_store().record_event(
            event_type,
            event_payload,
            workflow_id=task.get("workflow_id"),
            node_id=task.get("node") or task.get("stage"),
            task_id=task.get("task_id"),
            agent_id=task.get("agent"),
            run_id=task.get("run_id"),
            source="herdr-controller",
        )
        return True
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        print(f"[BLOCKED SLA EVENT WARN] {event_type}: {exc}")
        return False

def _blocked_sla_record_action(ports, task, decision, now):
    """Persist a coordinator notice or the second-SLA human escalation."""
    task_id = task.get("task_id", "")
    key = ports._blocked_sla_key(task_id)
    episode = ports.attention_get(key) or {}
    action = decision.get("action")
    episode_id = decision.get("episode_id", "")
    if action == "notice":
        count = int(episode.get("coordinator_notices") or 0) + 1
        episode["coordinator_notices"] = count
        episode["last_action_at"] = now
        ports._attention_store.upsert(key, episode)
        ports._record_blocked_event(
            task,
            "blocked_coordinator_notice",
            {"episode_id": episode_id, "count": count,
             "active_seconds": episode.get("active_seconds")},
        )
    elif action == "escalate":
        count = int(episode.get("human_escalations") or 0) + 1
        episode["human_escalations"] = count
        episode["last_action_at"] = now
        ports._attention_store.upsert(key, episode)
        try:
            active = float(episode.get("active_seconds") or 0)
        except (TypeError, ValueError):
            active = 0.0
        delivered = ports._notify_blocked_human_upgrade(task, episode_id, active)
        ports._record_blocked_event(
            task,
            "blocked_human_escalated" if delivered else "blocked_human_escalation_failed",
            {"episode_id": episode_id, "count": count,
             "active_seconds": active, "delivered": delivered},
        )
    elif action == "suppressed_human":
        ports._record_blocked_event(
            task,
            "auto_action_suppressed_human_present",
            {"episode_id": episode_id},
        )

def _blocked_sla_record_repush(ports, task, decision, success, detail, now):
    """Finalize only the claim that owns this prompt attempt."""
    task_id = task.get("task_id", "")
    key = ports._blocked_sla_key(task_id)
    claim_id = decision.get("claim_id")
    stale = False
    updated = {}
    with ports._attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        claim = episode.get("repush_claim") or {}
        if claim_id and claim.get("claim_id") != claim_id:
            stale = True
        else:
            episode["repushes"] = 1
            episode["delivery_attempts"] = int(episode.get("delivery_attempts") or 0) + 1
            episode["repush_state"] = "delivered" if success else "failed"
            episode["repush_inflight_at"] = None
            episode["repush_claim"] = None
            episode["last_action_at"] = now
            episode["last_repush_at"] = now
            if detail:
                key_name = "last_repush_receipt" if success else "last_repush_error"
                episode[key_name] = str(detail)[:500]
            episodes[key] = episode
            updated = dict(episode)

    if stale:
        ports._record_blocked_event(
            task,
            "blocked_repush_stale_result",
            {"episode_id": decision.get("episode_id", ""), "claim_id": claim_id},
        )
        return {}

    payload = {
        "episode_id": decision.get("episode_id", ""),
        "claim_id": decision.get("claim_id", ""),
        "repush_number": decision.get("repush_number", 1),
        "count": updated.get("repushes", 1),
        "delivered": bool(success),
        "detail": str(detail or "")[:500],
    }
    if success:
        event_type = (
            "blocked_auto_repush_recovered"
            if decision.get("action") == "repush_recover"
            else "blocked_auto_repush"
        )
    else:
        event_type = "prompt_delivery_failed"
    ports._record_blocked_event(task, event_type, payload)
    if not success:
        ports._record_blocked_event(task, "blocked_repush_failed", payload)
    return updated

def _blocked_sla_record_escalation(ports, task, decision, delivered, now):
    """Finalize a claimed human escalation without duplicate notification."""
    key = ports._blocked_sla_key(task.get("task_id", ""))
    claim_id = decision.get("claim_id")
    stale = False
    with ports._attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        claim = episode.get("human_escalation_claim") or {}
        if claim_id and claim.get("claim_id") != claim_id:
            stale = True
        else:
            episode["human_escalation_state"] = "delivered" if delivered else "failed"
            episode["human_escalation_claim"] = None
            episode["last_action_at"] = now
            if delivered:
                episode["human_escalations"] = max(
                    1, int(episode.get("human_escalations") or 0)
                )
            episodes[key] = episode
    if stale:
        ports._record_blocked_event(
            task,
            "blocked_human_escalation_stale_result",
            {"episode_id": decision.get("episode_id", ""), "claim_id": claim_id},
        )
        return False
    ports._record_blocked_event(
        task,
        "blocked_human_escalated" if delivered else "blocked_human_escalation_failed",
        {
            "episode_id": decision.get("episode_id", ""),
            "claim_id": decision.get("claim_id", ""),
            "count": int(episode.get("human_escalations") or 1),
            "active_seconds": episode.get("active_seconds"),
            "delivered": bool(delivered),
        },
    )
    return bool(delivered)


DEPENDENCIES = {'_notify_blocked_human_upgrade': (),
 '_record_blocked_event': ('_get_store',),
 '_blocked_sla_record_action': ('_attention_store', '_blocked_sla_key',
                                '_notify_blocked_human_upgrade', '_record_blocked_event',
                                'attention_get'),
 '_blocked_sla_record_repush': ('_attention_store', '_blocked_sla_key', '_record_blocked_event'),
 '_blocked_sla_record_escalation': ('_attention_store', '_blocked_sla_key',
                                    '_record_blocked_event')}

__all__ = tuple(DEPENDENCIES)
