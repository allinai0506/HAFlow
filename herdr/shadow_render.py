#!/usr/bin/env python3
"""Shadow evaluation text rendering (herdr/shadow_render.py).

Human-readable report over the machine-readable dict composed by
``herdr.shadow_metrics.build_shadow_evaluation_report``. Formatting
only: no analysis, no I/O, no writes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _secs(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value != value:  # NaN guard
        return "-"
    return f"{value:.0f}s"


def render_shadow_report(report: Dict[str, Any]) -> str:
    """Render the human-readable text report (read-only)."""
    coverage = report.get("coverage") or {}
    agreement = report.get("agreement") or {}
    actual = report.get("actual_outcome") or {}
    calibration = report.get("calibration") or {}
    etqs = report.get("etqs") or {}
    disagreement = report.get("disagreement") or {}
    uplift = report.get("predicted_uplift") or {}
    sufficiency = report.get("data_sufficiency") or []
    readiness = report.get("canary_readiness") or {}
    collection = report.get("collection") or {}
    lines = [
        "Adaptive Router Shadow Evaluation",
        "",
    ]
    if collection.get("source_window_size") is not None:
        window_note = (
            f"Source window: scanned {collection.get('source_window_size')}"
            f", matched {collection.get('matched_rows')}"
            f" (limit {collection.get('requested_limit')})"
        )
        if collection.get("truncated"):
            window_note += " [TRUNCATED: older matching history unscanned]"
        lines += [window_note, ""]
    lines += [
        "Coverage",
        "--------",
        f"Route decisions:          {coverage.get('total_route_decisions', 0)}",
        f"With settled outcome:     {coverage.get('route_decisions_with_outcome', 0)}",
        f"Unique executions:        {coverage.get('unique_executions', 0)}",
        f"Settled executions:       {coverage.get('settled_executions', 0)}",
        f"Coverage:                 {_pct(coverage.get('outcome_coverage_rate'))}",
        "",
        "Agreement (known decisions only)",
        "--------------------------------",
        f"Same decision:            {agreement.get('same_decision_count', 0)}",
        f"Different decision:       {agreement.get('different_decision_count', 0)}",
        f"Unknown recommendation:   {agreement.get('unknown_decision_count', 0)}",
        f"Disagreement rate:        {_pct(agreement.get('disagreement_rate'))}",
        "",
        "Observed Actual Outcome",
        "-----------------------",
        f"Qualified success:        {_pct(actual.get('actual_qualified_success_rate'))}",
        f"P50 wall time:            {_secs(actual.get('actual_p50_wall_time_seconds'))}",
        f"P90 wall time:            {_secs(actual.get('actual_p90_wall_time_seconds'))}",
        f"Rework rate:              {_pct(actual.get('actual_rework_rate'))}",
        f"Blocked rate:             {_pct(actual.get('actual_blocked_rate'))}",
        f"Human intervention rate:  {_pct(actual.get('actual_human_intervention_rate'))}",
        "",
        "Prediction Calibration",
        "----------------------",
        f"Brier score:              {calibration.get('brier_score') if calibration.get('brier_score') is not None else '-'}",
        "",
        "Predicted Success   Samples   Observed Success",
    ]
    for bucket in calibration.get("buckets") or []:
        lines.append(
            f"{bucket.get('bucket', '-'):>17}   "
            f"{bucket.get('samples', 0):<7}   "
            f"{_pct(bucket.get('observed_success_rate'))}"
        )
    lines += [
        "",
        "ETQS Approximation (wall time is one attempt, not full TTQS)",
        "------------------------------------------------------------",
        f"Predicted ETQS P50 (all): {_secs(etqs.get('predicted_actual_agent_etqs_p50'))}",
        f"Observed success wall P50:{_secs(etqs.get('observed_success_wall_time_p50'))}",
        f"Paired predicted P50:     {_secs(etqs.get('paired_predicted_etqs_p50'))}",
        f"Paired observed P50:      {_secs(etqs.get('paired_observed_wall_time_p50'))}",
        f"Median absolute error:    {_secs(etqs.get('median_absolute_error_seconds'))}",
        "",
        "Disagreement",
        "------------",
    ]
    groups = disagreement.get("groups") or []
    if not groups:
        lines.append("(none)")
    for group in groups:
        lines.append(
            f"{group.get('actual_agent')} -> {group.get('recommended_agent')}"
            f"  [{group.get('node')}/{group.get('task_type')}]"
            f"  {group.get('count')}"
        )
    uplift_val = uplift.get("median_predicted_success_uplift")
    uplift_text = "-" if uplift_val is None else f"{uplift_val * 100:+.1f}%"
    lines += [
        "",
        "Predicted Uplift (counterfactual, not observed)",
        "-----------------------------------------------",
        f"Median predicted success uplift: {uplift_text}",
        f"Median predicted ETQS improvement: {_secs(uplift.get('median_predicted_etqs_improvement_seconds'))}",
        f"Predicted samples (success/ETQS): {uplift.get('n_success', 0)}/{uplift.get('n_etqs', 0)}",
        "",
        "Data Sufficiency (model history vs calibration evidence)",
        "--------------------------------------------------------",
    ]
    if not sufficiency:
        lines.append("(no settled buckets)")
    for bucket in sufficiency:
        lines.append(
            f"{bucket.get('bucket_key')}"
            f"   model={bucket.get('model_sample_count')}"
            f"/{bucket.get('model_data_status')}"
            f"   eval={bucket.get('evaluation_sample_count')}"
            f"+calib={bucket.get('calibration_sample_count')}"
            f"/{bucket.get('evaluation_data_status')}"
        )
    lines += [
        "",
        "Canary Readiness (facts only, no eligibility verdict)",
        "-----------------------------------------------------",
        f"Model-sufficient buckets:      {readiness.get('model_sufficient_bucket_count', 0)}",
        f"Evaluation-sufficient buckets: {readiness.get('evaluation_sufficient_bucket_count', 0)}",
        f"Sufficient on both:            {readiness.get('sufficient_both_bucket_count', 0)}",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "render_shadow_report",
]
