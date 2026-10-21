"""context pack persistence; extracted without changing behavior."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from ._facade import _facade


def _decode_context_pack_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "context_id": row["context_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "goal": row["goal"],
        "current_state": json.loads(row["current_state_json"] or "{}"),
        "completed": json.loads(row["completed_json"] or "[]"),
        "verified_facts": json.loads(row["verified_facts_json"] or "[]"),
        "important_findings": json.loads(row["important_findings_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "artifact_refs": json.loads(row["artifact_refs_json"] or "[]"),
        "open_issues": json.loads(row["open_issues_json"] or "[]"),
        "next_focus": json.loads(row["next_focus_json"] or "[]"),
        "source_event_sequence": int(row["source_event_sequence"] or 0),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": float(row["created_at"]),
    }

def _trajectory_rows_in_conn(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    event_type: Optional[str] = None,
    limit: int,
    desc: bool = True,
) -> List[Dict[str, Any]]:
    query = "SELECT * FROM events WHERE run_id = ? AND source = 'trajectory'"
    params: List[Any] = [run_id]
    if event_type is not None:
        query += " AND event_type = ?"
        params.append(event_type)
    direction = "DESC" if desc else "ASC"
    query += f" ORDER BY sequence {direction}, id {direction} LIMIT ?"
    params.append(int(limit))
    return [
        {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "node_id": row["node_id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "source": row["source"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "payload": json.loads(row["payload_json"] or "{}"),
        }
        for row in conn.execute(query, tuple(params)).fetchall()
    ]

def _task_matches_run(task: Optional[Dict[str, Any]], run_id: str) -> bool:
    if not task:
        return False
    try:
        from herdr.trajectory import run_id_for_task
        return str(run_id_for_task(task)) == str(run_id)
    except Exception:
        return False

def read_context_compact_snapshot(
    run_id: str,
    *,
    task: Optional[Dict[str, Any]] = None,
    max_recent_events: int = 100,
    max_findings: int = 10,
    max_observations: int = 20,
    verification_limit: int = 10,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read all compact inputs and their source version in one SQLite snapshot."""
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN;")
        recent_rows = _trajectory_rows_in_conn(
            conn, run_id, limit=max_recent_events, desc=True,
        )
        verification_rows = _trajectory_rows_in_conn(
            conn, run_id, event_type="verification_completed", limit=verification_limit, desc=True,
        )
        all_rows = recent_rows + verification_rows
        requested_task_id = (task or {}).get("task_id")
        if requested_task_id is None:
            requested_task_id = next(
                (row.get("task_id") for row in reversed(all_rows) if row.get("task_id")),
                None,
            )
        selected_task = None
        if requested_task_id:
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (str(requested_task_id),)
            ).fetchone()
            if task_row is not None:
                candidate_task = _facade()._decode_task_row(task_row)
                if _task_matches_run(candidate_task, run_id):
                    selected_task = candidate_task
            elif _task_matches_run(task, run_id):
                selected_task = task
        elif _task_matches_run(task, run_id):
            selected_task = task

        finding_rows = conn.execute(
            """SELECT * FROM trajectory_findings
               WHERE run_id = ?
               ORDER BY CASE severity
                          WHEN 'critical' THEN 3
                          WHEN 'warning' THEN 2
                          ELSE 1
                        END DESC,
                        created_at DESC, rowid DESC
               LIMIT ?""",
            (run_id, int(max_findings)),
        ).fetchall()
        findings = [_facade()._decode_finding_row(row) for row in finding_rows]
        observation_rows = conn.execute(
            """SELECT * FROM observations
               WHERE run_id = ?
               ORDER BY created_at DESC, observation_id DESC
               LIMIT ?""",
            (run_id, int(max_observations)),
        ).fetchall()
        observations = _facade()._compact_observation_metadata(
            [_facade()._decode_observation_row(row) for row in observation_rows]
        )
        observations.reverse()
        sequence_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()
        source_sequence = int(sequence_row["sequence"] or 0)
        source_version = _facade()._context_source_version_in_conn(
            conn,
            run_id,
            (selected_task or {}).get("task_id"),
            max_findings,
            max_observations,
        )
        source_version["trajectory_sequence"] = source_sequence
        latest_row = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        conn.commit()
        return {
            "source_sequence": source_sequence,
            "recent_rows": recent_rows,
            "verification_rows": verification_rows,
            "task": selected_task,
            "findings": findings,
            "observations": observations,
            "source_version": source_version,
            "latest": _decode_context_pack_row(latest_row) if latest_row is not None else None,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def save_context_pack(
    context_pack: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one ContextPack, returning the existing pack on exact fingerprint dedup."""
    required = ("context_id", "run_id")
    if any(not context_pack.get(key) for key in required):
        raise ValueError("context_id and run_id are required")
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        fingerprint = (context_pack.get("metadata") or {}).get("context_source_fingerprint")
        source_version = (context_pack.get("metadata") or {}).get("context_source_version")
        source_version_valid = False
        latest = conn.execute(
            """SELECT * FROM context_packs
               WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (context_pack["run_id"],),
        ).fetchone()
        if isinstance(source_version, dict):
            current_source_version = _facade()._context_source_version_in_conn(
                conn,
                context_pack["run_id"],
                source_version.get("task_id"),
                int(source_version.get("finding_limit") or 0),
                int(source_version.get("observation_limit") or 0),
            )
            if current_source_version != source_version:
                conn.commit()
                if latest is not None:
                    return _decode_context_pack_row(latest)
                raise ValueError("stale ContextPack source version")
            source_version_valid = True
        if latest is not None:
            existing_metadata = json.loads(latest["metadata_json"] or "{}")
            if fingerprint and existing_metadata.get("context_source_fingerprint") == fingerprint:
                # Fingerprint match alone must not preserve a corrupt task_status.
                # The candidate is built by the current cleanup logic (single
                # current status or none); only dedup when latest carries the
                # same task_status values.
                try:
                    latest_facts = json.loads(latest["verified_facts_json"] or "[]")
                except Exception:
                    latest_facts = []
                candidate_facts = context_pack.get("verified_facts") or []
                def _status_values(facts: Any) -> List[Any]:
                    values = []
                    if isinstance(facts, list):
                        for fact in facts:
                            if isinstance(fact, dict) and fact.get("fact_type") == "task_status":
                                values.append(fact.get("status"))
                    return values
                if _status_values(latest_facts) == _status_values(candidate_facts):
                    conn.commit()
                    return _decode_context_pack_row(latest)
            # Compact requests may finish out of order.  The request start
            # timestamp is the snapshot's logical ordering key, so a late
            # older request must never become the latest snapshot.  A later
            # A->B->A request still has a newer timestamp and is appended.
            candidate_created_at = float(context_pack.get("created_at") or time.time())
            latest_created_at = float(latest["created_at"] or 0.0)
            if not source_version_valid and latest_created_at > candidate_created_at:
                conn.commit()
                return _decode_context_pack_row(latest)
        insert_created_at = float(context_pack.get("created_at") or time.time())
        if source_version_valid and latest is not None:
            # Source-version validation, rather than request start time, is
            # authoritative for a candidate that was read after waiting for
            # the writer lock.  Make that accepted snapshot the SQL latest.
            insert_created_at = max(insert_created_at, float(latest["created_at"] or 0.0) + 1e-9)
        conn.execute(
            """INSERT INTO context_packs (
                context_id, run_id, task_id, workflow_id, goal,
                current_state_json, completed_json, verified_facts_json,
                important_findings_json, evidence_refs_json, artifact_refs_json,
                open_issues_json, next_focus_json, source_event_sequence,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                context_pack["context_id"], context_pack["run_id"],
                context_pack.get("task_id"), context_pack.get("workflow_id"), context_pack.get("goal"),
                json.dumps(context_pack.get("current_state") or {}, ensure_ascii=False),
                json.dumps(context_pack.get("completed") or [], ensure_ascii=False),
                json.dumps(context_pack.get("verified_facts") or [], ensure_ascii=False),
                json.dumps(context_pack.get("important_findings") or [], ensure_ascii=False),
                json.dumps(context_pack.get("evidence_refs") or [], ensure_ascii=False),
                json.dumps(context_pack.get("artifact_refs") or [], ensure_ascii=False),
                json.dumps(context_pack.get("open_issues") or [], ensure_ascii=False),
                json.dumps(context_pack.get("next_focus") or [], ensure_ascii=False),
                int(context_pack.get("source_event_sequence") or 0),
                json.dumps(context_pack.get("metadata") or {}, ensure_ascii=False),
                insert_created_at,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM context_packs WHERE context_id = ?", (context_pack["context_id"],)
        ).fetchone()
        if row is None:
            raise RuntimeError("context pack insert was not readable")
        return _decode_context_pack_row(row)
    finally:
        conn.close()

def get_context_pack(
    context_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM context_packs WHERE context_id = ?", (context_id,)).fetchone()
        return _decode_context_pack_row(row) if row is not None else None
    finally:
        conn.close()

def get_latest_context_pack(
    run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""", (run_id,)
        ).fetchone()
        return _decode_context_pack_row(row) if row is not None else None
    finally:
        conn.close()

def list_context_packs(
    run_id: str,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at ASC, rowid ASC""", (run_id,)
        ).fetchall()
        return [_decode_context_pack_row(row) for row in rows]
    finally:
        conn.close()
