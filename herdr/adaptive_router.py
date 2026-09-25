#!/usr/bin/env python3
"""Adaptive Agent Router v1 -- Shadow Mode (herdr/adaptive_router.py).

Functional Core: deterministic, explainable shadow ranking of candidate
agents from historical (agent x node/stage x task_type) performance.
No LLM, no embeddings, no network, no randomness: identical inputs always
produce identical outputs.

Shadow First, Decision Later: this module never selects the production
agent. Callers (agent_router.choose_agent) keep their own decision and
persist this module's recommendation as a ``route_decision`` event only.

Definitions:
- Qualified Success: requirements_satisfied is True AND
  verification_passed is True AND final_status is in the completed family
  (herdr.transitions.COMPLETED_TASK_STATUSES). Rows without a complete
  eval fact are outcome-unknown and excluded from the denominator; they
  are never defaulted to success or failure.
- ETQS (Expected Time To Qualified Success):
    ETQS = queue_delay_est
         + expected_execution_time (p50 wall, observed; else fallback estimate)
         + (1 - blended_success_rate) * RECOVERY_PENALTY_SECONDS
         + rework_rate * REWORK_PENALTY_SECONDS
  verification_failure_rate, blocked_rate and human_intervention_rate are
  reported as observed facts but deliberately excluded from ETQS: blocked
  and failed-verification runs already depress qualified_success_rate, so
  adding them again would punish the same outcome twice.
- Cold start: blended_success_rate = (n*observed + K*prior) / (n + K)
  with K = MIN_SAMPLES_FOR_FULL_CONFIDENCE. Fewer samples -> the prior
  dominates -> ranking falls back toward the existing candidate order.
  Zero determined samples -> deterministic fallback to candidate order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import state_db
from .transitions import COMPLETED_TASK_STATUSES

ALGORITHM_VERSION = "adaptive-router-v1"

# Default parameters, centrally managed. Small count, each documented.
#: Samples needed for history to carry ~half weight (n/(n+K) = 0.5 at n=K).
MIN_SAMPLES_FOR_FULL_CONFIDENCE = 10
#: Conservative prior blended in when samples are scarce (Laplace-style).
PRIOR_QUALIFIED_SUCCESS_RATE = 0.5
#: Expected cost of one failed qualification (re-dispatch + redo).
RECOVERY_PENALTY_SECONDS = 600.0
#: Expected cost of one rework cycle.
REWORK_PENALTY_SECONDS = 300.0
#: Estimated queue seconds per unit of (active + reserved) load.
QUEUE_SECONDS_PER_LOAD = 30.0
#: Assumed execution time when an agent has no observed wall times here.
FALLBACK_EXECUTION_SECONDS = 600.0
#: Bounded history window per ranking (newest-first); see also lookback.
DEFAULT_HISTORY_LIMIT = 2000
#: Optional age floor for history; None disables time filtering.
DEFAULT_LOOKBACK_DAYS: Optional[float] = None


def normalize_task_type(task_type: Any) -> str:
    """Legacy tasks without task_type form their own ("") bucket; never guessed."""
    if task_type is None:
        return ""
    return str(task_type)


def qualified_success(sample: Dict[str, Any]) -> Optional[bool]:
    """True/False for determined outcomes, None for unknown (never defaulted)."""
    req = sample.get("requirements_satisfied")
    passed = sample.get("verification_passed")
    final = sample.get("final_status")
    if req is None or passed is None or final is None:
        return None
    return bool(req) and bool(passed) and str(final) in COMPLETED_TASK_STATUSES


def _percentile(sorted_vals: List[float], fraction: float) -> Optional[float]:
    """Deterministic nearest-rank percentile over an ascending-sorted list."""
    if not sorted_vals:
        return None
    rank = int(fraction * len(sorted_vals) + 0.999999999)
    rank = max(1, min(rank, len(sorted_vals)))
    return float(sorted_vals[rank - 1])


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def collect_samples(
    db_path: Optional[Path],
    *,
    node: str,
    task_type: str,
    cutoff: float,
    exclude_run_id: Optional[str] = None,
    limit: Optional[int] = None,
    lookback_days: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Fetch the bounded, cutoff-gated history slice for one bucket."""
    return state_db.query_adaptive_history(
        str(node),
        normalize_task_type(task_type),
        cutoff=float(cutoff),
        exclude_run_id=exclude_run_id,
        limit=int(limit) if limit is not None else DEFAULT_HISTORY_LIMIT,
        lookback_days=lookback_days if lookback_days is not None else DEFAULT_LOOKBACK_DAYS,
        db_path=db_path,
    )


