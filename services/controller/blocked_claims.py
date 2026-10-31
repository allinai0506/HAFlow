"""Fence blocked-SLA actions against the current persisted task episode.

Dependencies are explicit inputs; importing this module creates no runtime state.
"""

def _blocked_episode_matches_task(ports, episode, task):
    """Return whether a persisted SLA episode belongs to this task epoch."""
    if not isinstance(episode, dict) or not episode or not isinstance(task, dict) or not task:
        return False
    from herdr import blocked_sla as _sla

    # Explicit human intervention invalidates the episode
    if episode.get("human_touch") != task.get("human_touch"):
        return False

    episode_run = episode.get("run_id")
    task_run = task.get("run_id")
    same_run = not episode_run or str(episode_run) == str(task_run)
    episode_workflow = episode.get("workflow_id")
    task_workflow = task.get("workflow_id")
    same_workflow = not episode_workflow or str(episode_workflow) == str(task_workflow)
    if not (same_run and same_workflow):
        return False

    # Check status entry timestamp first to avoid resetting SLA on benign heartbeat/metadata touches
    task_stable_entry = _sla.task_blocked_entry_ts(task)
    ep_stable_entry = episode.get("entry_status_updated_at")
    if ep_stable_entry is not None and task_stable_entry is not None:
        try:
            return abs(float(ep_stable_entry) - float(task_stable_entry)) < 0.001
        except (TypeError, ValueError):
            return False

    # Fallback for legacy episodes without entry_status_updated_at
    try:
        same_entry = float(episode.get("entry_updated_at")) == float(task.get("updated_at"))
    except (TypeError, ValueError):
        same_entry = False
    try:
        same_version = int(episode.get("entry_version")) == int(task.get("version"))
    except (TypeError, ValueError):
        same_version = episode.get("entry_version") is None and task.get("version") is None
    return same_entry and same_version

def _claim_blocked_sla_action(ports, task, now, poll_seconds=3.0):
    """Atomically advance and claim one SLA side effect for an episode.

    The episode file is the cross-process authority.  Selection, budget
    reservation, and the claim lease happen under one EpisodeStore transaction
    so two Controller instances cannot both send the one permitted prompt.
    """
    from herdr import blocked_sla as _sla

    task = task or {}
    task_id = str(task.get("task_id") or "")
    key = ports._blocked_sla_key(task_id)
    now = float(now)
    claim_id = ports.uuid.uuid4().hex
    with ports._attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        if not ports._blocked_episode_matches_task(episode, task):
            episode = _sla.new_episode(task, now)
        else:
            episode["run_id"] = task.get("run_id")
            episode["workflow_id"] = task.get("workflow_id")
            try:
                last_tick = float(episode.get("last_tick_at") or now)
            except (TypeError, ValueError):
                last_tick = now
            episode["active_seconds"] = float(episode.get("active_seconds") or 0) + _sla.active_tick_increment(
                max(0.0, now - last_tick), poll_seconds=poll_seconds
            )
            episode["last_tick_at"] = now

        decision = _sla.decide_blocked_action(
            task=task, episode=episode, now=now
        )
        action = decision.get("action")
        decision["claimed"] = False

        # A live claim blocks every competing side effect for the episode,
        # including a later SLA escalation.
        active_repush_claim = episode.get("repush_claim")
        active_human_claim = episode.get("human_escalation_claim")
        if (
            _sla.claim_is_active(
                active_repush_claim,
                now,
                lease_seconds=_sla.REPUSH_CLAIM_LEASE_SECONDS,
            )
            or _sla.claim_is_active(
                active_human_claim,
                now,
                lease_seconds=_sla.HUMAN_ESCALATION_CLAIM_LEASE_SECONDS,
            )
        ):
            decision["action"] = "suppressed_inflight"
            decision["reason"] = "durable_sla_claim_inflight"
            episodes[key] = episode
            return episode, decision

        if action in {"repush", "repush_recover"}:
            previous_state = str(episode.get("repush_state") or "pending")
            recovery = (
                action == "repush_recover"
                or previous_state == "failed"
                or previous_state == "in_flight"
            )
            attempts = int(episode.get("recovery_attempts") or 0)
            if recovery and attempts >= _sla.MAX_REPUSH_DELIVERY_RETRIES:
                decision["action"] = "suppressed_bounds"
                decision["reason"] = "repush_recovery_budget_exhausted"
            else:
                if recovery:
                    episode["recovery_attempts"] = attempts + 1
                episode["repush_claim"] = _sla.new_claim(
                    claim_id,
                    now,
                    lease_seconds=_sla.REPUSH_CLAIM_LEASE_SECONDS,
                )
                episode["repush_state"] = "in_flight"
                episode["repush_inflight_at"] = now
                episode["last_action_at"] = now
                episode["last_repush_at"] = now
                decision["claim_id"] = claim_id
                decision["recovery"] = recovery
                if recovery:
                    decision["action"] = "repush_recover"
                decision["claimed"] = True
        elif action == "escalate":
            attempts = int(episode.get("human_escalation_attempts") or 0)
            if attempts > _sla.MAX_HUMAN_ESCALATION_DELIVERY_RETRIES:
                decision["action"] = "suppressed_bounds"
                decision["reason"] = "human_escalation_delivery_budget_exhausted"
            else:
                episode["human_escalation_attempts"] = attempts + 1
                episode["human_escalation_claim"] = _sla.new_claim(
                    claim_id,
                    now,
                    lease_seconds=_sla.HUMAN_ESCALATION_CLAIM_LEASE_SECONDS,
                )
                episode["human_escalation_state"] = "in_flight"
                episode["last_action_at"] = now
                decision["claim_id"] = claim_id
                decision["claimed"] = True

        episodes[key] = episode
        return episode, decision

