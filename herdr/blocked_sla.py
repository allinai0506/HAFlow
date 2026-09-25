"""FR-2 blocked-SLA policy (pure, no I/O).

The policy is deliberately separate from the Controller shell:

* one automatic ``herdr agent prompt`` re-push per blocked episode (N=1);
* an absent/present task episode boundary prevents dispatch flapping from
  consuming the budget;
* active-clock credit is bounded per sweep, so sleep/offline time does not
  cause a recovery-time storm;
* the second SLA is a separate, once-per-episode human escalation; and
* prompt delivery is a recoverable state transition with an observable result.

No function in this module sends a prompt or writes state.  The Controller
owns those side effects and records the corresponding events.
"""

from __future__ import annotations

import os

FIRST_SLA_SECONDS = 1800.0
SECOND_SLA_SECONDS = 1800.0
COOLDOWN_SECONDS = 600.0
JITTER_WINDOW_SECONDS = 120.0
POLL_CAP_MULTIPLIER = 3.0
MAX_AUTO_REPUSHES_PER_EPISODE = 1
MAX_REPUSH_DELIVERY_RETRIES = 1
MAX_HUMAN_ESCALATIONS_PER_EPISODE = 1
MAX_HUMAN_ESCALATION_DELIVERY_RETRIES = 1
MAX_COORDINATOR_NOTICES_PER_EPISODE = 1
REPUSH_CLAIM_LEASE_SECONDS = 180.0
HUMAN_ESCALATION_CLAIM_LEASE_SECONDS = 60.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return value


def first_sla_seconds() -> float:
    """First SLA threshold for the one automatic re-push."""
    return _env_float("HERDR_BLOCKED_FIRST_SLA", FIRST_SLA_SECONDS)


def second_sla_seconds() -> float:
    """Additional SLA window before human escalation."""
    return _env_float("HERDR_BLOCKED_SECOND_SLA", SECOND_SLA_SECONDS)


def cooldown_seconds() -> float:
    """Minimum spacing between side effects in one episode."""
    return _env_float("HERDR_BLOCKED_COOLDOWN", COOLDOWN_SECONDS)


def jitter_window_seconds() -> float:
    """Short dispatch/blocked flap window excluded from automatic action."""
    return _env_float("HERDR_BLOCKED_JITTER_WINDOW", JITTER_WINDOW_SECONDS)


def blocked_episode_id(
    task_id: str,
    entry_updated_at: float,
    entry_version: int | None = None,
) -> str:
    """Return a stable episode key, including the task epoch when available."""
    try:
        entry_ts = int(float(entry_updated_at))
    except (TypeError, ValueError):
        entry_ts = 0
    key = f"{task_id}:{entry_ts}"
    if entry_version is not None:
        try:
            key = f"{key}:{int(entry_version)}"
        except (TypeError, ValueError):
            pass
    return key


def is_jitter(entry_age_seconds: float) -> bool:
    """True only inside the short dispatch-time flapping window."""
    try:
        return float(entry_age_seconds) < jitter_window_seconds()
    except (TypeError, ValueError):
        return False


def active_tick_increment(tick_dt: float, poll_seconds: float = 3.0) -> float:
    """Credit one active sweep, capped across sleep/offline gaps."""
    try:
        delta = float(tick_dt)
        poll = float(poll_seconds)
    except (TypeError, ValueError):
        return 0.0
    if delta <= 0 or poll <= 0:
        return 0.0
    return min(delta, POLL_CAP_MULTIPLIER * poll)


