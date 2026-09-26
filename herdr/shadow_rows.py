#!/usr/bin/env python3
"""Shadow evaluation row layer (herdr/shadow_rows.py).

Joins frozen ``route_decision`` payloads with immutable
``agent_execution_outcomes`` into read-only evaluation rows. Pure
functions plus one bounded read path; no analysis, no rendering.

Identity: a decision joins an outcome only on (task_id, run_id), with
workflow_id cross-checked when both sides carry one. Anything else is
skipped, never guessed. A missing ranking entry means the prediction
is unavailable, never backfilled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import state_db


def extract_prediction(
    candidate_rankings: Any, agent: str
) -> Optional[Dict[str, Any]]:
    """Pull one agent's frozen prediction out of persisted rankings.

    Returns None when the agent has no entry: the prediction is
    unavailable and must never be guessed or backfilled.
    """
    if not isinstance(candidate_rankings, list):
        return None
    for entry in candidate_rankings:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("agent") or "") != str(agent):
            continue
        return {
            "qualified_success_rate": entry.get("qualified_success_rate"),
            "blended_success_rate": entry.get("blended_success_rate"),
            "etqs_seconds": entry.get("etqs_seconds"),
            "p50_wall_time_seconds": entry.get("p50_wall_time_seconds"),
            "sample_count": entry.get("sample_count"),
            "confidence": entry.get("confidence"),
        }
    return None


def _decision_identity(payload: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, str]:
    return {
        "workflow_id": str(payload.get("workflow_id") or event.get("workflow_id") or ""),
        "task_id": str(payload.get("task_id") or event.get("task_id") or ""),
        "run_id": str(payload.get("run_id") or event.get("run_id") or ""),
        "node": str(payload.get("node") or event.get("node_id") or ""),
        "task_type": str(payload.get("task_type") or ""),
    }


def build_evaluation_row(
    event: Dict[str, Any],
    outcome: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build one read-only evaluation row (pure function, no I/O)."""
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    identity = _decision_identity(payload, event)
    rankings = payload.get("candidate_rankings")
    actual_agent = str(payload.get("actual_agent") or "")
    # A missing recommendation is unknown, never defaulted to actual:
    # defaulting would fabricate same_decision=True for legacy payloads.
    recommended_agent = str(payload.get("recommended_agent") or "")
    same_decision = bool(recommended_agent and recommended_agent == actual_agent)
    actual_prediction = extract_prediction(rankings, actual_agent)
    if recommended_agent and recommended_agent == actual_agent:
        recommended_prediction = actual_prediction
    else:
        recommended_prediction = extract_prediction(rankings, recommended_agent)
    actual_outcome: Optional[Dict[str, Any]] = None
    if outcome is not None:
        actual_outcome = {
            "qualified_success": bool(outcome.get("qualified_success")),
            "wall_time_seconds": outcome.get("wall_time_seconds"),
            "rework_count": int(outcome.get("rework_count") or 0),
            "blocked_count": int(outcome.get("blocked_count") or 0),
            "human_intervention_count": int(
                outcome.get("human_intervention_count") or 0
            ),
        }
    return {
        "decision_event_id": event.get("decision_event_id"),
        "decision_at": event.get("decision_at"),
        "workflow_id": identity["workflow_id"],
        "task_id": identity["task_id"],
        "run_id": identity["run_id"],
        "node": identity["node"],
        "task_type": identity["task_type"],
        "actual_agent": actual_agent,
        "recommended_agent": recommended_agent,
        "same_decision": same_decision,
        "actual_prediction": actual_prediction,
        "recommended_prediction": recommended_prediction,
        "actual_outcome": actual_outcome,
    }


def _outcome_matches(
    identity: Dict[str, str], outcome: Dict[str, Any]
) -> bool:
    decision_wf = identity["workflow_id"]
    outcome_wf = str(outcome.get("workflow_id") or "")
    if decision_wf and outcome_wf and decision_wf != outcome_wf:
        return False
    return True


def collect_evaluation_rows(
    db_path: Optional[Path] = None,
    *,
    node: Optional[str] = None,
    task_type: Optional[str] = None,
    agent: Optional[str] = None,
    since: Optional[float] = None,
    before: Optional[float] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Join frozen decisions to settled outcomes (bounded, read-only).

    Decisions come newest-first from ``state_db.query_route_decisions``;
    outcomes resolve in one chunked batch (no N+1). Python-side filters
    (node/task_type/agent) apply after the join so ``limit`` always
    bounds storage reads. ``agent`` matches either side of the decision.
    """
    events = state_db.query_route_decisions(
        limit=limit, since=since, before=before, db_path=db_path
    )
    pairs = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        pairs.append((
            str(payload.get("task_id") or event.get("task_id") or ""),
            str(payload.get("run_id") or event.get("run_id") or ""),
        ))
    outcomes = state_db.batch_get_execution_outcomes(pairs, db_path=db_path)
    rows: List[Dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        identity = _decision_identity(payload, event)
        outcome: Optional[Dict[str, Any]] = None
        if identity["task_id"] and identity["run_id"]:
            candidate = outcomes.get(
                (identity["task_id"], identity["run_id"])
            )
            if candidate is not None and _outcome_matches(
                identity, candidate
            ):
                outcome = candidate
        row = build_evaluation_row(event, outcome)
        if node is not None and row["node"] != str(node):
            continue
        if task_type is not None and row["task_type"] != str(task_type):
            continue
        if agent is not None and row["actual_agent"] != str(agent) and row[
            "recommended_agent"
        ] != str(agent):
            continue
        rows.append(row)
    return rows


@dataclass(frozen=True)
class ShadowEvaluationFilters:
    """Read-only CLI filter set (never mutates router or outcome data)."""

    node: Optional[str] = None
    task_type: Optional[str] = None
    agent: Optional[str] = None
    since: Optional[float] = None
    before: Optional[float] = None
    limit: Optional[int] = None


__all__ = [
    "ShadowEvaluationFilters",
    "build_evaluation_row",
    "collect_evaluation_rows",
    "extract_prediction",
]
