#!/usr/bin/env python3
"""Shadow evaluation metrics layer (herdr/shadow_metrics.py).

Pure aggregations over rows built by ``herdr.shadow_rows``: coverage,
agreement, observed actual outcome, frozen-prediction calibration
(Brier), ETQS approximation, disagreement grouping, counterfactual
predicted uplift, data sufficiency, and the composed report. No I/O.

Shadow is not A/B: uplift here is a predicted estimate over
disagreement rows, never an observed win rate. Calibration uses the
blended success rate (the prior-smoothed belief the router prices
into ETQS); observed wall time is one attempt's dispatch-to-finish,
not full time-to-qualified-success.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .shadow_rows import _predicted_probability

#: Calibration buckets over the predicted success probability.
CALIBRATION_BUCKETS: Tuple[Tuple[float, float], ...] = (
    (0.0, 0.2),
    (0.2, 0.4),
    (0.4, 0.6),
    (0.6, 0.8),
    (0.8, 1.0),
)


def _percentile(sorted_vals: List[float], fraction: float) -> Optional[float]:
    """Deterministic nearest-rank percentile over an ascending-sorted list."""
    if not sorted_vals:
        return None
    rank = int(fraction * len(sorted_vals) + 0.999999999)
    rank = max(1, min(rank, len(sorted_vals)))
    return float(sorted_vals[rank - 1])


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return _percentile(sorted(values), 0.5)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return numerator / denominator


def _round6(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6)


def _bucket_for(probability: float) -> Tuple[float, float]:
    for low, high in CALIBRATION_BUCKETS:
        if low <= probability < high or (
            high == 1.0 and probability == 1.0
        ):
            return (low, high)
    if probability < 0.0:
        return CALIBRATION_BUCKETS[0]
    return CALIBRATION_BUCKETS[-1]


def _bucket_label(bounds: Tuple[float, float]) -> str:
    return f"{bounds[0]:.1f}-{bounds[1]:.1f}"


def evaluate_coverage(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """total decisions vs decisions with a settled outcome."""
    total = len(rows)
    with_outcome = sum(1 for row in rows if row.get("actual_outcome"))
    return {
        "total_route_decisions": total,
        "route_decisions_with_outcome": with_outcome,
        "outcome_coverage_rate": _round6(_rate(with_outcome, total)),
    }


def _agreement_of(row: Dict[str, Any]) -> str:
    """Row agreement state: same / different / unknown.

    Falls back to the legacy ``same_decision`` bool for rows built
    outside ``herdr.shadow_rows`` (unknown then reads as different,
    matching the pre-tri-state behavior).
    """
    status = row.get("agreement_status")
    if status in ("same", "different", "unknown"):
        return str(status)
    return "same" if row.get("same_decision") else "different"


def _is_different(row: Dict[str, Any]) -> bool:
    return _agreement_of(row) == "different"


def evaluate_agreement(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Same vs different over KNOWN decisions; unknown stays separate.

    ``disagreement_rate = different / (same + different)``: rows whose
    recommendation is unknown are counted but never enter the
    denominator, so "no recommendation" cannot read as "disagreed".
    """
    same = sum(1 for row in rows if _agreement_of(row) == "same")
    different = sum(1 for row in rows if _agreement_of(row) == "different")
    unknown = len(rows) - same - different
    known = same + different
    return {
        "same_decision_count": same,
        "different_decision_count": different,
        "unknown_decision_count": unknown,
        "agreement_known_count": known,
        "disagreement_rate": _round6(_rate(different, known)),
    }


