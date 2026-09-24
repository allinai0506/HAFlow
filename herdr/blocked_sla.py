"""FR-2 blocked-SLA policy (pure, no I/O).

Design (plan-arch T4 + plan-attack A4/B-2/B-3裁定):
  * Cancel automatic ``herdr agent prompt`` re-push (N=0). Re-prompting an
    ``inner_loop_exhausted`` pane repeats the exhausted loop (B-4 proven).
  * Active wall-clock SLA: only sweep ticks with ``tick_dt <= POLL_CAP``
    accumulate. Offline/sleep gaps (tick_dt >> POLL) do not count, so the
    8h overnight segment never fires on recovery.
  * Episode dedup key = ``blocked_episode_id`` = task_id + entry updated_at.
    One human escalation per episode, plus one bounded coordinator notice.
    Never reuses ``services/herdr-notifier.py:129`` state-change dedup.
  * Human-machine mutex: skip automatic action while a human is present
    (task ``interrupted``/``paused`` or recent human status write).

Policy values (explicit, failable):
  FIRST_SLA_SECONDS   = 1800 (30min, matches liveness stall threshold)
  SECOND_SLA_SECONDS  = 1800 (one more cycle -> human escalation)
  COOLDOWN_SECONDS    = 600  (matches attention retry interval)
  JITTER_WINDOW       = 120  (reuses liveness.attention_grace, no new magic)
  MAX_HUMAN_ESCALATIONS_PER_EPISODE = 1
  MAX_COORDINATOR_NOTICES_PER_EPISODE = 2
"""

from __future__ import annotations

import os

FIRST_SLA_SECONDS = 1800.0
SECOND_SLA_SECONDS = 1800.0
COOLDOWN_SECONDS = 600.0
JITTER_WINDOW_SECONDS = 120.0
POLL_CAP_MULTIPLIER = 3.0
MAX_HUMAN_ESCALATIONS_PER_EPISODE = 1
MAX_COORDINATOR_NOTICES_PER_EPISODE = 2


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def first_sla_seconds() -> float:
    """First SLA threshold (blocked entry -> coordinator notice)."""
    return _env_float("HERDR_BLOCKED_FIRST_SLA", FIRST_SLA_SECONDS)


def second_sla_seconds() -> float:
    """Second SLA window (coordinator notice -> human escalation)."""
    return _env_float("HERDR_BLOCKED_SECOND_SLA", SECOND_SLA_SECONDS)


def cooldown_seconds() -> float:
    """Minimum interval between two automatic actions on one episode."""
    return _env_float("HERDR_BLOCKED_COOLDOWN", COOLDOWN_SECONDS)


def jitter_window_seconds() -> float:
    """Debounce for dispatch-time blocked flapping (reuses grace)."""
    return _env_float("HERDR_BLOCKED_JITTER_WINDOW", JITTER_WINDOW_SECONDS)


def blocked_episode_id(task_id: str, entry_updated_at: float) -> str:
    """Stable dedup key: reset (not accumulate) on re-entry."""
    try:
        entry_ts = int(float(entry_updated_at))
    except (TypeError, ValueError):
        entry_ts = 0
    return f"{task_id}:{entry_ts}"


def is_jitter(entry_age_seconds: float) -> bool:
    """True inside the dispatch-flap debounce window."""
    try:
        return float(entry_age_seconds) < jitter_window_seconds()
    except (TypeError, ValueError):
        return False


def active_tick_increment(tick_dt: float, poll_seconds: float = 3.0) -> float:
    """Active wall-clock increment for one sweep tick.

    Caps a single tick at ``POLL_CAP_MULTIPLIER * poll`` so controller
    downtime/sleep never accrues SLA credit (B-3).
    """
    try:
        dt = float(tick_dt)
        poll = float(poll_seconds)
    except (TypeError, ValueError):
        return 0.0
    if dt <= 0 or poll <= 0:
        return 0.0
    return min(dt, POLL_CAP_MULTIPLIER * poll)


def human_present(task: dict | None) -> bool:
    """True when a human is already handling the task (mutex)."""
    if not isinstance(task, dict):
        return False
    status = task.get("status")
    if status in ("interrupted", "paused"):
        return True
    # A recent human status write is recorded via sentinel_reason/source.
    # Only explicit human sources count; machine re-pushes do not.
    for key in ("last_human_action_at", "human_present"):
        if task.get(key):
            return True
    source = str(task.get("last_action_source") or "")
    return source in ("human", "cli_set_status_human")