def _score_agent(
    agent: str,
    rows: List[Dict[str, Any]],
    *,
    candidate_index: int,
    active_load: int = 0,
    reserved_load: int = 0,
) -> Dict[str, Any]:
    """Score one candidate from its bucket rows (pure function)."""
    verdicts = [(row, qualified_success(row)) for row in rows]
    determined = [row for row, verdict in verdicts if verdict is not None]
    qualified = [row for row, verdict in verdicts if verdict is True]
    n = len(determined)
    observed_rate = _rate(len(qualified), n)

    walls = sorted(
        float(row["wall_time_seconds"]) for row in determined
        if row.get("wall_time_seconds") is not None
    )
    p50 = _percentile(walls, 0.5)
    p90 = _percentile(walls, 0.9)

    history_rows = [row for row in rows if row.get("has_history")]
    rework = sum(1 for row in history_rows if "rework" in row["status_history"])
    blocked = sum(1 for row in history_rows if "blocked" in row["status_history"])
    rework_rate = _rate(rework, len(history_rows))
    blocked_rate = _rate(blocked, len(history_rows))

    verified_rows = [row for row in rows if row.get("verification_passed") is not None]
    verification_failure_rate = _rate(
        sum(1 for row in verified_rows if row["verification_passed"] is False),
        len(verified_rows),
    )
    human_rows = [row for row in rows if row.get("human_intervention_count") is not None]
    human_intervention_rate = _rate(
        sum(1 for row in human_rows if int(row["human_intervention_count"] or 0) > 0),
        len(human_rows),
    )

    confidence = n / (n + MIN_SAMPLES_FOR_FULL_CONFIDENCE)
    if observed_rate is None:
        blended = PRIOR_QUALIFIED_SUCCESS_RATE
    else:
        blended = (
            n * observed_rate
            + MIN_SAMPLES_FOR_FULL_CONFIDENCE * PRIOR_QUALIFIED_SUCCESS_RATE
        ) / (n + MIN_SAMPLES_FOR_FULL_CONFIDENCE)

    if p50 is not None:
        exec_time, exec_source = p50, "observed"
    else:
        exec_time, exec_source = FALLBACK_EXECUTION_SECONDS, "estimated"
    queue_delay = float(active_load + reserved_load) * QUEUE_SECONDS_PER_LOAD
    etqs = (
        queue_delay
        + exec_time
        + (1.0 - blended) * RECOVERY_PENALTY_SECONDS
        + (rework_rate or 0.0) * REWORK_PENALTY_SECONDS
    )

    if n > 0:
        fallback_reason = None
    elif rows:
        fallback_reason = "unknown_outcome_no_determined_samples"
    else:
        fallback_reason = "no_history"

    return {
        "agent": agent,
        "sample_count": n,
        "qualified_success_count": len(qualified),
        "qualified_success_rate": observed_rate,
        "qualified_success_rate_source": "observed" if n else None,
        "blended_success_rate": round(blended, 6),
        "confidence": round(confidence, 6),
        "p50_wall_time_seconds": p50,
        "p50_wall_time_source": "observed" if p50 is not None else "estimated",
        "p90_wall_time_seconds": p90,
        "p90_wall_time_source": "observed" if p90 is not None else "estimated",
        "rework_rate": rework_rate,
        "blocked_rate": blocked_rate,
        "verification_failure_rate": verification_failure_rate,
        "human_intervention_rate": human_intervention_rate,
        "queue_delay_seconds": queue_delay,
        "queue_delay_source": "estimated",
        "active_load": int(active_load),
        "reserved_load": int(reserved_load),
        "etqs_seconds": round(etqs, 3),
        "etqs_source": "estimated",
        "fallback_reason": fallback_reason,
        "candidate_index": candidate_index,
    }