def _blocked_task_claim_is_current(ports, task, decision):
    """Revalidate task/run/pane identity immediately before external I/O."""
    if not decision.get("claimed"):
        return True, "not_claimed"
    task_id = str(task.get("task_id") or "")
    try:
        authoritative = ports._get_store().get_task(task_id)
    except (AttributeError, OSError, RuntimeError, ValueError):
        return False, "task_read_failed"
    if not authoritative:
        return False, "task_missing"
    if authoritative.get("status") != "blocked":
        return False, "task_status_changed"
    if ports._task_version(authoritative) != ports._task_version(task):
        return False, "task_version_changed"
    if authoritative.get("pane_id") != task.get("pane_id"):
        return False, "task_pane_changed"
    if task.get("run_id") and authoritative.get("run_id") != task.get("run_id"):
        return False, "task_run_changed"
    if task.get("workflow_id") and authoritative.get("workflow_id") != task.get("workflow_id"):
        return False, "task_workflow_changed"
    return True, "current"

def _release_stale_blocked_claim(ports, task, decision, reason):
    """Release a claim without charging its one-shot budget."""
    key = ports._blocked_sla_key(str(task.get("task_id") or ""))
    claim_id = decision.get("claim_id")
    with ports._attention_store.transaction() as episodes:
        episode = dict(episodes.get(key) or {})
        claim = episode.get("repush_claim") or episode.get("human_escalation_claim") or {}
        if claim_id and claim.get("claim_id") == claim_id:
            if "repush_claim" in episode:
                episode["repush_claim"] = None
                episode["repush_state"] = "pending"
                episode["repush_inflight_at"] = None
            if "human_escalation_claim" in episode:
                episode["human_escalation_claim"] = None
                episode["human_escalation_state"] = "pending"
            episodes[key] = episode
    ports._record_blocked_event(
        task,
        "blocked_sla_claim_aborted",
        {"episode_id": decision.get("episode_id", ""), "claim_id": claim_id, "reason": reason},
    )
    decision["action"] = "suppressed_stale_task"
    decision["reason"] = reason
    decision["claimed"] = False
    return decision


DEPENDENCIES = {'_blocked_episode_matches_task': (),
 '_claim_blocked_sla_action': ('_attention_store', '_blocked_episode_matches_task',
                               '_blocked_sla_key', 'uuid'),
 '_blocked_task_claim_is_current': ('_get_store', '_task_version'),
 '_release_stale_blocked_claim': ('_attention_store', '_blocked_sla_key', '_record_blocked_event')}

__all__ = tuple(DEPENDENCIES)