def should_notice_coordinator(
    *,
    active_seconds: float,
    coordinator_notices: int,
    last_action_at: float | None,
    now: float,
) -> bool:
    """First-level notice (to coordinator queue) guard."""
    if active_seconds < first_sla_seconds():
        return False
    if coordinator_notices >= MAX_COORDINATOR_NOTICES_PER_EPISODE:
        return False
    if last_action_at is not None:
        try:
            if float(now) - float(last_action_at) < cooldown_seconds():
                return False
        except (TypeError, ValueError):
            return False
    return True


def should_escalate_human(
    *,
    active_seconds: float,
    human_escalations: int,
    last_action_at: float | None,
    now: float,
) -> bool:
    """Second-level human escalation guard (exactly once per episode)."""
    threshold = first_sla_seconds() + second_sla_seconds()
    if active_seconds < threshold:
        return False
    if human_escalations >= MAX_HUMAN_ESCALATIONS_PER_EPISODE:
        return False
    if last_action_at is not None:
        try:
            if float(now) - float(last_action_at) < cooldown_seconds():
                return False
        except (TypeError, ValueError):
            return False
    return True


def decide_blocked_action(
    *,
    task: dict | None,
    episode: dict | None,
    now: float,
) -> dict:
    """Pure per-episode decision (no I/O, no prompt send).

    Returns ``{"action": "none"|"notice"|"escalate"|"suppressed_human"|
    "suppressed_jitter"|"suppressed_cooldown"|"suppressed_bounds",
    "episode_id": ..., "reason": ...}``.
    Automatic ``herdr agent prompt`` re-push is intentionally absent
    (N=0 by design; see module docstring).
    """
    task = task or {}
    episode = episode or {}
    task_id = str(task.get("task_id") or episode.get("task_id") or "")
    entry_updated_at = episode.get("entry_updated_at", task.get("updated_at") or 0)
    episode_id = blocked_episode_id(task_id, entry_updated_at or 0)
    try:
        active = float(episode.get("active_seconds") or 0)
    except (TypeError, ValueError):
        active = 0.0
    try:
        entry_age = float(now) - float(entry_updated_at or now)
    except (TypeError, ValueError):
        entry_age = 0.0
    if is_jitter(entry_age):
        return {
            "action": "suppressed_jitter",
            "episode_id": episode_id,
            "reason": "dispatch_flap_window",
        }
    if human_present(task):
        return {
            "action": "suppressed_human",
            "episode_id": episode_id,
            "reason": "human_present_mutex",
        }
    try:
        human_n = int(episode.get("human_escalations") or 0)
    except (TypeError, ValueError):
        human_n = 0
    try:
        coord_n = int(episode.get("coordinator_notices") or 0)
    except (TypeError, ValueError):
        coord_n = 0
    last_action = episode.get("last_action_at")
    if should_escalate_human(
        active_seconds=active,
        human_escalations=human_n,
        last_action_at=last_action,
        now=now,
    ):
        return {
            "action": "escalate",
            "episode_id": episode_id,
            "reason": "second_sla_human_upgrade",
        }
    if should_notice_coordinator(
        active_seconds=active,
        coordinator_notices=coord_n,
        last_action_at=last_action,
        now=now,
    ):
        return {
            "action": "notice",
            "episode_id": episode_id,
            "reason": "first_sla_coordinator_notice",
        }
    # Distinguish bounds vs cooldown for observability.
    if active >= first_sla_seconds() + second_sla_seconds() and human_n >= 1:
        return {
            "action": "suppressed_bounds",
            "episode_id": episode_id,
            "reason": "episode_already_escalated_once",
        }
    if last_action is not None:
        try:
            if float(now) - float(last_action) < cooldown_seconds():
                return {
                    "action": "suppressed_cooldown",
                    "episode_id": episode_id,
                    "reason": "cooldown_active",
                }
        except (TypeError, ValueError):
            pass
    return {"action": "none", "episode_id": episode_id, "reason": "sla_not_reached"}
