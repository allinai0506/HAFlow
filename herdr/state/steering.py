"""steering persistence; extracted without changing behavior."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from ._facade import _facade


def save_steer(
    steer_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert a steering item."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    sid = steer_dict.get("steer_id")
    tid = steer_dict.get("task_id")
    if not sid or not tid:
        raise ValueError("steer_id and task_id are required")

    wid = steer_dict.get("workflow_id")
    instruction = steer_dict.get("instruction", "")
    operator = steer_dict.get("operator", "human")
    urgent = 1 if steer_dict.get("urgent") else 0
    status = steer_dict.get("status", "pending")
    dispatched_at = steer_dict.get("dispatched_at")
    created_at = float(steer_dict.get("created_at") or time.time())

    payload = {k: v for k, v in steer_dict.items() if k not in {
        "steer_id", "task_id", "workflow_id", "instruction", "operator",
        "urgent", "status", "dispatched_at", "created_at"
    }}
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        conn.execute("""
            INSERT INTO steering_items (
                steer_id, task_id, workflow_id, instruction, operator,
                urgent, status, dispatched_at, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(steer_id) DO UPDATE SET
                task_id=excluded.task_id,
                workflow_id=excluded.workflow_id,
                instruction=excluded.instruction,
                operator=excluded.operator,
                urgent=excluded.urgent,
                status=excluded.status,
                dispatched_at=excluded.dispatched_at,
                payload_json=excluded.payload_json;
        """, (sid, tid, wid, instruction, operator, urgent, status, dispatched_at, created_at, payload_json))
    finally:
        if should_close:
            conn.close()

def get_steer(steer_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a steering item by its steer_id."""
    conn = _facade().get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM steering_items WHERE steer_id = ?", (steer_id,))
        row = cur.fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"] or "{}")
        item = dict(payload)
        item.update({
            "steer_id": row["steer_id"],
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "instruction": row["instruction"],
            "operator": row["operator"],
            "urgent": bool(row["urgent"]),
            "status": row["status"],
            "dispatched_at": row["dispatched_at"],
            "created_at": row["created_at"],
        })
        return item
    finally:
        conn.close()

def list_steers(
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List steering items optionally filtered by task_id and/or status."""
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM steering_items WHERE 1=1"
        params: List[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        cur = conn.execute(query, tuple(params))
        items = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            item = dict(payload)
            item.update({
                "steer_id": row["steer_id"],
                "task_id": row["task_id"],
                "workflow_id": row["workflow_id"],
                "instruction": row["instruction"],
                "operator": row["operator"],
                "urgent": bool(row["urgent"]),
                "status": row["status"],
                "dispatched_at": row["dispatched_at"],
                "created_at": row["created_at"],
            })
            items.append(item)
        return items
    finally:
        conn.close()

def update_steer_status(
    steer_id: str,
    status: str,
    dispatched_at: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Update status and dispatched_at for a steering item."""
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        if dispatched_at is not None:
            cur = conn.execute(
                "UPDATE steering_items SET status = ?, dispatched_at = ? WHERE steer_id = ?",
                (status, dispatched_at, steer_id),
            )
        else:
            cur = conn.execute(
                "UPDATE steering_items SET status = ? WHERE steer_id = ?",
                (status, steer_id),
            )
        updated = cur.rowcount > 0
        conn.execute("COMMIT;")
        return updated
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()

def record_steering_history(
    record: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record a steering audit/action entry."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True
    try:
        if should_close:
            conn.execute("BEGIN TRANSACTION;")
        now = float(record.get("timestamp") or time.time())
        action = record.get("action", "unknown")
        task_id = record.get("task_id")
        steer_id = record.get("steer_id")
        instruction = record.get("instruction")
        operator = record.get("operator")
        urgent = 1 if record.get("urgent") else 0
        reason = record.get("reason")
        payload = {k: v for k, v in record.items() if k not in {
            "action", "task_id", "steer_id", "instruction", "operator", "urgent", "reason", "timestamp"
        }}
        payload_json = json.dumps(payload, ensure_ascii=False)

        conn.execute("""
            INSERT INTO steering_history (
                action, task_id, steer_id, instruction, operator, urgent, reason, timestamp, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (action, task_id, steer_id, instruction, operator, urgent, reason, now, payload_json))
        _facade().record_event({
            "workflow_id": record.get("workflow_id"),
            "node_id": record.get("node_id") or record.get("node"),
            "task_id": task_id,
            "agent_id": record.get("agent_id") or record.get("agent"),
            "event_type": f"steering.{action}",
            "timestamp": now,
            "payload": {
                "steer_id": steer_id,
                "instruction": instruction,
                "operator": operator,
                "urgent": bool(urgent),
                "reason": reason,
                **payload,
            },
            "source": record.get("source") or "steering",
        }, conn=conn)
        if should_close:
            conn.execute("COMMIT;")
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()

def list_steering_history(
    task_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List steering history entries ordered chronologically."""
    conn = _facade().get_db_connection(db_path)
    try:
        if task_id:
            cur = conn.execute("SELECT * FROM steering_history WHERE task_id = ? ORDER BY timestamp ASC", (task_id,))
        else:
            cur = conn.execute("SELECT * FROM steering_history ORDER BY timestamp ASC")
        results = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            item = dict(payload)
            item.update({
                "action": row["action"],
                "task_id": row["task_id"],
                "steer_id": row["steer_id"],
                "instruction": row["instruction"],
                "operator": row["operator"],
                "urgent": bool(row["urgent"]),
                "reason": row["reason"],
                "timestamp": row["timestamp"],
            })
            results.append(item)
        return results
    finally:
        conn.close()
