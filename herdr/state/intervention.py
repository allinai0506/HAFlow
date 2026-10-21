"""intervention persistence; extracted without changing behavior."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from ._facade import _facade


def _decode_intervention_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "intervention_id": row["intervention_id"],
        "identity_key": row["identity_key"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "task_id": row["task_id"],
        "evaluation_id": row["evaluation_id"],
        "decision_id": row["decision_id"],
        "action": row["action"],
        "reason": row["reason"],
        "finding_refs": json.loads(row["finding_refs_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "status": row["status"],
        "attempt": row["attempt"],
        "max_attempts": row["max_attempts"],
        "requested_at": row["requested_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "execution_owner": row["execution_owner"],
        "lease_until": row["lease_until"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
    }

def _record_intervention_event(conn: sqlite3.Connection, item: Dict[str, Any], event_type: str) -> None:
    _facade().record_event(
        {
            "workflow_id": item.get("workflow_id"),
            "task_id": item["task_id"],
            "event_type": event_type,
            "payload": item,
            "source": "intervention",
            "run_id": item["run_id"],
        },
        conn=conn,
    )

def create_intervention(
    intervention: Dict[str, Any], db_path: Optional[Path] = None,
    *, verification_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Create or return the canonical Intervention under one SQLite write lock.

    When ``verification_limit`` is supplied, the VERIFY budget check and the
    identity insert happen under this same SQLite write transaction.
    """
    from herdr.intervention import ACTION_VERIFY, Intervention, STATUS_REQUESTED

    item = Intervention.from_mapping(intervention).to_mapping()
    if item["status"] != STATUS_REQUESTED:
        raise ValueError("new intervention must be requested")
    if not item["intervention_id"]:
        item["intervention_id"] = str(uuid.uuid4())
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        existing = conn.execute(
            "SELECT * FROM interventions WHERE identity_key = ?", (item["identity_key"],)
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT;")
            return _decode_intervention_row(existing)
        budget_exhausted = False
        if item["action"] == ACTION_VERIFY and verification_limit is not None:
            statuses = ("requested", "running", "completed", "failed")
            placeholders = ",".join("?" for _ in statuses)
            count = conn.execute(
                f"""SELECT COUNT(*) FROM interventions
                    WHERE run_id = ? AND task_id = ? AND action = ?
                      AND status IN ({placeholders})""",
                (item["run_id"], item["task_id"], ACTION_VERIFY, *statuses),
            ).fetchone()[0]
            budget_exhausted = int(count) >= int(verification_limit)
        inserted = conn.execute(
            """
            INSERT INTO interventions (
                intervention_id, identity_key, run_id, workflow_id, task_id,
                evaluation_id, decision_id, action, reason, finding_refs_json,
                evidence_refs_json, status, attempt, max_attempts, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO NOTHING
            """,
            (
                item["intervention_id"], item["identity_key"], item["run_id"],
                item["workflow_id"], item["task_id"], item["evaluation_id"],
                item["decision_id"], item["action"], item["reason"],
                json.dumps(item["finding_refs"], ensure_ascii=False),
                json.dumps(item["evidence_refs"], ensure_ascii=False),
                item["status"], item["attempt"], item["max_attempts"],
                item["requested_at"] or time.time(),
            ),
        ).rowcount
        row = conn.execute(
            "SELECT * FROM interventions WHERE identity_key = ?", (item["identity_key"],)
        ).fetchone()
        if row is None:
            raise RuntimeError("intervention insert did not produce a canonical row")
        canonical = _decode_intervention_row(row)
        if inserted:
            _record_intervention_event(conn, canonical, "intervention_requested")
        if budget_exhausted:
            error = {
                "code": "verification_budget_exhausted",
                "verification_count": int(count),
                "max_verifications": int(verification_limit),
            }
            finished_at = time.time()
            conn.execute(
                """UPDATE interventions SET status = ?, finished_at = ?, error_json = ?
                   WHERE intervention_id = ? AND status = ?""",
                (
                    "failed", finished_at, json.dumps(error, ensure_ascii=False),
                    canonical["intervention_id"], STATUS_REQUESTED,
                ),
            )
            canonical = _decode_intervention_row(conn.execute(
                "SELECT * FROM interventions WHERE intervention_id = ?",
                (canonical["intervention_id"],),
            ).fetchone())
            _record_intervention_event(conn, canonical, "intervention_failed")
        conn.execute("COMMIT;")
        return canonical
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def get_intervention(intervention_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        return _decode_intervention_row(row) if row else None
    finally:
        conn.close()

def list_interventions(
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    statuses: Optional[List[str]] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM interventions WHERE 1=1"
        params: List[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        query += " ORDER BY requested_at DESC, intervention_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_intervention_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()

def claim_intervention(
    intervention_id: str,
    db_path: Optional[Path] = None,
    *,
    execution_owner: Optional[str] = None,
    lease_seconds: float = 300.0,
    recover_running: bool = False,
) -> Optional[Dict[str, Any]]:
    from herdr.intervention import STATUS_REQUESTED, STATUS_RUNNING

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK;")
            return None
        now = time.time()
        if row["status"] == STATUS_REQUESTED:
            owner = execution_owner or str(uuid.uuid4())
            conn.execute(
                """UPDATE interventions
                   SET status = ?, started_at = ?, execution_owner = ?, lease_until = ?
                   WHERE intervention_id = ? AND status = ?""",
                (STATUS_RUNNING, now, owner, now + float(lease_seconds),
                 intervention_id, STATUS_REQUESTED),
            )
        elif (
            row["status"] == STATUS_RUNNING
            and recover_running
            and float(row["lease_until"] or 0.0) <= now
        ):
            owner = execution_owner or str(uuid.uuid4())
            conn.execute(
                """UPDATE interventions
                   SET execution_owner = ?, lease_until = ?
                   WHERE intervention_id = ? AND status = ?
                     AND COALESCE(lease_until, 0) <= ?""",
                (owner, now + float(lease_seconds), intervention_id,
                 STATUS_RUNNING, now),
            )
        else:
            conn.execute("ROLLBACK;")
            return None
        updated = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        item = _decode_intervention_row(updated)
        _record_intervention_event(conn, item, "intervention_started")
        conn.execute("COMMIT;")
        return item
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def complete_intervention(
    intervention_id: str, result: Dict[str, Any], db_path: Optional[Path] = None,
    *, execution_owner: Optional[str] = None,
) -> Dict[str, Any]:
    return _finish_intervention(intervention_id, "completed", result=result, db_path=db_path,
                                allowed=("running",), execution_owner=execution_owner)

def fail_intervention(
    intervention_id: str, error: Dict[str, Any], db_path: Optional[Path] = None,
    *, execution_owner: Optional[str] = None,
) -> Dict[str, Any]:
    return _finish_intervention(
        intervention_id, "failed", error=error, db_path=db_path,
        allowed=("requested", "running"), execution_owner=execution_owner,
    )

def _finish_intervention(
    intervention_id: str, status: str, *, result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None, db_path: Optional[Path], allowed: tuple,
    execution_owner: Optional[str] = None,
) -> Dict[str, Any]:
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"intervention '{intervention_id}' not found")
        if row["status"] not in allowed:
            raise ValueError(f"cannot finish intervention from {row['status']}")
        finished_at = time.time()
        if execution_owner is None:
            owner_clause = ""
            owner_params = ()
        else:
            owner_clause = " AND execution_owner = ?"
            owner_params = (execution_owner,)
        updated = conn.execute(
            f"""UPDATE interventions SET status = ?, finished_at = ?,
                   result_json = ?, error_json = ?, execution_owner = NULL,
                   lease_until = NULL
               WHERE intervention_id = ? AND status IN ({','.join('?' for _ in allowed)})
               {owner_clause}""",
            (
                status, finished_at,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                json.dumps(error, ensure_ascii=False) if error is not None else None,
                intervention_id, *allowed, *owner_params,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError(f"intervention '{intervention_id}' execution ownership changed")
        updated = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        item = _decode_intervention_row(updated)
        _record_intervention_event(
            conn, item, "intervention_completed" if status == "completed" else "intervention_failed"
        )
        conn.execute("COMMIT;")
        return item
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()
