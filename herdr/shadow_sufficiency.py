#!/usr/bin/env python3
"""Shadow evaluation sufficiency layer (herdr/shadow_sufficiency.py).

Model evidence vs evaluation evidence per agent x node x task_type.
Pure functions over rows built by ``herdr.shadow_rows``; no I/O.

A bucket never conflates the two sides: the router may have predicted
with deep history (model sufficient) while the evaluation window holds
almost no calibrated outcomes (evaluation cold), or vice versa.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .shadow_rows import _predicted_probability

#: Data-sufficiency cutoffs on settled samples per agent x node x task_type.
COLD_THRESHOLD = 10
SUFFICIENT_THRESHOLD = 30


def sufficiency_status(sample_count: int) -> str:
    """Bucket a settled-sample count: cold / warming / sufficient."""
    n = int(sample_count)
    if n < COLD_THRESHOLD:
        return "cold"
    if n < SUFFICIENT_THRESHOLD:
        return "warming"
    return "sufficient"


def _frozen_int(prediction: Dict[str, Any], key: str) -> int:
    try:
        return int(prediction.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _frozen_float(prediction: Dict[str, Any], key: str) -> float:
    try:
        return float(prediction.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evaluate_data_sufficiency(
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Model evidence vs evaluation evidence per agent x node x task_type.

    Two deliberately separate concepts share one bucket list:

    - Model evidence (``model_sample_count`` / ``model_confidence``):
      the frozen history size the router actually predicted with, taken
      as the max over every candidate's frozen ranking entry in the
      evaluated window. Answers "did the model have history?".
    - Evaluation evidence (``evaluation_sample_count`` /
      ``calibration_sample_count``): settled actual outcomes, and the
      subset pairing a frozen actual prediction with a settled
      outcome. Answers "can we calibrate the prediction?".

    Each side gets its own cold (<10) / warming (10-29) / sufficient
    (>=30) status; a missing side reports ``None`` / ``"unknown"``,
    never a guess. Buckets union every agent seen in frozen rankings
    or in settled outcomes.
    """
    model: Dict[Tuple[str, str, str], Dict[str, float]] = {}
    for row in rows:
        node = str(row.get("node") or "")
        task_type = str(row.get("task_type") or "")
        evidence = row.get("model_evidence")
        if not isinstance(evidence, list):
            continue
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            agent = str(entry.get("agent") or "")
            if not agent:
                continue
            cell = model.setdefault((agent, node, task_type), {
                "sample_count": 0,
                "confidence": 0.0,
            })
            cell["sample_count"] = max(
                cell["sample_count"], _frozen_int(entry, "sample_count")
            )
            cell["confidence"] = max(
                cell["confidence"], _frozen_float(entry, "confidence")
            )
    evaluated: Dict[Tuple[str, str, str], Dict[str, int]] = {}
    calibrated: Dict[Tuple[str, str, str], int] = {}
    for row in rows:
        if not row.get("actual_outcome"):
            continue
        key = (
            str(row.get("actual_agent") or ""),
            str(row.get("node") or ""),
            str(row.get("task_type") or ""),
        )
        if not key[0]:
            continue
        cell = evaluated.setdefault(key, {"n": 0})
        cell["n"] += 1
        if _predicted_probability(row.get("actual_prediction")) is not None:
            calibrated[key] = calibrated.get(key, 0) + 1
    buckets = []
    for key in sorted(set(model) | set(evaluated)):
        agent, node, task_type = key
        frozen = model.get(key)
        n_eval = evaluated.get(key, {"n": 0})["n"]
        n_calib = calibrated.get(key, 0)
        if frozen is None:
            model_n: Optional[int] = None
            model_c: Optional[float] = None
            model_status = "unknown"
        else:
            model_n = int(frozen["sample_count"])
            model_c = round(float(frozen["confidence"]), 6)
            model_status = sufficiency_status(model_n)
        buckets.append({
            "bucket_key": f"{agent}/{node}/{task_type}",
            "agent": agent,
            "node": node,
            "task_type": task_type,
            "model_sample_count": model_n,
            "model_confidence": model_c,
            "model_data_status": model_status,
            "evaluation_sample_count": n_eval,
            "calibration_sample_count": n_calib,
            "evaluation_data_status": sufficiency_status(n_calib),
        })
    return buckets


__all__ = [
    "COLD_THRESHOLD",
    "SUFFICIENT_THRESHOLD",
    "evaluate_data_sufficiency",
    "sufficiency_status",
]
