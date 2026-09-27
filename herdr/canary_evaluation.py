#!/usr/bin/env python3
"""Adaptive Router Canary Evaluation v1 (herdr/canary_evaluation.py).

Read-only retrospective over mode="canary" route decisions: the first
comparison of Legacy executions vs Adaptive executions on real,
settled Outcome facts. No LLM, no network, no randomness, no writes:
identical inputs always produce identical reports.

Arms come from the canary decision itself:

- adaptive arm: ``diverted=True`` — the routing picked the Adaptive
  Router's recommendation, and that agent really executed.
- legacy arm: ``diverted=False`` — the deterministic hash (or a
  same-decision agreement) kept the legacy pick.

Both arms live in the same whitelisted bucket and the same time
window, so the split is the honest control group the shadow phase
could never have. Attribution reuses the single Outcome contract
(``shadow_rows._outcome_matches``): an outcome joins only when
``decision.actual_agent == outcome.agent``.

The report states observed facts only. It never claims significance
and never issues a rollout verdict: expanding traffic is #104 and
remains a human decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .shadow_rows import (
    SCAN_PAGE_SIZE,
    _collect_rows_with_meta,
    _decision_identity,
    _outcome_matches,
    extract_prediction,
    select_authoritative_execution_rows,
)

#: Facts-only note carried by every report and rendering.
NO_ROLLOUT_VERDICT_NOTE = (
    "observed facts only: adaptive arm (diverted) vs legacy arm "
    "(not diverted) in the same whitelisted buckets; samples are "
    "small and no significance is claimed. Expanding canary traffic "
    "is #104 (Controlled Rollout) and remains a human decision."
)


@dataclass(frozen=True)
class CanaryEvaluationFilters:
    """Read-only CLI filter set (never mutates router or outcome data)."""

    node: Optional[str] = None
    task_type: Optional[str] = None
    agent: Optional[str] = None
    since: Optional[float] = None
    before: Optional[float] = None
    limit: Optional[int] = None
    scan_cap: Optional[int] = None


def build_canary_row(
    event: Dict[str, Any],
    outcome: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build one canary evaluation row; None for non-canary events."""
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    if str(payload.get("mode") or "") != "canary":
        return None
    identity = _decision_identity(payload, event)
    rankings = payload.get("candidate_rankings")
    actual_agent = str(payload.get("actual_agent") or "")
    recommended_agent = str(payload.get("recommended_agent") or "")
    actual_outcome: Optional[Dict[str, Any]] = None
    if outcome is not None and _outcome_matches(identity, actual_agent, outcome):
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
        "legacy_agent": str(payload.get("legacy_agent") or ""),
        "diverted": bool(payload.get("diverted")),
        "same_decision": bool(recommended_agent == actual_agent),
        "actual_prediction": extract_prediction(rankings, actual_agent),
        "actual_outcome": actual_outcome,
        "canary_gate": (
            payload.get("canary_gate")
            if isinstance(payload.get("canary_gate"), dict) else {}
        ),
    }


