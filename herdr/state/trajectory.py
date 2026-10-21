"""trajectory persistence; extracted without changing behavior."""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from ._facade import _facade


def record_trajectory_event(
    event: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one trajectory event with a transaction-local run sequence.

    This is deliberately separate from the legacy WorkflowEvent API. Both use
    the same SQLite event store, but trajectory rows carry the run/sequence
    identity needed for deterministic replay.
    """
    run_id = event.get("run_id")
    event_type = event.get("event_type")
    if not run_id:
        raise ValueError("run_id is required")
    if not event_type:
        raise ValueError("event_type is required")

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        sequence = conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM events
            WHERE run_id = ? AND source = 'trajectory'
            """,
            (run_id,),
        ).fetchone()[0]
        timestamp = float(event["timestamp"]) if event.get("timestamp") is not None else time.time()
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source,
                run_id, sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'trajectory', ?, ?)
            """,
            (
                event.get("workflow_id"),
                event.get("node_id") or event.get("node"),
                event.get("task_id"),
                event.get("agent_id") or event.get("agent"),
                event_type,
                json.dumps(payload, ensure_ascii=False),
                timestamp,
                run_id,
                sequence,
            ),
        )
        conn.execute("COMMIT;")
        return {
            "id": cur.lastrowid,
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": event_type,
            "timestamp": timestamp,
            "source": "trajectory",
            "run_id": run_id,
            "sequence": sequence,
            "payload": payload,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def record_observation_receipt(
    event: Dict[str, Any],
    observation_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically append at most one trajectory receipt for an Observation."""
    if not observation_id:
        raise ValueError("observation_id is required")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    run_id = event.get("run_id")
    if not run_id:
        raise ValueError("run_id is required")

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        timestamp = float(event["timestamp"]) if event.get("timestamp") is not None else time.time()
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO observation_receipts
                (observation_id, run_id, task_id, workflow_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (observation_id, run_id, event.get("task_id"), event.get("workflow_id"), timestamp),
        ).rowcount
        if not inserted:
            conn.execute("COMMIT;")
            return None
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source, run_id, sequence
            ) VALUES (?, ?, ?, ?, 'observation_created', ?, ?, 'trajectory', ?, ?)
            """,
            (
                event.get("workflow_id"), event.get("node_id") or event.get("node"),
                event.get("task_id"), event.get("agent_id") or event.get("agent"),
                json.dumps(payload, ensure_ascii=False), timestamp, run_id, sequence,
            ),
        )
        conn.execute("COMMIT;")
        return {
            "id": cur.lastrowid,
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": "observation_created",
            "timestamp": timestamp,
            "source": "trajectory",
            "run_id": run_id,
            "sequence": sequence,
            "payload": payload,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def list_trajectory_events(
    run_id: str,
    event_type: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: Optional[int] = None,
    desc: bool = False,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read trajectory rows with optional newest-first bounded selection."""
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM events WHERE run_id = ? AND source = 'trajectory'"
        params: List[Any] = [run_id]
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY sequence {direction}, id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        results = []
        for row in conn.execute(query, params).fetchall():
            results.append({
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
            })
        return results
    finally:
        conn.close()

def latest_trajectory_sequence(run_id: str, db_path: Optional[Path] = None) -> int:
    """Read only the run watermark without loading its event history."""
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()
        return int(row["sequence"] or 0)
    finally:
        conn.close()

def trajectory_event_exists(event_id: str, run_id: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """Check one event identity without loading the trajectory window."""
    try:
        row_id = int(str(event_id).removeprefix("evt_"))
    except ValueError:
        return False
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT 1 FROM events WHERE id = ? AND source = 'trajectory'"
        params: List[Any] = [row_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        return conn.execute(query, params).fetchone() is not None
    finally:
        conn.close()

def artifact_ref_exists(ref: str, run_id: str, db_path: Optional[Path] = None) -> bool:
    """Check an artifact reference in trajectory payloads without full history materialization."""
    conn = _facade().get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT payload_json FROM events
               WHERE run_id = ? AND source = 'trajectory'
                 AND event_type = 'artifact_created'
                 AND (payload_json LIKE '%"ref"%' OR payload_json LIKE '%"path"%')""",
            (run_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            artifact = payload.get("artifact") or {}
            if str(artifact.get("ref") or artifact.get("path") or "") == str(ref):
                return True
        return False
    finally:
        conn.close()
