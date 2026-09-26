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


def _predicted_probability(prediction: Optional[Dict[str, Any]]) -> Optional[float]:
    """Router belief used for calibration: blended success rate.

    Blended is the prior-smoothed rate the router actually prices into
    ETQS; it is always present when the ranking entry exists, while the
    raw observed rate may legitimately be None for cold agents.
    """
    if not prediction:
        return None
    value = prediction.get("blended_success_rate")
    if value is None:
        return None
    try:
        prob = float(value)
    except (TypeError, ValueError):
        return None
    if prob != prob:  # NaN never calibrates
        return None
    return prob


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
    # Unknown is its own agreement state: it must not count as either
    # agreement or disagreement downstream.
    recommended_agent = str(payload.get("recommended_agent") or "")
    if recommended_agent and recommended_agent == actual_agent:
        agreement_status = "same"
    elif recommended_agent:
        agreement_status = "different"
    else:
        agreement_status = "unknown"
    ranking_list = rankings if isinstance(rankings, list) else []
    model_evidence = [
        {
            "agent": str(entry.get("agent") or ""),
            "sample_count": entry.get("sample_count"),
            "confidence": entry.get("confidence"),
        }
        for entry in ranking_list
        if isinstance(entry, dict) and str(entry.get("agent") or "")
    ]
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
        "same_decision": agreement_status == "same",
        "agreement_status": agreement_status,
        "model_evidence": model_evidence,
        "actual_prediction": actual_prediction,
        "recommended_prediction": recommended_prediction,
        "actual_outcome": actual_outcome,
    }


def _outcome_matches(
    identity: Dict[str, str],
    actual_agent: str,
    outcome: Dict[str, Any],
) -> bool:
    """Single Outcome attribution contract (fail-closed).

    A prediction belongs to one execution identity; only an Outcome
    produced by that same identity may evaluate it:

    - (task_id, run_id) equal (checked by the caller keying);
    - workflow_id equal when both sides carry one;
    - decision.actual_agent == outcome.agent (no fallback);
    - node equal when both sides carry one;
    - task_type equal when both sides carry one.

    Anything uncertain returns False: the decision keeps its coverage
    count but never receives an Outcome.
    """
    decision_wf = identity["workflow_id"]
    outcome_wf = str(outcome.get("workflow_id") or "")
    if decision_wf and outcome_wf and decision_wf != outcome_wf:
        return False
    if str(actual_agent or "") != str(outcome.get("agent") or ""):
        return False
    decision_node = identity["node"]
    outcome_node = str(outcome.get("node") or "")
    if decision_node and outcome_node and decision_node != outcome_node:
        return False
    decision_type = identity["task_type"]
    outcome_type = str(outcome.get("task_type") or "")
    if decision_type and outcome_type and decision_type != outcome_type:
        return False
    return True


#: Default cap on route_decision events scanned while paginating for
#: filtered matches. Pagination stops at matched ``limit`` or at this
#: many scanned events, whichever comes first; the collection meta
#: reports which bound stopped the scan.
DEFAULT_SCAN_CAP = 10000

#: Newest-first page size for filtered decision scans.
SCAN_PAGE_SIZE = 1000


def _matches_filters(
    row: Dict[str, Any],
    *,
    node: Optional[str],
    task_type: Optional[str],
    agent: Optional[str],
) -> bool:
    if node is not None and row["node"] != str(node):
        return False
    if task_type is not None and row["task_type"] != str(task_type):
        return False
    if (
        agent is not None
        and row["actual_agent"] != str(agent)
        and row["recommended_agent"] != str(agent)
    ):
        return False
    return True


def _page_pairs(page: List[Dict[str, Any]]) -> List[Any]:
    pairs: List[Any] = []
    for event in page:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        pairs.append((
            str(payload.get("task_id") or event.get("task_id") or ""),
            str(payload.get("run_id") or event.get("run_id") or ""),
        ))
    return pairs