def evaluate_actual_outcome(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Observed performance of actual_agent over settled outcomes only."""
    settled = [row for row in rows if row.get("actual_outcome")]
    n = len(settled)
    if not n:
        return {
            "n": 0,
            "actual_qualified_success_rate": None,
            "actual_p50_wall_time_seconds": None,
            "actual_p90_wall_time_seconds": None,
            "actual_rework_rate": None,
            "actual_blocked_rate": None,
            "actual_human_intervention_rate": None,
        }
    qualified = sum(
        1 for row in settled if row["actual_outcome"]["qualified_success"]
    )
    walls = sorted(
        float(row["actual_outcome"]["wall_time_seconds"])
        for row in settled
        if row["actual_outcome"]["wall_time_seconds"] is not None
    )
    rework = sum(
        1 for row in settled if row["actual_outcome"]["rework_count"] > 0
    )
    blocked = sum(
        1 for row in settled if row["actual_outcome"]["blocked_count"] > 0
    )
    human = sum(
        1
        for row in settled
        if row["actual_outcome"]["human_intervention_count"] > 0
    )
    return {
        "n": n,
        "actual_qualified_success_rate": _round6(_rate(qualified, n)),
        "actual_p50_wall_time_seconds": _percentile(walls, 0.5),
        "actual_p90_wall_time_seconds": _percentile(walls, 0.9),
        "actual_rework_rate": _round6(_rate(rework, n)),
        "actual_blocked_rate": _round6(_rate(blocked, n)),
        "actual_human_intervention_rate": _round6(_rate(human, n)),
    }


def evaluate_calibration(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Frozen actual_agent prediction vs settled outcome.

    Buckets the blended success probability and reports the observed
    qualified-success rate per bucket, plus the Brier score
    mean((p - y)^2). Rows without a frozen actual prediction or
    without a settled outcome are excluded, never imputed.
    """
    paired = []
    for row in rows:
        if not row.get("actual_outcome"):
            continue
        prob = _predicted_probability(row.get("actual_prediction"))
        if prob is None:
            continue
        paired.append(
            (prob, 1.0 if row["actual_outcome"]["qualified_success"] else 0.0)
        )
    buckets = []
    for bounds in CALIBRATION_BUCKETS:
        probs = [p for p, _ in paired if _bucket_for(p) == bounds]
        outcomes = [y for p, y in paired if _bucket_for(p) == bounds]
        buckets.append({
            "bucket": _bucket_label(bounds),
            "samples": len(probs),
            "observed_success_rate": _round6(
                _rate(sum(1 for y in outcomes if y >= 1.0), len(outcomes))
            ),
        })
    if paired:
        brier = sum((p - y) ** 2 for p, y in paired) / len(paired)
    else:
        brier = None
    return {
        "n": len(paired),
        "brier_score": None if brier is None else round(brier, 6),
        "buckets": buckets,
    }


def evaluate_etqs_approximation(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Frozen ETQS vs observed wall time (approximation, not TTQS).

    Observed wall time is dispatch-to-finish for one attempt; it is NOT
    a full time-to-qualified-success (retries after this outcome are
    invisible here). Ratios and errors are paired per row.
    """
    predicted: List[float] = []
    success_walls: List[float] = []
    paired_errors: List[float] = []
    paired_ratios: List[float] = []
    paired_predicted: List[float] = []
    paired_observed: List[float] = []
    for row in rows:
        outcome = row.get("actual_outcome")
        prediction = row.get("actual_prediction") or {}
        etqs = prediction.get("etqs_seconds")
        try:
            etqs_f = float(etqs) if etqs is not None else None
        except (TypeError, ValueError):
            etqs_f = None
        if etqs_f is not None and etqs_f == etqs_f:
            predicted.append(etqs_f)
        if outcome is None:
            continue
        wall = outcome.get("wall_time_seconds")
        try:
            wall_f = float(wall) if wall is not None else None
        except (TypeError, ValueError):
            wall_f = None
        if wall_f is None or wall_f != wall_f:
            continue
        if outcome.get("qualified_success"):
            success_walls.append(wall_f)
        if etqs_f is not None:
            paired_errors.append(abs(etqs_f - wall_f))
            paired_predicted.append(etqs_f)
            paired_observed.append(wall_f)
            if wall_f > 0:
                paired_ratios.append(etqs_f / wall_f)
    return {
        "n_paired": len(paired_errors),
        "predicted_actual_agent_etqs_p50": _median(predicted),
        "observed_success_wall_time_p50": _median(success_walls),
        "paired_predicted_etqs_p50": _median(paired_predicted),
        "paired_observed_wall_time_p50": _median(paired_observed),
        "median_absolute_error_seconds": _median(paired_errors),
        "p50_prediction_ratio": _median(paired_ratios),
        "p90_prediction_ratio": _percentile(sorted(paired_ratios), 0.9),
        "note": (
            "observed wall time is one attempt's dispatch-to-finish, "
            "not full time-to-qualified-success; compare as approximation."
        ),
    }


def evaluate_disagreement(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Group known-different decisions by node/task_type/pair.

    Unknown recommendations are excluded: "no recommendation" is not
    a disagreement and must not appear as an ``actual -> ""`` group.
    """
    groups: Dict[Tuple[str, str, str, str], Dict[str, int]] = {}
    for row in rows:
        if not _is_different(row):
            continue
        key = (
            str(row.get("node") or ""),
            str(row.get("task_type") or ""),
            str(row.get("actual_agent") or ""),
            str(row.get("recommended_agent") or ""),
        )
        cell = groups.setdefault(key, {"count": 0, "with_outcome": 0})
        cell["count"] += 1
        if row.get("actual_outcome"):
            cell["with_outcome"] += 1
    ordered = sorted(
        (
            {
                "node": key[0],
                "task_type": key[1],
                "actual_agent": key[2],
                "recommended_agent": key[3],
                "count": cell["count"],
                "with_outcome": cell["with_outcome"],
            }
            for key, cell in groups.items()
        ),
        key=lambda g: (-g["count"], g["node"], g["task_type"],
                       g["actual_agent"], g["recommended_agent"]),
    )
    return {
        "disagreement_count": sum(cell["count"] for cell in ordered),
        "groups": ordered,
    }


def evaluate_predicted_uplift(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Predicted (counterfactual) uplift on known-different rows only.

    NEVER an observed win rate: recommended_agent never executed.
    Positive medians mean the frozen model *believed* it could do
    better, not that it did. Unknown recommendations are excluded.
    """
    success_gaps: List[float] = []
    etqs_gaps: List[float] = []
    for row in rows:
        if not _is_different(row):
            continue
        actual = row.get("actual_prediction") or {}
        recommended = row.get("recommended_prediction") or {}
        try:
            actual_b = float(actual["blended_success_rate"])
            recommended_b = float(recommended["blended_success_rate"])
        except (TypeError, ValueError, KeyError):
            continue
        if actual_b != actual_b or recommended_b != recommended_b:
            continue
        success_gaps.append(recommended_b - actual_b)
        try:
            actual_e = float(actual["etqs_seconds"])
            recommended_e = float(recommended["etqs_seconds"])
        except (TypeError, ValueError, KeyError):
            continue
        if actual_e == actual_e and recommended_e == recommended_e:
            etqs_gaps.append(actual_e - recommended_e)
    return {
        "n_success": len(success_gaps),
        "n_etqs": len(etqs_gaps),
        "median_predicted_success_uplift": _round6(_median(success_gaps)),
        "median_predicted_etqs_improvement_seconds": _median(etqs_gaps),
        "note": (
            "counterfactual estimate, not observed fact: recommended_agent "
            "never executed; do not report as a win rate or saved time."
        ),
    }




__all__ = [
    "CALIBRATION_BUCKETS",
    "evaluate_actual_outcome",
    "evaluate_agreement",
    "evaluate_calibration",
    "evaluate_coverage",
    "evaluate_disagreement",
    "evaluate_etqs_approximation",
    "evaluate_predicted_uplift",
]
