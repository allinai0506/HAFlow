#!/usr/bin/env python3
"""Immutable Agent Execution Outcome Fact Layer (herdr/execution_outcome.py).

Write-time truth, not query-time reconstruction.

Every settled Agent run produces exactly one AgentExecutionOutcome row at
the moment its result becomes provable (terminal task + complete, owned
eval). The row is never updated afterwards; later task mutations, newer
eval revisions, or other runs' events cannot change it.

Responsibilities:
- resolve_execution_outcome: pure function (task + eval -> outcome
  candidate). Shared by live finalization and backfill; there is no
  second history-judgment algorithm.
- finalize_execution_outcome: canonical choke point. Validates identity,
  checks readiness, performs the immutable insert (idempotent).
- Readiness is fail-closed: any unknown key fact (eval missing,
  incomplete, or unattributable) yields no outcome, never a guess.

The Adaptive Router reads only agent_execution_outcomes and must never
reconstruct history from mutable tasks / eval revisions / status_history.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import state_db
from .transitions import COMPLETED_TASK_STATUSES, TERMINAL_TASK_STATUSES

OUTCOME_SCHEMA_VERSION = state_db.OUTCOME_SCHEMA_VERSION


def outcome_id_for(task_id: str, run_id: str) -> str:
    """Deterministic, unambiguous outcome identity for (task_id, run_id).

    Plain f"outcome_{task_id}_{run_id}" is ambiguous: ("a_b", "c") and
    ("a", "b_c") collide. JSON canonical encoding keeps the pair
    boundary; sha256 keeps the PRIMARY KEY fixed-length.
    """
    identity = json.dumps(
        [str(task_id), str(run_id)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"outcome_{digest}"

#: Eval columns needed to settle one outcome (never SELECT *; the set of
#: facts the resolver depends on stays explicit and reviewable).
_EVAL_FACT_COLUMNS = (
    "eval_id, run_id, revision, requirements_satisfied, "
    "verification_passed, human_intervention_count, final_status, "
    "task_id, workflow_id, created_at"
)


def compute_qualified_success(
    requirements_satisfied: Optional[bool],
    verification_passed: Optional[bool],
    final_status: Optional[str],
) -> Optional[bool]:
    """Single authoritative Qualified Success computation.

    None when any input fact is unknown; the router must read the
    persisted outcome value and never run a second version of this.
    """
    if requirements_satisfied is None or verification_passed is None:
        return None
    if not final_status:
        return None
    return (
        bool(requirements_satisfied)
        and bool(verification_passed)
        and str(final_status) in COMPLETED_TASK_STATUSES
    )


def _as_bool(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    return bool(raw)


def _count_status_entries(history: Any, name: str) -> int:
    """Count visits to one state-machine status in a frozen history list."""
    if not isinstance(history, list):
        return 0
    count = 0
    for entry in history:
        if isinstance(entry, dict):
            status = entry.get("to")
        else:
            status = entry
        if status is not None and str(status) == name:
            count += 1
    return count


def _wall_time(task: Dict[str, Any]) -> Tuple[Any, Any, Optional[float]]:
    """Freeze (started, finished, wall) or leave wall NULL, never guessed.

    Only task-owned started_at/finished_at count. A missing endpoint or a
    negative/non-finite delta yields wall_time_seconds=None; mutable
    updated_at is never substituted as a completion time.
    """
    started = task.get("started_at")
    finished = task.get("finished_at")
    try:
        if started is None or finished is None:
            return started, finished, None
        wall = float(finished) - float(started)
        if wall != wall or wall == float("inf") or wall == float("-inf"):
            return started, finished, None
        if wall < 0:
            return started, finished, None
        return started, finished, wall
    except (TypeError, ValueError):
        return task.get("started_at"), task.get("finished_at"), None


def _decode_eval_fact(row: Any) -> Dict[str, Any]:
    return {
        "eval_id": row["eval_id"],
        "run_id": row["run_id"],
        "revision": int(row["revision"]),
        "requirements_satisfied": _as_bool(row["requirements_satisfied"]),
        "verification_passed": _as_bool(row["verification_passed"]),
        "human_intervention_count": row["human_intervention_count"],
        "final_status": row["final_status"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "created_at": float(row["created_at"]),
    }


def _run_sibling_count(run_id: str, task_id: str, db_path: Optional[Path]) -> int:
    """Count other tasks claiming the same payload run_id (write path only)."""
    conn = state_db.get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE COALESCE(NULLIF(CASE WHEN json_valid(payload_json) "
            "THEN json_extract(payload_json, '$.run_id') END, ''), '') = ? "
            "AND task_id <> ?",
            (str(run_id), str(task_id)),
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def _latest_owned_eval(
    run_id: str,
    task_id: str,
    workflow_id: str,
    *,
    before: float,
    db_path: Optional[Path],
) -> Optional[Dict[str, Any]]:
    """Latest eval before ``before`` attributable to this task, else None.

    Ownership is proven per row: a task-specific eval must match run_id,
    task_id, AND explicit workflow_id; a taskless (legacy, task_id IS NULL)
    eval counts only when no sibling task claims the same run_id AND its
    explicit workflow_id matches. Newest non-owned revisions are skipped,
    never borrowed. Unknown workflow_id never proves attribution.
    """
    expected_wf = str(workflow_id or "")
    if not expected_wf:
        return None
    conn = state_db.get_db_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_EVAL_FACT_COLUMNS} FROM eval_results "
            "WHERE run_id = ? AND created_at < ? ORDER BY revision DESC",
            (str(run_id), float(before)),
        ).fetchall()
    finally:
        conn.close()
    sole_owner: Optional[bool] = None
    for row in rows:
        fact = _decode_eval_fact(row)
        if str(fact.get("workflow_id") or "") != expected_wf:
            continue
        owner = fact["task_id"]
        if owner:
            if str(owner) == str(task_id) and str(fact["run_id"]) == str(run_id):
                return fact
            continue
        if sole_owner is None:
            sole_owner = _run_sibling_count(run_id, task_id, db_path) == 0
        if sole_owner and str(fact["run_id"]) == str(run_id):
            return fact
    return None


def resolve_execution_outcome(
    task: Dict[str, Any],
    eval_fact: Optional[Dict[str, Any]],
    *,
    finalized_at: float,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Pure resolver: task + owned eval -> immutable outcome candidate.

    Returns (outcome_dict, "ready") or (None, reason). No I/O, no SQL,
    no guessing: any unknown key fact yields a reason, never a row.
    """
    task_id = str(task.get("task_id") or "")
    run_id = str(task.get("run_id") or "")
    if not task_id or not run_id:
        return None, "identity_incomplete"
    agent = str(task.get("agent") or "")
    node = str(task.get("node") or task.get("stage") or "")
    workflow_id = str(task.get("workflow_id") or "")
    if not agent or not node or not workflow_id:
        return None, "identity_incomplete"
    if str(task.get("status") or "") not in TERMINAL_TASK_STATUSES:
        return None, "task_not_terminal"
    if eval_fact is None:
        return None, "eval_missing"
    requirements = eval_fact.get("requirements_satisfied")
    verification = eval_fact.get("verification_passed")
    final_status = eval_fact.get("final_status")
    if requirements is None or verification is None or not final_status:
        return None, "eval_incomplete"
    verdict = compute_qualified_success(requirements, verification, str(final_status))
    if verdict is None:  # defensive; inputs checked above
        return None, "eval_incomplete"
    started, finished, wall = _wall_time(task)
    human_raw = eval_fact.get("human_intervention_count")
    if human_raw is None or isinstance(human_raw, bool):
        return None, "eval_incomplete"
    try:
        human = int(human_raw)
    except (TypeError, ValueError):
        return None, "eval_incomplete"
    if human < 0:
        return None, "eval_incomplete"
    history = task.get("status_history")
    if not isinstance(history, list):
        return None, "history_missing"
    try:
        version = int(task.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    outcome = {
        "outcome_id": outcome_id_for(task_id, run_id),
        "run_id": run_id,
        "task_id": task_id,
        "workflow_id": workflow_id,
        "agent": agent,
        "node": node,
        "task_type": str(task.get("task_type") or ""),
        "started_at": started,
        "finished_at": finished,
        "wall_time_seconds": wall,
        "final_status": str(final_status),
        "requirements_satisfied": bool(requirements),
        "verification_passed": bool(verification),
        "qualified_success": bool(verdict),
        "rework_count": _count_status_entries(history, "rework"),
        "blocked_count": _count_status_entries(history, "blocked"),
        "human_intervention_count": human,
        "recorded_at": float(finalized_at),
        "source_eval_id": str(eval_fact.get("eval_id")),
        "source_eval_revision": int(eval_fact.get("revision")),
        "source_task_version": version,
        "schema_version": OUTCOME_SCHEMA_VERSION,
    }
    return outcome, "ready"


def finalize_execution_outcome(
    task_id: str,
    *,
    db_path: Optional[Path] = None,
    finalized_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Canonical choke point: settle one task's outcome exactly once.

    Idempotent and crash-safe: an existing (task_id, run_id) row is
    returned as-is ("exists"); a repeat call after a crash mid-write
    completes the insert via ON CONFLICT DO NOTHING + re-read.
    """
    now = float(finalized_at) if finalized_at is not None else time.time()
    task = state_db.get_task(str(task_id), db_path)
    if task is None:
        return {"status": "not_ready", "reason": "task_missing", "outcome": None}
    run_id = str(task.get("run_id") or "")
    if run_id:
        existing = state_db.get_execution_outcome(str(task_id), run_id, db_path)
        if existing is not None:
            return {"status": "exists", "reason": "already_settled", "outcome": existing}
    eval_fact = (
        _latest_owned_eval(
            run_id,
            str(task_id),
            str(task.get("workflow_id") or ""),
            before=now,
            db_path=db_path,
        )
        if run_id
        else None
    )
    outcome, reason = resolve_execution_outcome(task, eval_fact, finalized_at=now)
    if outcome is None:
        return {"status": "not_ready", "reason": reason, "outcome": None}
    stored = state_db.insert_execution_outcome(outcome, db_path=db_path)
    return {"status": "created", "reason": "ready", "outcome": stored}


def try_autofinalize_for_task(
    task_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort hook for write paths (save_task / record_eval_result).

    Never raises: finalization must not break task persistence or eval
    recording. Returns the outcome dict when one is settled or already
    exists, else None.
    """
    if not state_db._outcome_autofinalize_enabled():
        return None
    try:
        result = finalize_execution_outcome(str(task_id), db_path=db_path)
    except Exception:
        return None
    return result.get("outcome")


def get_execution_outcome(
    task_id: str,
    run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read one settled outcome by identity."""
    return state_db.get_execution_outcome(task_id, run_id, db_path)


def backfill_execution_outcomes(
    *,
    db_path: Optional[Path] = None,
    finalized_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Best-effort backfill over terminal tasks using the same resolver.

    Live finalization and backfill share resolve_execution_outcome; there
    is no second history-judgment algorithm. Unprovable rows are skipped
    (never guessed) and reported under ``skipped`` with per-reason counts.
    """
    now = float(finalized_at) if finalized_at is not None else time.time()
    conn = state_db.get_db_connection(db_path)
    try:
        placeholders = ", ".join(["?"] * len(TERMINAL_TASK_STATUSES))
        rows = conn.execute(
            f"SELECT task_id FROM tasks WHERE status IN ({placeholders})",
            tuple(sorted(TERMINAL_TASK_STATUSES)),
        ).fetchall()
        task_ids = [str(row["task_id"]) for row in rows]
    finally:
        conn.close()
    tallies: Dict[str, Any] = {
        "scanned": len(task_ids),
        "created": 0,
        "exists": 0,
        "skipped": 0,
        "skip_reasons": {},
    }
    for task_id in task_ids:
        try:
            result = finalize_execution_outcome(
                task_id, db_path=db_path, finalized_at=now
            )
        except Exception as exc:  # noqa: BLE001 -- backfill reports, never crashes
            result = {"status": "not_ready", "reason": f"error:{type(exc).__name__}"}
        status = result.get("status")
        if status == "created":
            tallies["created"] += 1
        elif status == "exists":
            tallies["exists"] += 1
        else:
            tallies["skipped"] += 1
            reason = str(result.get("reason") or "unknown")
            reasons = tallies["skip_reasons"]
            reasons[reason] = int(reasons.get(reason) or 0) + 1
    return tallies


__all__ = [
    "OUTCOME_SCHEMA_VERSION",
    "backfill_execution_outcomes",
    "compute_qualified_success",
    "finalize_execution_outcome",
    "get_execution_outcome",
    "outcome_id_for",
    "resolve_execution_outcome",
    "try_autofinalize_for_task",
]