def collect_canary_rows(
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
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Scan route_decision pages newest-first; keep mode="canary" only.

    Bounded like the shadow scan (one shared implementation in
    ``shadow_rows``): stops at matched ``limit`` or the scanned
    ``scan_cap`` (or when the stream is exhausted), and reports which
    bound stopped it. Non-canary events consume scan budget and are
    counted in the meta.
    """
    return _collect_rows_with_meta(
        db_path,
        node=node,
        task_type=task_type,
        agent=agent,
        since=since,
        before=before,
        limit=limit,
        page_size=page_size,
        scan_cap=scan_cap,
        mode="canary",
        row_builder=build_canary_row,
    )


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _arm_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate observed Outcome facts for one arm (pure)."""
    settled = [row for row in rows if row.get("actual_outcome")]
    walls: List[float] = []
    etqs_errors: List[float] = []
    rework: List[float] = []
    blocked: List[float] = []
    human: List[float] = []
    successes = 0
    for row in settled:
        outcome = row["actual_outcome"]
        if outcome["qualified_success"]:
            successes += 1
        wall = outcome.get("wall_time_seconds")
        if wall is not None:
            walls.append(float(wall))
            prediction = row.get("actual_prediction") or {}
            predicted = prediction.get("etqs_seconds")
            if predicted is not None:
                etqs_errors.append(abs(float(predicted) - float(wall)))
        rework.append(float(outcome.get("rework_count") or 0))
        blocked.append(float(outcome.get("blocked_count") or 0))
        human.append(float(outcome.get("human_intervention_count") or 0))
    count = len(settled)
    mean = _mean
    return {
        "sample_count": count,
        "qualified_success_count": successes,
        "qualified_success_rate": (
            round(successes / count, 6) if count else None
        ),
        "median_wall_time_seconds": _median(walls),
        "wall_time_sample_count": len(walls),
        "mean_rework_count": round(mean(rework), 4) if count else None,
        "mean_blocked_count": round(mean(blocked), 4) if count else None,
        "mean_human_intervention_count": (
            round(mean(human), 4) if count else None
        ),
        "etqs_mae_seconds": _median(etqs_errors),
        "etqs_sample_count": len(etqs_errors),
    }


def _delta(adaptive: Dict[str, Any], legacy: Dict[str, Any]) -> Dict[str, Any]:
    """Signed adaptive-minus-legacy differences (facts, not verdicts)."""

    def difference(key: str, digits: int) -> Optional[float]:
        left = adaptive.get(key)
        right = legacy.get(key)
        if left is None or right is None:
            return None
        return round(float(left) - float(right), digits)

    return {
        "qualified_success_rate_delta": difference(
            "qualified_success_rate", 6),
        "median_wall_time_delta_seconds": difference(
            "median_wall_time_seconds", 6),
        "mean_rework_count_delta": difference("mean_rework_count", 4),
        "mean_blocked_count_delta": difference("mean_blocked_count", 4),
        "mean_human_intervention_count_delta": difference(
            "mean_human_intervention_count", 4),
        "etqs_mae_delta_seconds": difference("etqs_mae_seconds", 6),
    }


def build_canary_evaluation_report(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compose the deterministic machine-readable canary report.

    Two explicit sample units: ``coverage`` reads decision-level rows;
    arm metrics read authoritative execution rows — the shadow dedup
    (one (task_id, run_id) keeps at most one settled sample, latest
    decision first) so a retried execution's immutable Outcome is
    never counted twice. Buckets are keyed on the same identity the
    gate admits: recommended_agent x node x task_type.
    """
    settled_rows = select_authoritative_execution_rows(rows)
    diverted = [row for row in rows if row["diverted"]]
    legacy = [row for row in rows if not row["diverted"]]
    buckets: Dict[Tuple[str, str, str], Dict[str, List[Dict[str, Any]]]] = {}
    for row in settled_rows:
        key = (
            str(row["recommended_agent"]),
            str(row["node"]),
            str(row["task_type"]),
        )
        buckets.setdefault(
            key, {"adaptive": [], "legacy": []})["adaptive" if row["diverted"]
                                                 else "legacy"].append(row)
    bucket_reports = []
    for (agent, node, task_type) in sorted(buckets):
        arms = buckets[(agent, node, task_type)]
        adaptive_metrics = _arm_metrics(arms["adaptive"])
        legacy_metrics = _arm_metrics(arms["legacy"])
        bucket_reports.append({
            "recommended_agent": agent,
            "node": node,
            "task_type": task_type,
            "adaptive_arm": adaptive_metrics,
            "legacy_arm": legacy_metrics,
            "delta": _delta(adaptive_metrics, legacy_metrics),
        })
    return {
        "coverage": {
            "canary_decisions": len(rows),
            "settled_canary_executions": len(settled_rows),
            "diverted_decisions": len(diverted),
            "legacy_decisions": len(legacy),
        },
        "buckets": bucket_reports,
        "note": NO_ROLLOUT_VERDICT_NOTE,
    }


def run_canary_evaluation(
    db_path: Optional[Path] = None,
    filters: Optional[CanaryEvaluationFilters] = None,
) -> Dict[str, Any]:
    """Collect rows and build the report in one read-only call."""
    active = filters or CanaryEvaluationFilters()
    rows, collection = collect_canary_rows(
        db_path,
        node=active.node,
        task_type=active.task_type,
        agent=active.agent,
        since=active.since,
        before=active.before,
        limit=active.limit,
        scan_cap=active.scan_cap,
    )
    report = build_canary_evaluation_report(rows)
    report["collection"] = collection
    return {"rows": rows, "report": report}


def render_canary_report(report: Dict[str, Any]) -> str:
    """Render a human-readable canary report (deterministic)."""
    coverage = report.get("coverage") or {}
    lines = [
        "Adaptive Router v2 — Canary Evaluation (observed facts only)",
        "",
        f"canary decisions: {coverage.get('canary_decisions', 0)}"
        f" | settled executions: "
        f"{coverage.get('settled_canary_executions', 0)}"
        f" | diverted (adaptive): {coverage.get('diverted_decisions', 0)}"
        f" | legacy: {coverage.get('legacy_decisions', 0)}",
        "",
    ]
    buckets = report.get("buckets") or []
    if not buckets:
        lines.append("no settled canary executions in the scanned window")
    for bucket in buckets:
        lines.append(
            f"bucket {bucket['recommended_agent']}"
            f" / {bucket['node']}"
            f" / {bucket['task_type'] or '(no type)'}")
        for arm_name in ("adaptive_arm", "legacy_arm"):
            arm = bucket[arm_name]
            label = "adaptive" if arm_name == "adaptive_arm" else "legacy"
            rate = arm.get("qualified_success_rate")
            rate_text = f"{rate * 100:.1f}%" if rate is not None else "-"
            median = arm.get("median_wall_time_seconds")
            median_text = f"{median}s" if median is not None else "-"
            etqs = arm.get("etqs_mae_seconds")
            etqs_text = f"{etqs}s" if etqs is not None else "-"
            lines.append(
                f"  {label}: n={arm.get('sample_count', 0)}"
                f" qualified={rate_text}"
                f" median_wall={median_text}"
                f" rework={arm.get('mean_rework_count')}"
                f" blocked={arm.get('mean_blocked_count')}"
                f" human={arm.get('mean_human_intervention_count')}"
                f" etqs_mae={etqs_text}"
            )
        delta = bucket.get("delta") or {}
        lines.append(
            f"  delta (adaptive - legacy):"
            f" qualified={delta.get('qualified_success_rate_delta')}"
            f" median_wall={delta.get('median_wall_time_delta_seconds')}s"
            f" rework={delta.get('mean_rework_count_delta')}"
        )
        lines.append("")
    lines.append(f"note: {report.get('note', '')}")
    return "\n".join(lines)


__all__ = [
    "CanaryEvaluationFilters",
    "NO_ROLLOUT_VERDICT_NOTE",
    "build_canary_evaluation_report",
    "build_canary_row",
    "collect_canary_rows",
    "render_canary_report",
    "run_canary_evaluation",
]
