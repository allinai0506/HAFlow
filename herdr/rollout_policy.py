#!/usr/bin/env python3
"""Adaptive Router Controlled Rollout v1 (herdr/rollout_policy.py).

Functional Core + thin persistence orchestration for per-bucket traffic
expansion. A bucket is ``recommended_agent x node x task_type`` and owns
an independent staged percentage. This module never scores agents, never
hashes identities, and never aggregates outcomes: ranking stays in
``adaptive_router``, the deterministic split stays in ``canary_router``
(``sha256("canary-v2|{run_id}|{task_id}") mod 100``), and arm metrics
stay in ``canary_evaluation``. Routers only consume
``effective_percentage`` from here.

Safety contract (Promotion is manual. Rollback can be automatic. Safety
always wins.):

- Stages are a closed enum: off(0)/5/10/25/50. No 75/100, no open ints.
- Promotion is adjacent-only and manual: off->5->10->25->50, each with
  an explicit non-empty reason. 5->50 is refused.
- Emergency rollback to off is allowed from any stage.
- No auto-promotion path exists in this module.
- Fail-safe: kill switch, invalid bucket, corrupt state, guard error,
  evaluation/DB outage all resolve to "do not expand" (effective 0 or
  the conservative fallback), never to more Adaptive traffic.
- Every state change appends exactly one immutable audit row in the
  same SQLite transaction as the state upsert, and both the change and
  the no-op decision are re-verified against the exact snapshot they
  were computed from (see ``state_db.transact_rollout_stage``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import state_db

ALGORITHM_VERSION = "adaptive-router-rollout-v1"

#: Closed stage enum. ``0`` renders as ``off``. 75/100 are deliberately
#: absent: Controlled Rollout never becomes the production default.
ALLOWED_PERCENTAGES = (0, 5, 10, 25, 50)

#: Adjacent-only promotion ladder. Rollback to off bypasses adjacency.
_ALLOWED_NEXT = {0: (5,), 5: (10,), 10: (25,), 25: (50,), 50: ()}

ROLLOUT_ENABLED_ENV_VAR = "HERDR_ADAPTIVE_ROLLOUT_ENABLED"
GUARD_ENABLED_ENV_VAR = "HERDR_ROLLOUT_GUARD_ENABLED"
#: Opt-in per-dispatch guard. The guard reads the #103 canary evaluation
#: (a bounded decision scan), which is too expensive to run on every
#: dispatch by default, so the routing hot path leaves it off and the
#: explicit ``rollout check-guard`` path owns automatic stop. Enabling
#: it trades dispatch latency for immediate stop-on-regression.
HOT_GUARD_ENV_VAR = "HERDR_ROLLOUT_HOT_GUARD"

#: Bounded guard read budget: matched canary decisions and the scan
#: budget that finds them. Keeps the guard off unbounded scans even
#: with a long route_decision history.
GUARD_DECISION_LIMIT = 200
GUARD_SCAN_CAP = 400

#: Guard defaults, centrally configured and documented. Small counts,
#: each with a reason so thresholds are never mystery numbers.
#: - MIN_SETTLED_SAMPLES (20): proportions on fewer settled executions
#:   swing wildly (one retry flips 25%); below this the guard stays
#:   quiet instead of misjudging on noise.
#: - SUCCESS_DROP_TOLERANCE (0.20): adaptive may trail legacy by noise
#:   on small canaries; a 20-point qualified-success gap is the smallest
#:   difference worth an automatic stop.
#: - BLOCKED_TOLERANCE (0.30) / HUMAN_TOLERANCE (0.30): blocked and
#:   human-intervention means are bursty; only a large excess stops.
#: - MIN_ARM_SAMPLES (8): each arm needs a minimal voice before any
#:   rate comparison; otherwise a 1-vs-1 anecdote could roll back.
MIN_SETTLED_SAMPLES = 20
MIN_ARM_SAMPLES = 8
SUCCESS_DROP_TOLERANCE = 0.20
BLOCKED_TOLERANCE = 0.30
HUMAN_TOLERANCE = 0.30


@dataclass(frozen=True)
class GuardConfig:
    """Tunable safety-guard thresholds (defaults are the contract)."""

    enabled: bool = True
    min_settled_samples: int = MIN_SETTLED_SAMPLES
    min_arm_samples: int = MIN_ARM_SAMPLES
    success_drop_tolerance: float = SUCCESS_DROP_TOLERANCE
    blocked_tolerance: float = BLOCKED_TOLERANCE
    human_tolerance: float = HUMAN_TOLERANCE


def rollout_enabled() -> bool:
    """Master kill switch. Explicit false stops all Adaptive diversion.

    Unset defaults to enabled so staged rows take effect without extra
    env; production stays safe because empty rollout state plus absent
    canary config still resolves to 0 (no diversion). Any read error
    elsewhere resolves conservatively too.
    """
    raw = os.environ.get(ROLLOUT_ENABLED_ENV_VAR)
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def guard_enabled() -> bool:
    """Guard master switch. Explicit 0/false disables auto-rollback."""
    raw = os.environ.get(GUARD_ENABLED_ENV_VAR)
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def hot_guard_enabled() -> bool:
    """Opt-in per-dispatch guard check (default off: see HOT_GUARD_ENV_VAR)."""
    raw = os.environ.get(HOT_GUARD_ENV_VAR)
    if raw is None:
        return False
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def normalize_bucket(agent: Any, node: Any, task_type: Any) -> Dict[str, str]:
    """Validate bucket identity; raise ValueError on anything invalid."""
    clean_agent = str(agent or "").strip()
    clean_node = str(node or "").strip()
    if not clean_agent:
        raise ValueError("agent must be a non-empty string")
    if not clean_node:
        raise ValueError("node must be a non-empty string")
    try:
        from .adaptive_router import normalize_task_type
    except ImportError:  # pragma: no cover - script-style import fallback
        from herdr.adaptive_router import normalize_task_type  # type: ignore
    clean_type = normalize_task_type(task_type)
    return {"agent": clean_agent, "node": clean_node, "task_type": clean_type}


def normalize_percentage(value: Any) -> int:
    """Map off/0/5/10/25/50 to int; reject every open percentage."""
    if value is None:
        return 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("off", "0", "none", "disabled"):
            return 0
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(
                f"percentage must be one of off/5/10/25/50, "
                f"got {value!r}") from exc
    if isinstance(value, bool):
        raise ValueError(
            f"percentage must be one of off/5/10/25/50, got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"percentage must be one of off/5/10/25/50, got {value!r}") from exc
    if number not in ALLOWED_PERCENTAGES:
        raise ValueError(
            f"percentage must be one of off/5/10/25/50, got {value!r}")
    return number


def is_valid_transition(previous: int, new: int) -> bool:
    """Adjacent promote or any rollback to off; nothing else."""
    prev = normalize_percentage(previous)
    nxt = normalize_percentage(new)
    if nxt == 0:
        return True
    return nxt in _ALLOWED_NEXT.get(prev, ())


def _safe_fallback(value: Any) -> int:
    """Normalize a canary config percentage to a servable int (0 if unusable).

    The canary config accepts any 1..100 integer, so this deliberately
    does not use ``normalize_percentage`` (that is the closed rollout
    enum). An unusable or out-of-range value serves 0: never guess a
    traffic number.
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = int(str(value).strip() if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return 0
    return number if 0 <= number <= 100 else 0


def _action_for(previous: int, new: int, *, automatic: bool) -> str:
    """Label a transition by the direction of the *traffic* change.

    ``previous`` MUST be the percentage that was actually being
    diverted, not the staged value. The two differ when a bucket is
    migrated off the canary config fallback for the first time, and
    labeling that by the staged value would write a self-contradictory
    immutable fact (``previous_percentage=50, new_percentage=5,
    action="promote"``).
    """
    if automatic:
        return "auto_rollback"
    return "promote" if new > previous else "rollback"


def get_stage(
    db_path: Optional[Path],
    agent: str,
    node: str,
    task_type: str,
) -> int:
    """Current staged percentage; unknown buckets read as off (0).

    Invalid bucket identity is a caller error (raise); storage errors
    resolve to 0 so callers fail safe without extra branches. Anything
    that decides whether to write must use the full snapshot
    (``state_db.read_rollout_snapshot``) instead.
    """
    bucket = normalize_bucket(agent, node, task_type)
    try:
        return int(state_db.get_rollout_stage(
            bucket["agent"], bucket["node"], bucket["task_type"],
            db_path=db_path))
    except Exception:
        return 0


def effective_percentage(
    db_path: Optional[Path],
    *,
    agent: str,
    node: str,
    task_type: str,
    config_fallback: Optional[int] = None,
) -> int:
    """Resolve the percentage the router must enforce for one bucket.

    Order: kill switch (0) -> staged row -> canary config fallback ->
    0. Invalid identity, storage errors, and unknown stages all yield
    0. This function never raises for routing inputs: rollout control
    failure must never make production routing more aggressive.
    """
    try:
        if not rollout_enabled():
            return 0
        bucket = normalize_bucket(agent, node, task_type)
    except ValueError:
        return 0
    try:
        staged = state_db.get_rollout_stage(
            bucket["agent"], bucket["node"], bucket["task_type"],
            db_path=db_path)
    except Exception:
        return 0
    try:
        staged_int = normalize_percentage(staged)
    except ValueError:
        return 0
    # A staged row (including explicit off) is authoritative once the
    # operator migrated the bucket; absent rows preserve #103 behavior.
    try:
        known = state_db.rollout_stage_known(
            bucket["agent"], bucket["node"], bucket["task_type"],
            db_path=db_path)
    except Exception:
        return 0
    if known:
        return staged_int
    if config_fallback is None:
        return 0
    try:
        fallback = config_fallback
        if isinstance(fallback, bool):
            raise ValueError("invalid fallback")
        # Canary config percentages are 1..100 (plus 0/off for rollout);
        # any out-of-range fallback fails safe to 0, never expands.
        number = int(str(fallback).strip() if isinstance(fallback, str) else fallback)
        if not 0 <= number <= 100:
            return 0
        return number
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class RolloutDecision:
    """Pure verdict for one requested change against a given snapshot."""

    snapshot: state_db.RolloutSnapshot
    #: Staged ladder position (0 when absent or corrupt).
    staged: int
    #: Percentage actually being diverted right now.
    effective_prev: int
    new_percentage: int
    is_noop: bool
    action: str


def decide_rollout_change(
    snapshot: state_db.RolloutSnapshot,
    *,
    new_percentage: Any,
    automatic: bool = False,
    config_fallback: Optional[int] = None,
) -> RolloutDecision:
    """Decide what one requested change means (pure, no I/O).

    Everything that can reject a change happens here, before any write:
    the transition ladder, the no-op question, and the audit action. The
    caller passes the returned decision to a single transaction that
    re-verifies ``snapshot``, so a decision can never be applied to a
    state it was not made against.
    """
    nxt = normalize_percentage(new_percentage)
    try:
        staged = normalize_percentage(snapshot.percentage)
    except ValueError as exc:
        # Corrupt staged value: only a rollback to off may proceed.
        if nxt != 0:
            raise ValueError(
                "corrupt rollout state; only rollback to off is allowed"
            ) from exc
        staged = 0
        snapshot = state_db.RolloutSnapshot(exists=False, percentage=0)
    # What is actually being diverted right now: the staged row when one
    # exists (explicit off included), otherwise the canary config.
    effective_prev = (
        staged if snapshot.exists else _safe_fallback(config_fallback))
    if nxt == effective_prev:
        return RolloutDecision(
            snapshot=snapshot, staged=staged, effective_prev=effective_prev,
            new_percentage=nxt, is_noop=True, action="noop")
    if not is_valid_transition(staged, nxt):
        raise ValueError(
            f"rollout transition {staged} -> {nxt} is not allowed: "
            "promote one stage at a time (off->5->10->25->50); "
            "rollback may go directly to off")
    return RolloutDecision(
        snapshot=snapshot, staged=staged, effective_prev=effective_prev,
        new_percentage=nxt, is_noop=False,
        # The ladder is validated against the staged value (a first
        # migration still starts at stage 5), but the audit action must
        # describe the real traffic direction: taking a 50% config bucket
        # down to stage 5 is a rollback, not a promotion.
        action=_action_for(effective_prev, nxt, automatic=automatic))


def set_stage(
    db_path: Optional[Path],
    *,
    agent: str,
    node: str,
    task_type: str,
    new_percentage: Any,
    reason: str,
    source: str = "cli",
    automatic: bool = False,
    config_fallback: Optional[int] = None,
    created_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Manually promote or roll back one bucket (atomic + audited).

    ``config_fallback`` is the percentage the canary config would serve
    for this bucket while it has no staged row. It only informs the
    no-op decision and the audit fact; the transition ladder is always
    evaluated against the staged value.

    **Absent is not explicit off.** A bucket with no staged row still
    runs at the canary config percentage, so ``rollout off`` on such a
    bucket must write an explicit ``percentage=0`` row to suppress that
    fallback; treating it as a no-op would report success while Adaptive
    traffic kept flowing.

    Read, decide, and write are one verified cycle: the snapshot this
    decision was made against is re-checked inside the write
    transaction, presence included, so a concurrent emergency rollback
    can never be silently overwritten by a promotion that read "absent"
    and a no-op can never be reported from a stale read.

    Raises ValueError on invalid bucket/stage/transition/empty reason,
    and on a concurrent-writer conflict. Storage errors propagate so the
    CLI can report failure; no partial state is ever visible.
    """
    bucket = normalize_bucket(agent, node, task_type)
    normalize_percentage(new_percentage)  # reject before any I/O
    if not str(reason or "").strip():
        raise ValueError("reason is required for every rollout change")
    if not str(source or "").strip():
        raise ValueError("source is required for every rollout change")
    now = float(created_at) if created_at is not None else time.time()
    snapshot = state_db.read_rollout_snapshot(
        bucket["agent"], bucket["node"], bucket["task_type"], db_path=db_path)
    decision = decide_rollout_change(
        snapshot, new_percentage=new_percentage, automatic=automatic,
        config_fallback=config_fallback)
    write = None
    if not decision.is_noop:
        write = state_db.RolloutWrite(
            new_percentage=decision.new_percentage,
            previous_percentage=decision.effective_prev,
            action=decision.action, reason=str(reason).strip(),
            source=str(source).strip(), algorithm_version=ALGORITHM_VERSION,
            created_at=now)
    record = state_db.transact_rollout_stage(
        agent=bucket["agent"], node=bucket["node"],
        task_type=bucket["task_type"], expected=decision.snapshot,
        write=write, db_path=db_path)
    if record is None:
        # Verified inside the transaction: nothing to change right now.
        return {
            "bucket_key": state_db.rollout_bucket_key(
                bucket["agent"], bucket["node"], bucket["task_type"]),
            "recommended_agent": bucket["agent"],
            "node": bucket["node"],
            "task_type": bucket["task_type"],
            "previous_percentage": decision.effective_prev,
            "new_percentage": decision.new_percentage,
            "action": "noop",
            "changed": False,
            "reason": str(reason).strip(),
            "source": str(source).strip(),
            "algorithm_version": ALGORITHM_VERSION,
            "created_at": now,
        }
    record["changed"] = True
    return record


def get_history(
    db_path: Optional[Path],
    *,
    agent: Optional[str] = None,
    node: Optional[str] = None,
    task_type: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Immutable audit history, newest-first (strictly read-only).

    A database without the rollout tables reads as an empty history; any
    other failure (locked, corrupt, unreadable) propagates so the caller
    reports "unavailable" instead of "no audit events".
    """
    try:
        return state_db.list_rollout_audit(
            agent=agent, node=node, task_type=task_type, limit=limit,
            db_path=db_path, readonly=True)
    except state_db.ReadonlySchemaError:
        return []


def list_states(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Current staged percentages for buckets with explicit rows.

    Strictly read-only and honest: a pre-rollout database reports no
    staged bucket, but a failure to read is raised rather than
    flattened into an empty list — reporting "all off" when the truth
    is "unknown" is a false safety signal.
    """
    try:
        return state_db.list_rollout_states(db_path=db_path, readonly=True)
    except state_db.ReadonlySchemaError:
        return []


def _bucket_report(
    db_path: Optional[Path],
    *,
    agent: str,
    node: str,
    task_type: str,
) -> Optional[Dict[str, Any]]:
    """Fetch this bucket's canary evaluation report (read-only reuse).

    The read is scoped to the exact bucket with the dedicated
    ``recommended_agent`` filter, which is applied before the
    matched-row budget. Filtering by ``agent`` instead would be both too
    wide (it also matches actual/legacy agent) and too narrow in the
    worst way: ``node``/``task_type`` alone let a busier sibling
    recommendation consume the whole budget, so this bucket would read
    as "no samples" and the guard would stay quiet while the bucket was
    actually failing.
    """
    from . import canary_evaluation
    bundle = canary_evaluation.run_canary_evaluation(
        db_path,
        filters=canary_evaluation.CanaryEvaluationFilters(
            node=node, task_type=task_type, agent=None,
            recommended_agent=agent,
            limit=GUARD_DECISION_LIMIT, scan_cap=GUARD_SCAN_CAP),
    )
    for bucket in bundle["report"].get("buckets") or []:
        if (str(bucket.get("recommended_agent")) == agent
                and str(bucket.get("node")) == node
                and str(bucket.get("task_type") or "") == task_type):
            return bucket
    return None


def evaluate_guard(
    db_path: Optional[Path],
    *,
    agent: str,
    node: str,
    task_type: str,
    config: Optional[GuardConfig] = None,
) -> Dict[str, Any]:
    """Evaluate the safety guard against #103 canary facts (read-only).

    Consumes only ``canary_evaluation`` arm metrics: qualified success,
    wall-time-adjacent rework/blocked/human means, and sample counts.
    Never promotes.

    ``status`` distinguishes "evaluated and decided" from "could not
    evaluate", because the two demand opposite routing behavior:

    ==========================  =========  ===============================
    status                      triggered   hot path routes
    ==========================  =========  ===============================
    ``triggered``               True        Legacy
    ``within_tolerance``        False       Adaptive
    ``insufficient_samples``    False       Adaptive (not enough data)
    ``unavailable``             False       **Legacy** (cannot judge)
    ``disabled``                False       Adaptive (operator opted out)
    ==========================  =========  ===============================

    Sample starvation is a decision, an outage is not: ``unavailable``
    must never read as "no problem".
    """
    cfg = config or GuardConfig()
    base: Dict[str, Any] = {
        "triggered": False,
        "status": "unavailable",
        "reason": "",
        "bucket": None,
        "config": {
            "enabled": cfg.enabled,
            "min_settled_samples": cfg.min_settled_samples,
            "min_arm_samples": cfg.min_arm_samples,
            "success_drop_tolerance": cfg.success_drop_tolerance,
            "blocked_tolerance": cfg.blocked_tolerance,
            "human_tolerance": cfg.human_tolerance,
        },
    }
    if not cfg.enabled or not guard_enabled():
        base["status"] = "disabled"
        base["reason"] = "guard disabled"
        return base
    try:
        bucket = normalize_bucket(agent, node, task_type)
    except ValueError as exc:
        base["reason"] = f"invalid bucket: {exc}"
        return base
    try:
        report = _bucket_report(
            db_path, agent=bucket["agent"], node=bucket["node"],
            task_type=bucket["task_type"])
    except Exception as exc:
        base["reason"] = (
            f"canary evaluation unavailable: {type(exc).__name__}: {exc}")
        return base
    if report is None:
        base["status"] = "insufficient_samples"
        base["reason"] = "insufficient samples: no settled bucket yet"
        return base
    base["bucket"] = report
    adaptive = report.get("adaptive_arm") or {}
    legacy = report.get("legacy_arm") or {}
    adaptive_n = int(adaptive.get("sample_count") or 0)
    legacy_n = int(legacy.get("sample_count") or 0)
    total = adaptive_n + legacy_n
    if total < cfg.min_settled_samples:
        base["status"] = "insufficient_samples"
        base["reason"] = (
            f"insufficient samples: settled={total} "
            f"< min={cfg.min_settled_samples}")
        return base
    if adaptive_n < cfg.min_arm_samples or legacy_n < cfg.min_arm_samples:
        base["status"] = "insufficient_samples"
        base["reason"] = (
            f"insufficient arm samples: adaptive={adaptive_n} "
            f"legacy={legacy_n} < min_arm={cfg.min_arm_samples}")
        return base
    triggers: List[str] = []
    a_rate = adaptive.get("qualified_success_rate")
    l_rate = legacy.get("qualified_success_rate")
    if a_rate is not None and l_rate is not None:
        if float(a_rate) < float(l_rate) - cfg.success_drop_tolerance:
            triggers.append(
                f"qualified_success dropped: adaptive={a_rate} "
                f"legacy={l_rate}")
    a_blocked = adaptive.get("mean_blocked_count")
    l_blocked = legacy.get("mean_blocked_count")
    if a_blocked is not None and l_blocked is not None:
        if float(a_blocked) > float(l_blocked) + cfg.blocked_tolerance:
            triggers.append(
                f"blocked elevated: adaptive={a_blocked} legacy={l_blocked}")
    a_human = adaptive.get("mean_human_intervention_count")
    l_human = legacy.get("mean_human_intervention_count")
    if a_human is not None and l_human is not None:
        if float(a_human) > float(l_human) + cfg.human_tolerance:
            triggers.append(
                f"human_intervention elevated: adaptive={a_human} "
                f"legacy={l_human}")
    if triggers:
        base["status"] = "triggered"
        base["triggered"] = True
        base["reason"] = "; ".join(triggers)
    else:
        base["status"] = "within_tolerance"
        base["reason"] = "within tolerance"
    return base


def should_force_legacy(
    db_path: Optional[Path],
    *,
    agent: str,
    node: str,
    task_type: str,
    config: Optional[GuardConfig] = None,
) -> bool:
    """Hot-path guard check: True means route Legacy for this decision.

    Gated by ``HERDR_ROLLOUT_HOT_GUARD`` (default off) because the guard
    reads the #103 canary evaluation and must not add an unbounded scan
    to every dispatch inside the router critical section.

    Once opted in, it is read-only and conservative in both directions:
    a triggered regression **and** an evaluation that could not run
    (DB unavailable, schema error, unexpected error) both force Legacy.
    "Cannot judge" is never treated as "no problem".
    """
    if not hot_guard_enabled():
        return False
    try:
        verdict = evaluate_guard(
            db_path, agent=agent, node=node, task_type=task_type,
            config=config)
    except Exception:
        return True
    if verdict.get("triggered"):
        return True
    return verdict.get("status") == "unavailable"


def maybe_auto_rollback(
    db_path: Optional[Path],
    *,
    agent: str,
    node: str,
    task_type: str,
    reason: str,
    source: str = "guard",
    config: Optional[GuardConfig] = None,
    config_fallback: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist an automatic rollback to off when the guard triggers.

    Returns the audit record on rollback, or ``{"action": "none"}`` when
    the guard is quiet or the bucket is already serving 0. Never
    promotes. Like the manual path, a bucket with no staged row is still
    running at the canary config percentage, so the rollback writes an
    explicit off row instead of reporting "already off".
    """
    verdict = evaluate_guard(
        db_path, agent=agent, node=node, task_type=task_type, config=config)
    if not verdict.get("triggered"):
        return {
            "action": "none",
            "status": verdict.get("status", "unavailable"),
            "reason": verdict.get("reason", ""),
        }
    return set_stage(
        db_path, agent=agent, node=node, task_type=task_type,
        new_percentage=0,
        reason=reason or verdict.get("reason", "safety guard triggered"),
        source=source, automatic=True, config_fallback=config_fallback)


__all__ = [
    "ALGORITHM_VERSION",
    "ALLOWED_PERCENTAGES",
    "GUARD_ENABLED_ENV_VAR",
    "ROLLOUT_ENABLED_ENV_VAR",
    "GuardConfig",
    "RolloutDecision",
    "decide_rollout_change",
    "effective_percentage",
    "evaluate_guard",
    "get_history",
    "get_stage",
    "guard_enabled",
    "is_valid_transition",
    "list_states",
    "maybe_auto_rollback",
    "normalize_bucket",
    "normalize_percentage",
    "rollout_enabled",
    "set_stage",
    "should_force_legacy",
]
