#!/usr/bin/env python3
"""Adaptive Router Shadow Evaluation v1 (herdr/shadow_evaluation.py).

Read-only retrospective: joins frozen ``route_decision`` payloads with
immutable ``agent_execution_outcomes`` and reports whether the shadow
recommendation was worth trusting. No LLM, no network, no randomness,
no writes: identical inputs always produce identical reports.

Layout (one lifecycle stage per module):

- ``herdr.shadow_rows``: frozen decision x outcome join into rows.
- ``herdr.shadow_metrics``: pure aggregations over rows.
- ``herdr.shadow_sufficiency``: model vs evaluation evidence + statuses.
- ``herdr.shadow_render``: human-readable text rendering.
- This module: pipeline composition (collect rows, build report) and
  the stable public surface (``__all__`` unchanged since v1).

Shadow is not A/B. For one task only ``actual_agent`` ever executed;
``recommended_agent`` never ran, so this package NEVER claims it
"would have won". Uplift is labeled a counterfactual predicted
estimate, and calibration is measured only on ``actual_agent``
(frozen prediction vs later settled outcome).

Frozen, not recomputed: predictions always come from the decision's
``candidate_rankings`` persisted at decision time. Re-running today's
router over past tasks would leak future outcomes into the prediction.

Identity: a decision joins an outcome only on (task_id, run_id), with
workflow_id cross-checked when both sides carry one. Anything else is
skipped, never guessed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .shadow_metrics import (
    CALIBRATION_BUCKETS,
    evaluate_actual_outcome,
    evaluate_agreement,
    evaluate_calibration,
    evaluate_coverage,
    evaluate_disagreement,
    evaluate_etqs_approximation,
    evaluate_predicted_uplift,
)
from .shadow_render import render_shadow_report
from .shadow_rows import (
    ShadowEvaluationFilters,
    _collect_rows_with_meta,
    build_evaluation_row,
    collect_evaluation_rows,
    extract_prediction,
)
from .shadow_sufficiency import (
    COLD_THRESHOLD,
    SUFFICIENT_THRESHOLD,
    evaluate_data_sufficiency,
    sufficiency_status,
)


def build_shadow_evaluation_report(
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compose the deterministic machine-readable evaluation report."""
    coverage = evaluate_coverage(rows)
    agreement = evaluate_agreement(rows)
    actual_outcome = evaluate_actual_outcome(rows)
    calibration = evaluate_calibration(rows)
    etqs = evaluate_etqs_approximation(rows)
    disagreement = evaluate_disagreement(rows)
    predicted_uplift = evaluate_predicted_uplift(rows)
    sufficiency = evaluate_data_sufficiency(rows)
    model_ok = sum(
        1 for b in sufficiency if b["model_data_status"] == "sufficient"
    )
    eval_ok = sum(
        1 for b in sufficiency
        if b["evaluation_data_status"] == "sufficient"
    )
    both = sorted(
        b["bucket_key"] for b in sufficiency
        if b["model_data_status"] == "sufficient"
        and b["evaluation_data_status"] == "sufficient"
    )
    return {
        "coverage": coverage,
        "agreement": agreement,
        "actual_outcome": actual_outcome,
        "calibration": calibration,
        "etqs": etqs,
        "disagreement": disagreement,
        "predicted_uplift": predicted_uplift,
        "data_sufficiency": sufficiency,
        "canary_readiness": {
            "model_sufficient_bucket_count": model_ok,
            "evaluation_sufficient_bucket_count": eval_ok,
            "sufficient_both_bucket_count": len(both),
            "sufficient_both_buckets": both,
            "note": (
                "facts only, no eligibility verdict: a bucket counts as "
                "sufficient on both sides only when the model predicted "
                "with history AND the prediction has been calibrated "
                "against settled outcomes; canary entry remains a "
                "separate human decision."
            ),
        },
    }


def run_shadow_evaluation(
    db_path: Optional[Path] = None,
    filters: Optional[ShadowEvaluationFilters] = None,
) -> Dict[str, Any]:
    """Collect rows and build the report in one read-only call."""
    active = filters or ShadowEvaluationFilters()
    rows, collection = _collect_rows_with_meta(
        db_path,
        node=active.node,
        task_type=active.task_type,
        agent=active.agent,
        since=active.since,
        before=active.before,
        limit=active.limit,
        scan_cap=active.scan_cap,
    )
    report = build_shadow_evaluation_report(rows)
    report["collection"] = collection
    return {"rows": rows, "report": report}


__all__ = [
    "CALIBRATION_BUCKETS",
    "COLD_THRESHOLD",
    "SUFFICIENT_THRESHOLD",
    "ShadowEvaluationFilters",
    "build_evaluation_row",
    "build_shadow_evaluation_report",
    "collect_evaluation_rows",
    "evaluate_actual_outcome",
    "evaluate_agreement",
    "evaluate_calibration",
    "evaluate_coverage",
    "evaluate_data_sufficiency",
    "evaluate_disagreement",
    "evaluate_etqs_approximation",
    "evaluate_predicted_uplift",
    "extract_prediction",
    "render_shadow_report",
    "run_shadow_evaluation",
    "sufficiency_status",
]
