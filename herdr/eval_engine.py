"""Eval Engine: read-only run evaluation plus factual compare (herdr/eval_engine.py).

Reads Task/Run authority, strict verification facts, and owned steering
facts, then derives a conservative verdict. Unknown stays null; the
engine never guesses. Only ``record_run_eval`` writes, delegating to
``eval_store`` idempotency on (run_id, revision).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import eval_store, state_db
from .trajectory import run_id_for_task
from .transitions import COMPLETED_TASK_STATUSES


def normalize_stage_verdict(value: Any) -> str | None:
    """Map empty markers to None; pass through real verdict strings."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value  # type: ignore[return-value]


def _resolve_db_path(db_path: Path | None, store: Any) -> Path | None:
    if db_path is not None:
        return db_path
    return getattr(store, "db_path", None)


def _owned_task(run_id: str, facts: dict[str, Any], db_path: Path | None) -> dict[str, Any] | None:
    task_id = facts.get("task_id")
    if not task_id:
        return None
    try:
        task = state_db.get_task(str(task_id), db_path=db_path)
    except sqlite3.Error:
        return None
    if task is None:
        return None
    try:
        if str(run_id_for_task(task)) != str(run_id):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return task


def evaluate_run(
    run_id: str,
    *,
    db_path: Path | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Evaluate one run without writing state. Unknown facts stay null."""
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    db_path = _resolve_db_path(db_path, store)

    facts = state_db.aggregate_run_metric_rows(run_id, db_path=db_path)
    task = _owned_task(run_id, facts, db_path)
    warnings: list[str] = []
    if task is None:
        if facts.get("task_id"):
            warnings.append("task_ownership_mismatch")
        else:
            warnings.append("unknown_task")

    workflow: dict[str, Any] | None = None
    workflow_id: str | None = None
    if task is not None:
        workflow_id = task.get("workflow_id")
        try:
            workflow = state_db.get_workflow(str(workflow_id), db_path=db_path)
        except sqlite3.Error:
            workflow = None
        if workflow is None:
            workflow_id = task.get("workflow_id")

    task_status: str | None = None
    task_completed = False
    observed: str | None = None
    if task is not None:
        task_status = task.get("status")
        task_completed = task_status in COMPLETED_TASK_STATUSES
        if not task_completed:
            warnings.append("run_incomplete")
        observed = normalize_stage_verdict(task.get("stage_verdict"))

    verification = eval_store.latest_run_verification_fact(run_id, db_path=db_path)
    if verification is None and "insufficient_verification" not in warnings:
        warnings.append("insufficient_verification")

    steering: dict[str, Any] = {"total": 0, "human": 0, "task_id": None}
    if task is not None:
        try:
            items = state_db.list_steers(task_id=str(task["task_id"]), db_path=db_path)
        except sqlite3.Error:
            items = []
        human = sum(1 for item in items if (item.get("operator") or "human") == "human")
        steering = {"total": len(items), "human": human, "task_id": task["task_id"]}

    override: dict[str, Any] | None = None
    if task is not None and workflow is not None:
        node = task.get("node") or task.get("stage")
        overrides = workflow.get("gate_overrides") or {}
        if isinstance(overrides, dict) and node in overrides and isinstance(overrides[node], dict):
            entry = overrides[node]
            override = {
                "node": node,
                "verdict": entry.get("verdict"),
                "operator": entry.get("operator"),
                "timestamp": entry.get("timestamp"),
            }

    verdict: str | None = None
    if task is not None and task_completed and verification is not None:
        passed = verification.get("passed")
        if passed is True and observed in (None, "pass"):
            verdict = "pass"
        elif passed is False or observed == "blocked":
            verdict = "fail"
            if passed is True and observed == "blocked":
                warnings.append("conflicting_facts")
        else:
            warnings.append("unexpected_stage_verdict")

    evidence: list[dict[str, Any]] = []
    if task is not None:
        evidence.append({"kind": "task_status", "ref": task["task_id"], "status": task_status})
    if verification is not None:
        evidence.append({"kind": "verification", "ref": verification.get("event_id")})
    if observed is not None:
        evidence.append({"kind": "stage_verdict", "ref": observed})
    if steering["total"]:
        evidence.append({"kind": "steering", "ref": f"{steering['task_id']}:{steering['total']}"})
    if override is not None:
        evidence.append({"kind": "gate_override", "ref": override["node"]})

    scores: dict[str, float] | None = None
    if verification is not None:
        scores = {"verification_passed": 1.0 if verification.get("passed") is True else 0.0}

    return {
        "run_id": run_id,
        "task_id": task["task_id"] if task is not None else None,
        "workflow_id": workflow_id,
        "verdict": verdict,
        "scores": scores,
        "evidence": evidence,
        "warnings": warnings,
        "task_status": task_status,
        "task_completed": task_completed,
        "verification": verification,
        "steering": steering,
        "observed_stage_verdict": observed,
        "override": override,
    }


def record_run_eval(
    run_id: str,
    *,
    revision: int | None = None,
    db_path: Path | None = None,
    store: Any | None = None,
    eval_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate then persist one eval fact via the idempotent store."""
    result = evaluate_run(run_id, db_path=db_path, store=store)
    return eval_store.record_eval_result(
        result["run_id"],
        revision=revision,
        verdict=result["verdict"],
        scores=result["scores"],
        evidence=result["evidence"],
        eval_id=eval_id,
        db_path=_resolve_db_path(db_path, store),
    )


def _brief(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "run_id": row.get("run_id"),
        "revision": row.get("revision"),
        "verdict": row.get("verdict"),
    }


def compare_evals(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    """Diff two eval rows factually; either side may be null."""
    before_verdict = (before or {}).get("verdict")
    after_verdict = (after or {}).get("verdict")
    before_scores = (before or {}).get("scores")
    after_scores = (after or {}).get("scores")

    def _refs(row: dict[str, Any] | None) -> set[str]:
        items: set[str] = set()
        for entry in (row or {}).get("evidence") or []:
            if isinstance(entry, dict):
                items.add(f"{entry.get('kind')}:{entry.get('ref')}")
        return items

    before_refs = _refs(before)
    after_refs = _refs(after)
    before_warnings = set((before or {}).get("warnings") or [])
    after_warnings = set((after or {}).get("warnings") or [])

    return {
        "before": _brief(before),
        "after": _brief(after),
        "verdict_changed": before_verdict != after_verdict,
        "verdict_transition": f"{before_verdict or 'null'}->{after_verdict or 'null'}",
        "scores_changed": before_scores != after_scores,
        "evidence_added": sorted(after_refs - before_refs),
        "evidence_removed": sorted(before_refs - after_refs),
        "warnings_added": sorted(after_warnings - before_warnings),
        "warnings_removed": sorted(before_warnings - after_warnings),
    }


__all__ = [
    "compare_evals",
    "evaluate_run",
    "normalize_stage_verdict",
    "record_run_eval",
]