def score_candidates(
    candidates: List[str],
    samples: List[Dict[str, Any]],
    *,
    active_loads: Optional[Dict[str, int]] = None,
    reserved_loads: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Score every candidate (unsorted). Deterministic, no I/O."""
    active_loads = active_loads or {}
    reserved_loads = reserved_loads or {}
    by_agent: Dict[str, List[Dict[str, Any]]] = {}
    for row in samples:
        by_agent.setdefault(str(row["agent"]), []).append(row)
    scored = []
    for index, agent in enumerate(candidates):
        scored.append(_score_agent(
            str(agent),
            by_agent.get(str(agent), []),
            candidate_index=index,
            active_load=int(active_loads.get(str(agent), 0) or 0),
            reserved_load=int(reserved_loads.get(str(agent), 0) or 0),
        ))
    return scored


def rank_candidates(
    candidates: List[str],
    *,
    db_path: Optional[Path],
    node: str,
    task_type: str,
    cutoff: float,
    exclude_run_id: Optional[str] = None,
    active_loads: Optional[Dict[str, int]] = None,
    reserved_loads: Optional[Dict[str, int]] = None,
    limit: Optional[int] = None,
    lookback_days: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Rank candidates by ETQS; ties keep the existing candidate order.

    History is strictly before ``cutoff`` and never includes
    ``exclude_run_id`` (no look-ahead from the run being routed now).
    """
    samples = collect_samples(
        db_path, node=node, task_type=task_type, cutoff=cutoff,
        exclude_run_id=exclude_run_id, limit=limit,
        lookback_days=lookback_days,
    )
    scored = score_candidates(
        list(candidates), samples,
        active_loads=active_loads, reserved_loads=reserved_loads,
    )
    ordered = sorted(scored, key=lambda row: (row["etqs_seconds"], row["candidate_index"]))
    ranked = []
    for position, row in enumerate(ordered, start=1):
        entry = dict(row)
        entry["rank"] = position
        ranked.append(entry)
    return ranked


def build_shadow_decision(
    *,
    workflow_id: str,
    run_id: str,
    task_id: str,
    node: str,
    task_type: str,
    actual_agent: str,
    rankings: List[Dict[str, Any]],
    created_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the persisted shadow payload (pure function, JSON-serializable)."""
    recommended = rankings[0]["agent"] if rankings else actual_agent
    return {
        "mode": "shadow",
        "workflow_id": workflow_id or "",
        "run_id": run_id or "",
        "task_id": task_id or "",
        "node": str(node or ""),
        "task_type": normalize_task_type(task_type),
        "actual_agent": actual_agent,
        "recommended_agent": recommended,
        "same_decision": bool(recommended == actual_agent),
        "candidate_rankings": [dict(row) for row in rankings],
        "algorithm_version": ALGORITHM_VERSION,
        "created_at": float(created_at) if created_at is not None else time.time(),
    }


def explain_ranking(rankings: List[Dict[str, Any]]) -> str:
    """Render a human-readable shadow report (read-only, for CLI/debug)."""
    lines = ["Adaptive Router v1 — Shadow", ""]
    for row in rankings:
        rate = row.get("qualified_success_rate")
        rate_text = f"{rate * 100:.1f}%" if rate is not None else "unknown"
        p50 = row.get("p50_wall_time_seconds")
        p50_text = f"{p50}s" if p50 is not None else "-"
        lines.append(f"{row['rank']}. {row['agent']}")
        lines.append(f"   ETQS: {row['etqs_seconds']}s")
        lines.append(f"   success: {rate_text}")
        lines.append(f"   p50: {p50_text}")
        lines.append(f"   samples: {row['sample_count']}")
    return "\n".join(lines)


@dataclass(frozen=True)
class ShadowContext:
    """Inputs the production router hands to the shadow layer (no I/O)."""

    workflow_id: str = ""
    stage: str = ""
    task_type: str = ""
    candidates: List[str] = field(default_factory=list)
    active_loads: Dict[str, int] = field(default_factory=dict)
    reserved_loads: Dict[str, int] = field(default_factory=dict)
    run_id: str = ""
    task_id: str = ""


def record_shadow_decision(
    *,
    store: Any,
    decided_at: float,
    actual_agent: str,
    context: ShadowContext,
    limit: Optional[int] = None,
    lookback_days: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute the shadow ranking and persist one route_decision event.

    Raises on any failure; the caller (agent_router) must treat this as
    fail-open instrumentation and never let it change the actual agent.
    """
    db_path = getattr(store, "db_path", None)
    rankings = rank_candidates(
        list(context.candidates),
        db_path=db_path,
        node=context.stage,
        task_type=context.task_type,
        cutoff=float(decided_at),
        exclude_run_id=context.run_id or None,
        active_loads=dict(context.active_loads),
        reserved_loads=dict(context.reserved_loads),
        limit=limit,
        lookback_days=lookback_days,
    )
    decision = build_shadow_decision(
        workflow_id=context.workflow_id,
        run_id=context.run_id,
        task_id=context.task_id,
        node=context.stage,
        task_type=context.task_type,
        actual_agent=actual_agent,
        rankings=rankings,
        created_at=float(decided_at),
    )
    store.record_event(
        "route_decision",
        decision,
        workflow_id=context.workflow_id or None,
        node_id=context.stage or None,
        task_id=context.task_id or None,
        agent_id=actual_agent or None,
        source="adaptive-router-shadow",
        timestamp=float(decided_at),
        run_id=context.run_id or None,
    )
    return decision


__all__ = [
    "ALGORITHM_VERSION",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_LOOKBACK_DAYS",
    "FALLBACK_EXECUTION_SECONDS",
    "MIN_SAMPLES_FOR_FULL_CONFIDENCE",
    "PRIOR_QUALIFIED_SUCCESS_RATE",
    "QUEUE_SECONDS_PER_LOAD",
    "RECOVERY_PENALTY_SECONDS",
    "REWORK_PENALTY_SECONDS",
    "ShadowContext",
    "build_shadow_decision",
    "collect_samples",
    "explain_ranking",
    "normalize_task_type",
    "qualified_success",
    "rank_candidates",
    "record_shadow_decision",
    "score_candidates",
]
