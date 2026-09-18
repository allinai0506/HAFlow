#!/usr/bin/env python3
"""SupervisorEvaluation records + deltas (herdr/supervisor/evaluation.py).

Functional Core: one immutable evaluation snapshot per supervision run,
plus trend math against the previous evaluation ("is the agent getting
closer to the goal?"). Persisted as WorkflowEvents by the shell layer -
no schema migration, no Task-top-level dumping.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from ..decision.models import DecisionResult, clamp_probability
from .state import redact_text

EVALUATION_EVENT = "supervisor_evaluation"
POLICY_EVENT = "supervisor_policy"

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


def build_evaluation(
    *,
    task_id: Optional[str],
    workflow_id: Optional[str],
    trigger: str,
    provider: str,
    results: Dict[str, DecisionResult],
    requested_signals: List[str],
    previous: Optional[dict] = None,
    latency_ms: Optional[float] = None,
    fallback_used: bool = False,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Normalize provider results into one SupervisorEvaluation record."""
    timestamp = now if now is not None else time.time()
    signals: Dict[str, float] = {}
    confidences: Dict[str, float] = {}
    for name, result in results.items():
        value = clamp_probability(result.value)
        if value is not None:
            signals[name] = round(value, 4)
        if result.confidence is not None:
            confidences[name] = round(float(result.confidence), 4)

    if error or not signals:
        status = STATUS_FAILED
    elif len(signals) < len(requested_signals):
        status = STATUS_PARTIAL
    else:
        status = STATUS_OK

    certainty = None
    if signals:
        certainty = round(min(abs(2.0 * value - 1.0) for value in signals.values()), 4)

    return {
        "evaluation_id": str(uuid.uuid4()),
        "task_id": task_id,
        "workflow_id": workflow_id,
        "timestamp": timestamp,
        "trigger": trigger,
        "provider": provider,
        "status": status,
        "signals": signals,
        "confidences": confidences,
        "certainty": certainty,
        "deltas": compute_deltas(signals, previous),
        "previous_evaluation_id": (previous or {}).get("evaluation_id"),
        "latency_ms": latency_ms,
        "fallback_used": bool(fallback_used),
        "error": redact_free_error(error),
        "metadata": dict(metadata or {}),
    }


def redact_free_error(error: Optional[str]) -> Optional[str]:
    """Never carry raw provider payloads (may echo headers/keys) into records."""
    if not error:
        return None
    return redact_text(str(error))[:200]


def compute_deltas(
    signals: Dict[str, float],
    previous: Optional[dict],
) -> Dict[str, Dict[str, float]]:
    """current - previous per signal; only for signals present in both."""
    if not isinstance(previous, dict):
        return {}
    previous_signals = previous.get("signals")
    if not isinstance(previous_signals, dict):
        return {}
    deltas: Dict[str, Dict[str, float]] = {}
    for name, value in signals.items():
        before = previous_signals.get(name)
        if isinstance(before, (int, float)):
            deltas[name] = {
                "previous": round(float(before), 4),
                "current": round(float(value), 4),
                "delta": round(float(value) - float(before), 4),
            }
    return deltas


def trend_label(delta: Optional[float], epsilon: float = 0.05) -> str:
    if delta is None:
        return "unknown"
    if delta > epsilon:
        return "rising"
    if delta < -epsilon:
        return "falling"
    return "flat"


def evaluation_deltas(evaluation: dict) -> Dict[str, Dict[str, Any]]:
    """Deltas annotated with a coarse trend label for readability."""
    annotated: Dict[str, Dict[str, Any]] = {}
    for name, entry in (evaluation.get("deltas") or {}).items():
        annotated[name] = dict(entry)
        annotated[name]["trend"] = trend_label(entry.get("delta"))
    return annotated


def latest_evaluation(events: List[dict]) -> Optional[dict]:
    """Most recent supervisor_evaluation payload from a task's event list."""
    best = None
    for event in events or []:
        if not isinstance(event, dict) or event.get("event_type") != EVALUATION_EVENT:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("evaluation_id"):
            if best is None or float(payload.get("timestamp") or 0) >= float(best.get("timestamp") or 0):
                best = payload
    return best


def latest_tests_completed_evidence_id(events: List[dict]) -> Optional[str]:
    """Latest evaluated test evidence_id from supervisor_evaluation events.

    Provides persistent deduplication across Controller restarts by reading
    the task's event ledger for previous tests_completed checkpoints.
    """
    best_ev_id = None
    best_ts = -1.0
    for event in events or []:
        if not isinstance(event, dict) or event.get("event_type") != EVALUATION_EVENT:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("trigger") != "tests_completed":
            continue
        meta = payload.get("metadata") or {}
        ev_id = meta.get("evidence_id") or payload.get("evidence_id")
        if not ev_id:
            continue
        try:
            ts = float(payload.get("timestamp") or event.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= best_ts:
            best_ts = ts
            best_ev_id = str(ev_id)
    return best_ev_id

