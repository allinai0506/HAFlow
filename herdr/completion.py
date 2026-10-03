"""FR-1 completion decision layer (pure, no I/O).

Ownership: Sentinel detects pane markers, Controller arbitrates final
transitions. This module holds the pure triple-condition and epoch logic so
both daemons share one policy without duplicating mutable state.

Triple condition (plan-arch T3, A-1裁定):
  accept iff marker newly appeared (absent -> present) AND
  agent_status in {"idle", "done"} AND elapsed(started_at) >= MIN_COMPLETION_SECONDS.

MIN_COMPLETION_SECONDS is fixed at 60s (floor). Env override
HERDR_MIN_COMPLETION_SECONDS may raise it, never lower it, so the
"no <60s accepted" machine invariant holds for every configuration.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

DONE_MARKER_PREFIX = "HERDR_TASK_DONE:"
BLOCKER_MARKER_PREFIX = "HERDR_TASK_BLOCKER:"
ORCH_MARKER_PREFIX = "HERDR_ORCH_TASK:"

# A pane is a hard-wrapped terminal screen, not a logical document.  A TUI
# message body keeps a left margin, so every soft-wrapped continuation line
# starts with horizontal whitespace.  Only those joins are undone; hard breaks
# (blank/whitespace-only lines, unindented lines) keep their newline so
# unrelated text can never be spliced into a marker.
_SOFT_WRAP_RE = re.compile(r"\r?\n[ \t]+(?=\S)")
MIN_COMPLETION_SECONDS = 60.0
# Sentinel polls once every three seconds.  A second marker sighting is only
# a confirmation when it comes from a later poll, not from a repeated read in
# the same sweep.
MIN_SAMPLE_INTERVAL_SECONDS = 3.0
MAX_OBSERVATION_AGE_SECONDS = 10.0
MIN_CONFIRMATION_INTERVAL_SECONDS = 3.0
REQUIRED_CONFIRMATIONS = 2
NEUTRAL_TOKEN = "HERDR_TASK_DONE:<TASK_ID>"


def min_completion_seconds() -> float:
    """Return the effective completion delay floor (>= 60s)."""
    import math

    raw = os.environ.get("HERDR_MIN_COMPLETION_SECONDS")
    if raw is None:
        return MIN_COMPLETION_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return MIN_COMPLETION_SECONDS
    if math.isnan(value):
        return MIN_COMPLETION_SECONDS
    return max(MIN_COMPLETION_SECONDS, value)


def observation_age_satisfied(last_observed_at: float | None, now: float) -> bool:
    """Return whether a ready observation is still fresh enough to consume."""
    if last_observed_at is None:
        return False
    try:
        age = float(now) - float(last_observed_at)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= MAX_OBSERVATION_AGE_SECONDS


def min_sample_interval_seconds() -> float:
    """Return the minimum interval between two stable marker samples."""
    import math

    raw = os.environ.get("HERDR_MIN_SAMPLE_INTERVAL_SECONDS")
    if raw is None:
        return MIN_CONFIRMATION_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return MIN_CONFIRMATION_INTERVAL_SECONDS
    if math.isnan(value):
        return MIN_CONFIRMATION_INTERVAL_SECONDS
    return max(MIN_CONFIRMATION_INTERVAL_SECONDS, value)


def sample_interval_satisfied(
    first_seen_at: float | None,
    last_sample_at: float | None,
) -> bool:
    """Return whether two counted samples span at least one Sentinel poll."""
    if first_seen_at is None or last_sample_at is None:
        return False
    try:
        return float(last_sample_at) - float(first_seen_at) >= min_sample_interval_seconds()
    except (TypeError, ValueError):
        return False


def marker_literal(task_id: str, prefix: str = DONE_MARKER_PREFIX) -> str:
    """Compose the exact marker string a pane has to display."""
    if not task_id:
        return ""
    return f"{prefix}{task_id}"


def _marker_pattern(task_id: str, prefix: str, *, wrapped=False):
    """Same task boundary and horizontal separator for detection and hygiene."""
    join = r"(?:\r?\n[ \t]+(?=\S))?" if wrapped else ""
    token = join.join(re.escape(char) for char in prefix)
    token += r"[ \t]*" + join
    token += join.join(re.escape(char) for char in task_id)
    return re.compile(token + f"(?!{join}[A-Za-z0-9_.-])")


def marker_present(
    screen: str,
    task_id: str,
    prefix: str = DONE_MARKER_PREFIX,
) -> bool:
    """Return whether a pane already displays ``prefix + task_id``.

    The single marker-detection choke point for both daemons.  A naive
    ``literal in screen`` read is not enough: terminal and TUI rendering hard
    wraps long tokens, so a 60-character marker no longer appears as one
    contiguous substring and the task can never leave ``working`` even though
    every deliverable is on disk.  Soft wraps are therefore undone before the
    search, while hard breaks and the identifier boundary keep the match
    fail-closed.
    """
    if not screen or not task_id:
        return False
    pattern = _marker_pattern(task_id, prefix)
    unwrapped = _SOFT_WRAP_RE.sub("", screen)
    return bool(pattern.search(unwrapped))


def sanitize_completion_marker(text: str, task_id: str) -> tuple[str, int]:
    """Neutralize this task's DONE and BLOCKER literals in Agent input.

    Input echo must not impersonate a terminal signal emitted by the Agent.
    Returns (cleaned_text, replacement_count). Empty task_id is a no-op.
    """
    if not text or not task_id:
        return text, 0
    count = 0
    for prefix in (DONE_MARKER_PREFIX, BLOCKER_MARKER_PREFIX):
        text, replaced = _marker_pattern(task_id, prefix, wrapped=True).subn(
            lambda match: f"{prefix}<TASK_ID>", text)
        count += replaced
    return text, count


def sanitize_prompt(prompt: str, task_id: str) -> tuple[str, int]:
    """Choke-point sanitizer for every prompt sink (T3b).

    All upstream sources (acceptance_criteria, goal, context, note
    summaries, re-dispatch, steering) converge on the final prompt string
    actually delivered via ``herdr agent prompt`` / ``pane send-text``.
    Sanitizing here covers every sink even if a future caller adds a new
    upstream source.
    """
    return sanitize_completion_marker(prompt, task_id)


def is_new_appearance(marker_present: bool, was_present: bool) -> bool:
    """True only on the absent -> present transition."""
    return bool(marker_present) and not bool(was_present)


def is_stale_epoch(epoch_updated_at: float | None, current_updated_at: float) -> bool:
    """True when the task was rewritten after the marker epoch (B-1b).

    Human ``blocked -> working`` re-open bumps ``updated_at``. A marker
    observed before that rewrite must never count as evidence for the new
    working episode, even though the status value is still ``working``.
    """
    if epoch_updated_at is None:
        return False
    try:
        return float(current_updated_at) != float(epoch_updated_at)
    except (TypeError, ValueError):
        return True


def observation_is_current(
    observation: dict | None,
    *,
    authoritative_status: str | None,
    authoritative_version: int | None,
) -> bool:
    """Return whether a durable Sentinel sample can still win its CAS.

    ``herdr-task set-status``, a human reopen, or another Controller write all
    bump the task version.  A sample recorded before that write can never be
    accepted again, so the owner must wait for a fresh sample instead of
    retrying a compare-and-set that is unwinnable by construction.

    This is a pre-check, not the authoritative gate: the atomic completion
    path still validates inside its own transaction.  It exists for the paths
    that read an observation outside a transaction and would otherwise
    re-attempt a hopeless transition on every sweep.
    """
    if not isinstance(observation, dict):
        return False
    observed_status = observation.get("observed_status")
    if observed_status is not None and observed_status != authoritative_status:
        return False
    observed_version = observation.get("observed_version")
    if observed_version is None or authoritative_version is None:
        # Nothing to bind the sample to; let the authoritative CAS decide.
        return True
    try:
        return int(observed_version) == int(authoritative_version)
    except (TypeError, ValueError):
        return False


def should_accept(
    *,
    marker_present: bool,
    agent_status: str | None,
    elapsed_seconds: float,
    is_new_or_tracked: bool = True,
    stale_epoch: bool = False,
) -> bool:
    """Pure triple condition for completion acceptance."""
    if stale_epoch:
        return False
    if not marker_present:
        return False
    if not is_new_or_tracked:
        # Marker was already on screen at dispatch/first sight:
        # pane-reuse residue, never a fresh completion signal.
        return False
    if agent_status not in {"idle", "done"}:
        return False
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError):
        return False
    return elapsed >= min_completion_seconds()


def classify_signal(marker_present: bool, agent_status: str | None) -> str:
    """Classify a visible marker for FR-1.2 observability.

    Returns ``early`` (marker present, agent provably busy),
    ``unknown`` (marker present but agent state unreadable),
    or ``none`` (no marker or agent idle/done).
    Only ``early`` may increment early_done_signal; ``unknown`` must be
    recorded separately and never counted (A-3).
    """
    if not marker_present:
        return "none"
    if agent_status is None:
        return "unknown"
    if agent_status in {"idle", "done"}:
        return "none"
    return "early"


def cas_allows(
    *,
    authoritative_status: str | None,
    expected_status: str | None,
    epoch_updated_at: float | None,
    current_updated_at: float | None,
) -> bool:
    """Version-aware compare-and-set guard for sentinel writes.

    Both the status value AND the task epoch must match the observation.
    A human re-open that restores the same status value (blocked -> working)
    still bumps ``updated_at``, so the stale marker epoch fails closed.
    """
    if expected_status is not None and authoritative_status != expected_status:
        return False
    if epoch_updated_at is not None and current_updated_at is not None:
        try:
            if float(epoch_updated_at) != float(current_updated_at):
                return False
        except (TypeError, ValueError):
            return False
    return True


def describe_decision(
    *,
    marker_present: bool,
    marker_was_present: bool,
    agent_status: str | None,
    elapsed_seconds: float,
    epoch_updated_at: float | None,
    current_updated_at: float | None,
) -> dict:
    """One-call decision helper used by the sentinel thin wrapper.

    Returns a dict with ``action`` in
    {accepted, early, unknown, uncertain_vanished, stale, debounce}
    plus machine-readable reasons. No I/O.
    """
    if marker_was_present and not marker_present:
        return {"action": "uncertain_vanished", "reason": "marker_vanished"}
    stale = is_stale_epoch(epoch_updated_at, current_updated_at or 0)
    if marker_present and stale:
        return {"action": "stale", "reason": "epoch_moved_human_reopen"}
    signal = classify_signal(marker_present, agent_status)
    if signal == "early":
        return {"action": "early", "reason": "marker_present_agent_busy"}
    if signal == "unknown":
        return {"action": "unknown", "reason": "agent_status_unreadable"}
    if should_accept(
        marker_present=marker_present,
        agent_status=agent_status,
        elapsed_seconds=elapsed_seconds,
        is_new_or_tracked=True,
        stale_epoch=stale,
    ):
        # New-appearance is tracked by the caller via first_seen_at;
        # a marker present since dispatch is passed as not-tracked there.
        return {"action": "accepted", "reason": "triple_condition_met"}
    if marker_present:
        return {"action": "debounce", "reason": "elapsed_below_floor_or_not_idle"}
    return {"action": "debounce", "reason": "no_marker"}


def stable_confirmation(
    samples: Sequence[dict],
    *,
    agent_status: str | None,
    elapsed_seconds: float,
    min_confirmations: int = REQUIRED_CONFIRMATIONS,
) -> bool:
    """Pure two-round gate used by both the store and service adapters.

    Samples must be in observation order and each must contain a true marker.
    A missing sample, an unknown agent state, or a short elapsed duration keeps
    the decision closed.  The function deliberately does not inspect task
    identity; callers bind samples to one task/version epoch before invoking it.
    """
    if agent_status not in {"idle", "done"}:
        return False
    try:
        if float(elapsed_seconds) < min_completion_seconds():
            return False
    except (TypeError, ValueError):
        return False
    required = max(REQUIRED_CONFIRMATIONS, int(min_confirmations or 0))
    if len(samples) < required:
        return False
    recent = list(samples[-required:])
    if not all(bool(sample.get("marker_present")) for sample in recent):
        return False
    if sample_interval_satisfied(
        recent[0].get("first_seen_at", recent[0].get("observed_at")),
        recent[-1].get("last_sample_at", recent[-1].get("observed_at")),
    ) is False and all(
        "first_seen_at" in sample or "last_sample_at" in sample
        for sample in recent
    ):
        return False
    epochs = {
        sample.get("observed_version")
        for sample in recent
        if sample.get("observed_version") is not None
    }
    return len(epochs) <= 1


def observation_ready(
    observation: dict | None,
    *,
    task_status: str | None,
    elapsed_seconds: float,
    agent_status: str | None = None,
) -> bool:
    """Evaluate a durable observation without performing any I/O."""
    if not isinstance(observation, dict):
        return False
    status = agent_status or observation.get("agent_status")
    return should_accept(
        marker_present=bool(observation.get("marker_present")),
        agent_status=status,
        elapsed_seconds=elapsed_seconds,
        is_new_or_tracked=(
            observation.get("first_seen_at") is not None
            and int(observation.get("consecutive_samples") or 0) >= REQUIRED_CONFIRMATIONS
            and sample_interval_satisfied(
                observation.get("first_seen_at"),
                observation.get("last_sample_at"),
            )
            and not observation.get("vanished")
        ),
        stale_epoch=bool(observation.get("epoch_changed")),
    ) and task_status in {"dispatched", "working"}


# Shared execution gates for terminal samples and structured declarations.
def latest_verification_dispatch(task, store=None):
    if store is None:
        raise ValueError("StateStore is required")
    from herdr.trajectory import run_id_for_task
    rows = store.list_events(
        task_id=task.get("task_id"), event_type="verification_dispatched",
        source="supervisor", limit=100, desc=True,
    )
    expected_run = run_id_for_task(task)
    for event in rows:
        if event.get("run_id") == expected_run:
            return event
    return None


def latest_retry_dispatch(task, store=None):
    if store is None:
        raise ValueError("StateStore is required")
    from herdr.trajectory import run_id_for_task
    expected_run = run_id_for_task(task)
    for event in store.list_events(
        task_id=task.get("task_id"), event_type="retry_dispatched",
        source="supervisor", limit=100, desc=True,
    ):
        if event.get("run_id") == expected_run:
            return event
    return None


def retry_execution_complete_for_rework(task, store=None):
    """Old deliverables cannot heal a task while a new RETRY lacks dispatch."""
    if store is None:
        raise ValueError("StateStore is required")
    try:
        from herdr.trajectory import run_id_for_task
        rows = store.list_interventions(
            run_id=run_id_for_task(task), task_id=task.get("task_id"), limit=1001,
        )
        if len(rows) > 1000:
            return False
        active = [
            row for row in rows
            if row.get("action") == "RETRY" and row.get("status") != "superseded"
        ]
        latest_agent_done = max(
            (
                float(entry.get("timestamp") or 0)
                for entry in (task.get("status_history") or [])
                if isinstance(entry, dict) and entry.get("to") == "agent_done"
            ),
            default=0.0,
        )
        active = [
            row for row in active
            if float(row.get("requested_at") or 0) >= latest_agent_done
        ]
        if not active:
            return True
        latest = max(active, key=lambda row: float(row.get("requested_at") or 0))
        if latest.get("status") in ("requested", "running", "failed"):
            dispatch = latest_retry_dispatch(task, store)
            return bool(
                dispatch
                and (dispatch.get("payload") or {}).get("intervention_id")
                == latest.get("intervention_id")
            )
        return True
    except Exception:
        return False


def verification_execution_complete_for_rework(task, store=None):
    """Old deliverables cannot heal a Task while a new VERIFY lacks evidence."""
    if store is None:
        raise ValueError("StateStore is required")
    try:
        from herdr.trajectory import run_id_for_task
        verify_rows = store.list_interventions(
            run_id=run_id_for_task(task),
            task_id=task.get("task_id"), limit=1001,
        )
        if len(verify_rows) > 1000:
            return False
        active_verify = [
            row for row in verify_rows
            if row.get("action") == "VERIFY"
            and row.get("status") != "superseded"
        ]
        latest_agent_done = max(
            (
                float(entry.get("timestamp") or 0)
                for entry in (task.get("status_history") or [])
                if isinstance(entry, dict) and entry.get("to") == "agent_done"
            ),
            default=0.0,
        )
        active_verify = [
            row for row in active_verify
            if float(row.get("requested_at") or 0) >= latest_agent_done
        ]
        if not active_verify:
            return True
        latest_verify = max(
            active_verify,
            key=lambda row: float(row.get("requested_at") or 0),
        ) if active_verify else None
        dispatch = latest_verification_dispatch(task, store)
        dispatch_intervention_id = (
            (dispatch.get("payload") or {}).get("intervention_id")
            if dispatch is not None else None
        )
        if latest_verify and latest_verify.get("status") in ("requested", "running", "failed"):
            if dispatch_intervention_id != latest_verify.get("intervention_id"):
                return False
        if dispatch is None:
            latest = max(
                active_verify,
                key=lambda row: float(row.get("requested_at") or 0),
            )
            return latest.get("status") not in ("requested", "running", "failed")
        intervention_id = (dispatch.get("payload") or {}).get("intervention_id")
        for event in store.list_events(
            task_id=task.get("task_id"), event_type="verification_completed",
            source="trajectory", limit=100, desc=True,
        ):
            if event.get("run_id") != run_id_for_task(task):
                continue
            payload = event.get("payload") or {}
            verification = payload.get("verification") or {}
            if (
                verification.get("intervention_id") == intervention_id
                and float(event.get("timestamp") or 0) >= float(dispatch.get("timestamp") or 0)
            ):
                return True
        return False
    except Exception:
        return False