def _build_page_rows(
    page: List[Dict[str, Any]],
    outcomes: Dict[Any, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for event in page:
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
                identity,
                str((payload.get("actual_agent")) or ""),
                candidate,
            ):
                outcome = candidate
        rows.append(build_evaluation_row(event, outcome))
    return rows


def _collect_rows_with_meta(
    db_path: Optional[Path] = None,
    *,
    node: Optional[str] = None,
    task_type: Optional[str] = None,
    agent: Optional[str] = None,
    since: Optional[float] = None,
    before: Optional[float] = None,
    limit: Optional[int] = None,
    page_size: int = SCAN_PAGE_SIZE,
    scan_cap: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Scan pages newest-first; join, filter and stop per page.

    Each iteration reads at most ``min(page_size, scan_cap - scanned)``
    events, batch-joins that page's Outcomes (no N+1), builds rows,
    applies filters, and stops immediately once ``limit`` matched rows
    exist. ``limit`` therefore counts matched rows, never raw
    pre-filter reads. Exactly one of four stop reasons is reported:

    - ``matched_limit``: matched target reached (not truncated);
    - ``scan_cap``: hard scan budget hit first (truncated);
    - ``exhausted``: event stream ended (not truncated);
    - ``cursor_stalled``: cursor made no progress (truncated).
    """
    effective_limit = (
        state_db.ROUTE_DECISION_DEFAULT_LIMIT if limit is None else int(limit)
    )
    if effective_limit < 1:
        raise ValueError("limit must be a positive int")
    page = max(1, min(int(page_size or SCAN_PAGE_SIZE), SCAN_PAGE_SIZE))
    cap = DEFAULT_SCAN_CAP if scan_cap is None else int(scan_cap)
    if cap < 1:
        raise ValueError("scan_cap must be a positive int")
    matched: List[Dict[str, Any]] = []
    seen_ids = set()
    scanned = 0
    stop_reason = "exhausted"
    cursor_ts: Optional[float] = None
    cursor_id: Optional[int] = None
    while True:
        remaining = cap - scanned
        if remaining <= 0:
            stop_reason = "scan_cap"
            break
        # The explicit ``before`` floor composes with the keyset cursor:
        # the query is bounded by whichever is older.
        query_before: Optional[float] = cursor_ts
        query_before_id: Optional[int] = cursor_id
        if before is not None and (
            cursor_ts is None or cursor_ts > float(before)
        ):
            query_before = float(before)
            query_before_id = None
        query_limit = min(page, remaining)
        chunk = state_db.query_route_decisions(
            limit=query_limit, since=since, before=query_before,
            before_id=query_before_id, db_path=db_path,
        )
        if not chunk:
            stop_reason = "exhausted"
            break
        fresh = [event for event in chunk
                 if event.get("decision_event_id") not in seen_ids]
        if not fresh:  # cursor made no progress; avoid an infinite loop
            stop_reason = "cursor_stalled"
            break
        for event in fresh:
            seen_ids.add(event.get("decision_event_id"))
        outcomes = state_db.batch_get_execution_outcomes(
            _page_pairs(fresh), db_path=db_path)
        for row in _build_page_rows(fresh, outcomes):
            if _matches_filters(
                row, node=node, task_type=task_type, agent=agent
            ):
                matched.append(row)
                if len(matched) >= effective_limit:
                    break
        scanned += len(fresh)
        if len(matched) >= effective_limit:
            stop_reason = "matched_limit"
            break
        if scanned >= cap:
            stop_reason = "scan_cap"
            break
        if len(chunk) < query_limit:
            stop_reason = "exhausted"
            break
        cursor_ts = min(float(event["decision_at"]) for event in fresh)
        cursor_id = min(int(event["decision_event_id"]) for event in fresh
                        if float(event["decision_at"]) == cursor_ts)
    matched = matched[:effective_limit]
    meta = {
        "source_window_size": scanned,
        "matched_rows": len(matched),
        "requested_limit": effective_limit,
        "scan_cap": cap,
        "stop_reason": stop_reason,
        "truncated": stop_reason in ("scan_cap", "cursor_stalled"),
        "exhausted": stop_reason == "exhausted",
        "filter_mode": "filter-then-limit",
    }
    return matched, meta


def collect_evaluation_rows(
    db_path: Optional[Path] = None,
    *,
    node: Optional[str] = None,
    task_type: Optional[str] = None,
    agent: Optional[str] = None,
    since: Optional[float] = None,
    before: Optional[float] = None,
    limit: Optional[int] = None,
    page_size: int = SCAN_PAGE_SIZE,
    scan_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Join frozen decisions to settled outcomes (bounded, read-only).

    Filters apply during a newest-first paginated scan BEFORE ``limit``,
    so ``limit`` counts matched rows: deep-history matches are found
    instead of silently dropped. See ``_collect_rows_with_meta`` for
    the scan bounds and collection metadata.
    """
    rows, _ = _collect_rows_with_meta(
        db_path, node=node, task_type=task_type, agent=agent,
        since=since, before=before, limit=limit,
        page_size=page_size, scan_cap=scan_cap,
    )
    return rows


def select_authoritative_execution_rows(
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Dedup decision rows to one authoritative row per execution.

    Execution identity is (task_id, run_id); rows without both never
    enter settled-execution evaluation (they keep their decision-level
    coverage only). Attribution already guarantees an attached Outcome
    was produced by the row's own actual_agent, so selection is:

    1. keep rows with a settled ``actual_outcome``;
    2. group by (task_id, run_id);
    3. keep the latest ``decision_at``, tie-broken by the larger
       ``decision_event_id`` (``ORDER BY decision_at DESC,
       decision_event_id DESC LIMIT 1``).

    Pure function, order-independent: one execution yields at most one
    calibration sample no matter how many retries were decided.
    """
    best: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        if not row.get("actual_outcome"):
            continue
        task_id = str(row.get("task_id") or "")
        run_id = str(row.get("run_id") or "")
        if not task_id or not run_id:
            continue
        key = (task_id, run_id)
        current = best.get(key)
        if current is None or (
            float(row.get("decision_at") or 0.0),
            int(row.get("decision_event_id") or 0),
        ) > (
            float(current.get("decision_at") or 0.0),
            int(current.get("decision_event_id") or 0),
        ):
            best[key] = row
    return [best[key] for key in sorted(best)]


@dataclass(frozen=True)
class ShadowEvaluationFilters:
    """Read-only CLI filter set (never mutates router or outcome data)."""

    node: Optional[str] = None
    task_type: Optional[str] = None
    agent: Optional[str] = None
    since: Optional[float] = None
    before: Optional[float] = None
    limit: Optional[int] = None
    scan_cap: Optional[int] = None


__all__ = [
    "ShadowEvaluationFilters",
    "build_evaluation_row",
    "collect_evaluation_rows",
    "extract_prediction",
    "select_authoritative_execution_rows",
]
