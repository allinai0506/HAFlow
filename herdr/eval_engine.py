"""Eval Engine: read-only run evaluation plus factual compare (herdr/eval_engine.py).

Reads Task/Run authority, strict verification facts, and owned steering
facts. Unknown stays null; the engine never guesses. Only ``record_run_eval`` writes, delegating to
``eval_store`` idempotency on (run_id, revision).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from . import eval_store, state_db
from .trajectory import run_id_for_task
from .transitions import COMPLETED_TASK_STATUSES


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
    if task is not None:
        task_status = task.get("status")
        task_completed = task_status in COMPLETED_TASK_STATUSES
        if not task_completed and task_status != "failed":
            warnings.append("run_incomplete")

    verification = eval_store.latest_run_verification_fact(run_id, db_path=db_path)
    if verification is None and "insufficient_verification" not in warnings:
        warnings.append("insufficient_verification")

    human_intervention_count: int | None = None
    if task is not None:
        try:
            items = state_db.list_steers(task_id=str(task["task_id"]), db_path=db_path)
        except sqlite3.Error:
            warnings.append("steering_unavailable")
        else:
            human_intervention_count = sum(
                1 for item in items if (item.get("operator") or "human") == "human")

    evidence: list[dict[str, Any]] = []
    if task is not None:
        evidence.append({"kind": "task_status", "ref": task["task_id"]})
    if verification is not None:
        evidence.append({"kind": "verification", "ref": verification.get("event_id")})
    verification_passed: bool | None = None
    if verification is not None:
        passed_flag = verification.get("passed")
        verification_passed = passed_flag if isinstance(passed_flag, bool) else None
    final_status: str | None = task_status if task is not None else None
    requirements_satisfied: bool | None = None
    if task_completed and verification_passed is not None:
        requirements_satisfied = verification_passed

    return {
        "run_id": run_id,
        "task_id": task["task_id"] if task is not None else None,
        "workflow_id": workflow_id,
        "requirements_satisfied": requirements_satisfied,
        "verification_passed": verification_passed,
        "human_intervention_count": human_intervention_count,
        "final_status": final_status,
        "evidence": evidence,
        "warnings": warnings,
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
        evidence=result["evidence"],
        eval_id=eval_id,
        db_path=_resolve_db_path(db_path, store),
        requirements_satisfied=result.get("requirements_satisfied"),
        verification_passed=result.get("verification_passed"),
        human_intervention_count=result.get("human_intervention_count"),
        final_status=result.get("final_status"),
        warnings=result.get("warnings"),
        task_id=result.get("task_id"),
        workflow_id=result.get("workflow_id"),
    )


def _brief(row: dict[str, Any] | None) -> dict[str, Any] | None:
    row = row or {}
    return {
        "requirements_satisfied": row.get("requirements_satisfied"),
        "verification_passed": row.get("verification_passed"),
        "human_intervention_count": row.get("human_intervention_count"),
        "final_status": row.get("final_status"),
    }


def compare_evals(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the four factual values on each side; null means unknown."""
    return {"before": _brief(before), "after": _brief(after)}


__all__ = [
    "compare_evals",
    "evaluate_run",
    "record_run_eval",
]
