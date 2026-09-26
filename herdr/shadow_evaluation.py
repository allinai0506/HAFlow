#!/usr/bin/env python3
"""Adaptive Router Shadow Evaluation v1 (herdr/shadow_evaluation.py).

Read-only retrospective: joins frozen ``route_decision`` payloads with
immutable ``agent_execution_outcomes`` and reports whether the shadow
recommendation was worth trusting. No LLM, no network, no randomness,
no writes: identical inputs always produce identical reports.

Layout (one lifecycle stage per module):

- ``herdr.shadow_rows``: frozen decision x outcome join into rows.
- ``herdr.shadow_metrics``: pure aggregations over rows + report.
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
from typing import Any, Dict, Optional

from .shadow_metrics import (
    CALIBRATION_BUCKETS,
    COLD_THRESHOLD,
    SUFFICIENT_THRESHOLD,
    build_shadow_evaluation_report,
    evaluate_actual_outcome,
    evaluate_agreement,
    evaluate_calibration,
    evaluate_coverage,
    evaluate_data_sufficiency,
    evaluate_disagreement,
    evaluate_etqs_approximation,
    evaluate_predicted_uplift,
    sufficiency_status,
)
from .shadow_render import render_shadow_report
from .shadow_rows import (
    ShadowEvaluationFilters,
    build_evaluation_row,
    collect_evaluation_rows,
    extract_prediction,
)


def run_shadow_evaluation(
    db_path: Optional[Path] = None,
    filters: Optional[ShadowEvaluationFilters] = None,
) -> Dict[str, Any]:
    """Collect rows and build the report in one read-only call."""
    active = filters or ShadowEvaluationFilters()
    rows = collect_evaluation_rows(
        db_path,
        node=active.node,
        task_type=active.task_type,
        agent=active.agent,
        since=active.since,
        before=active.before,
        limit=active.limit,
    )
    report = build_shadow_evaluation_report(rows)
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