def _int(value, default=0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def human_present(task: dict | None) -> bool:
    """Detect a human handling signal without treating machine writes as human."""
    if not isinstance(task, dict):
        return False
    if task.get("status") in {"interrupted", "paused"}:
        return True
    if task.get("human_present") or task.get("last_human_action_at"):
        return True
    if str(task.get("last_action_source") or "") in {
        "human", "cli_set_status_human", "operator",
    }:
        return True
    history = task.get("status_history")
    if isinstance(history, list) and history:
        latest = history[-1]
        if not isinstance(latest, dict):
            return False
        source = str(latest.get("source") or "")
        reason = str(latest.get("reason") or "")
        return source in {"human", "cli_set_status_human"} or reason in {
            "cli_set_status", "human_set_status",
        }
    return False


def should_repush(
    *,
    active_seconds: float,
    repushes: int,
    last_action_at: float | None,
    now: float,
) -> bool:
    """Guard the single logical automatic re-push for an episode."""
    try:
        active = float(active_seconds)
    except (TypeError, ValueError):
        return False
    if active < first_sla_seconds() or _int(repushes) >= MAX_AUTO_REPUSHES_PER_EPISODE:
        return False
    if last_action_at is not None:
        try:
            if float(now) - float(last_action_at) < cooldown_seconds():
                return False
        except (TypeError, ValueError):
            return False
    return True


def should_recover_repush(
    *,
    active_seconds: float,
    repush_state: str,
    recovery_attempts: int,
    last_action_at: float | None,
    now: float,
) -> bool:
    """Allow one transport retry without spending a second logical re-push."""
    try:
        active = float(active_seconds)
    except (TypeError, ValueError):
        return False
    if active < first_sla_seconds() or repush_state != "failed":
        return False
    if _int(recovery_attempts) >= MAX_REPUSH_DELIVERY_RETRIES:
        return False
    if last_action_at is not None:
        try:
            if float(now) - float(last_action_at) < cooldown_seconds():
                return False
        except (TypeError, ValueError):
            return False
    return True


def repush_inflight_timeout_seconds() -> float:
    """Return the lease after which an interrupted worker may be retried."""
    return _env_float(
        "HERDR_BLOCKED_REPUSH_TIMEOUT", REPUSH_CLAIM_LEASE_SECONDS
    )


def repush_inflight_is_stale(
    episode: dict | None,
    *,
    now: float,
    timeout_seconds: float | None = None,
) -> bool:
    """Detect a worker lease left behind by a Controller restart."""
    episode = episode or {}
    if episode.get("repush_state") != "in_flight":
        return False
    started = episode.get("repush_inflight_at")
    if started is None:
        return True
    try:
        timeout = (
            repush_inflight_timeout_seconds()
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        return float(now) - float(started) >= timeout
    except (TypeError, ValueError):
        return True


def recover_stale_repush(episode: dict, *, now: float) -> dict:
    """Release an abandoned worker lease so one bounded retry can run."""
    updated = dict(episode or {})
    updated["repush_state"] = "failed"
    updated["repush_inflight_at"] = None
    updated["last_repush_recovery_at"] = now
    return updated


def claim_is_active(
    claim: object,
    now: float,
    *,
    lease_seconds: float,
) -> bool:
    """Return whether a side-effect claim still owns its lease.

    Malformed non-empty claims fail closed: an operator must be able to recover
    them explicitly rather than accidentally launching a second prompt.
    """
    if not isinstance(claim, dict) or not claim:
        return False
    try:
        lease_until = float(claim["lease_until"])
    except (KeyError, TypeError, ValueError):
        return True
    return lease_until > float(now)


def claim_is_expired(
    claim: object,
    now: float,
    *,
    lease_seconds: float,
) -> bool:
    """Inverse of :func:`claim_is_active` for a known claim shape."""
    if not isinstance(claim, dict) or not claim:
        return True
    try:
        lease_until = float(claim["lease_until"])
    except (KeyError, TypeError, ValueError):
        return False
    return lease_until <= float(now)


def new_claim(claim_id: str, now: float, *, lease_seconds: float) -> dict:
    """Create a bounded, observable side-effect lease."""
    return {
        "claim_id": str(claim_id),
        "claimed_at": float(now),
        "lease_until": float(now) + float(lease_seconds),
    }


def should_notice_coordinator(
    *,
    active_seconds: float,
    coordinator_notices: int,
    last_action_at: float | None,
    now: float,
) -> bool:
    """Compatibility guard for a bounded coordinator notice."""
    try:
        active = float(active_seconds)
    except (TypeError, ValueError):
        return False
    if active < first_sla_seconds() or _int(coordinator_notices) >= MAX_COORDINATOR_NOTICES_PER_EPISODE:
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
    """Guard the independent second-SLA human escalation."""
    try:
        active = float(active_seconds)
    except (TypeError, ValueError):
        return False
    if active < first_sla_seconds() + second_sla_seconds():
        return False
    if _int(human_escalations) >= MAX_HUMAN_ESCALATIONS_PER_EPISODE:
        return False
    if last_action_at is not None:
        try:
            if float(now) - float(last_action_at) < cooldown_seconds():
                return False
        except (TypeError, ValueError):
            return False
    return True


def new_episode(task: dict | None, now: float) -> dict:
    """Create the durable episode shell used by the Controller."""
    task = task or {}
    try:
        entry = float(task.get("updated_at") or now)
    except (TypeError, ValueError):
        entry = float(now)
    version = task.get("version")
    return {
        "task_id": str(task.get("task_id") or ""),
        "workflow_id": task.get("workflow_id"),
        "run_id": task.get("run_id"),
        "entry_updated_at": entry,
        "entry_version": version,
        "episode_id": blocked_episode_id(
            str(task.get("task_id") or ""), entry, version
        ),
        "active_seconds": 0.0,
        "last_tick_at": now,
        "coordinator_notices": 0,
        "repushes": 0,
        "repush_state": "pending",
        "repush_inflight_at": None,
        "last_repush_recovery_at": None,
        "delivery_attempts": 0,
        "recovery_attempts": 0,
        "human_escalations": 0,
        "last_action_at": None,
    }


def decide_blocked_action(
    *,
    task: dict | None,
    episode: dict | None,
    now: float,
) -> dict:
    """Return the next side-effect decision for one blocked episode.

    ``repush`` is attempted before ``escalate`` so a Controller that was
    offline across both deadlines performs the required one re-push first;
    the next sweep can then perform the one human escalation.
    """
    task = task or {}
    episode = episode or {}
    task_id = str(task.get("task_id") or episode.get("task_id") or "")
    entry = episode.get("entry_updated_at", task.get("updated_at") or now)
    version = episode.get("entry_version", task.get("version"))
    episode_id = str(
        episode.get("episode_id")
        or blocked_episode_id(task_id, entry, version)
    )
    try:
        active = float(episode.get("active_seconds") or 0)
    except (TypeError, ValueError):
        active = 0.0
    try:
        entry_age = float(now) - float(entry)
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

    repushes = _int(episode.get("repushes"))
    human_n = _int(episode.get("human_escalations"))
    last_action = episode.get("last_action_at")
    if should_repush(
        active_seconds=active,
        repushes=repushes,
        last_action_at=last_action,
        now=now,
    ):
        return {
            "action": "repush",
            "episode_id": episode_id,
            "repush_number": repushes + 1,
            "reason": "first_sla_automatic_repush",
        }
    if should_recover_repush(
        active_seconds=active,
        repush_state=str(episode.get("repush_state") or "pending"),
        recovery_attempts=_int(episode.get("recovery_attempts")),
        last_action_at=last_action,
        now=now,
    ):
        return {
            "action": "repush_recover",
            "episode_id": episode_id,
            "repush_number": 1,
            "reason": "prompt_delivery_recovery",
        }
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
    if active >= first_sla_seconds() and repushes >= MAX_AUTO_REPUSHES_PER_EPISODE:
        return {
            "action": "suppressed_bounds",
            "episode_id": episode_id,
            "reason": "repush_budget_exhausted",
        }
    if human_n >= MAX_HUMAN_ESCALATIONS_PER_EPISODE:
        return {
            "action": "suppressed_bounds",
            "episode_id": episode_id,
            "reason": "human_escalation_already_sent",
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


def mark_repush_result(
    episode: dict,
    *,
    success: bool,
    now: float,
    detail: str = "",
    recovery: bool = False,
) -> dict:
    """Return the durable episode update after a prompt attempt.

    The logical N=1 budget is consumed even when delivery fails.  The failure
    is retained as ``repush_state=failed`` so a later sweep can escalate and a
    recovery/reconciliation tool can inspect the exact error without guessing.
    """
    updated = dict(episode or {})
    updated["repushes"] = MAX_AUTO_REPUSHES_PER_EPISODE
    updated["delivery_attempts"] = _int(updated.get("delivery_attempts")) + 1
    if recovery:
        updated["recovery_attempts"] = _int(updated.get("recovery_attempts")) + 1
    updated["repush_state"] = "delivered" if success else "failed"
    updated["last_action_at"] = now
    updated["last_repush_at"] = now
    if detail:
        updated["last_repush_error" if not success else "last_repush_receipt"] = str(detail)[:500]
    return updated
