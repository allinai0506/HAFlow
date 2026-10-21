"""rollout persistence; extracted without changing behavior."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from ._facade import _facade
from .connection import ReadonlySchemaError
from .schema_ddl_helpers import _ensure_rollout_schema


def rollout_bucket_key(agent: str, node: str, task_type: str) -> str:
    """Unambiguous display/render key for one bucket.

    JSON array encoding, not ``"/"``-joining: the latter cannot tell
    ``("a/b", "c", "d")`` from ``("a", "b/c", "d")``. Storage does not
    rely on this string at all — ``rollout_state`` is keyed by the real
    composite columns — but audit rows and CLI output do, so two buckets
    must never render as the same key.
    """
    return json.dumps(
        [str(agent or ""), str(node or ""), str(task_type or "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )

def _rollout_tables_present(conn: sqlite3.Connection) -> bool:
    """True when this database already carries the rollout tables."""
    names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    return "rollout_state" in names and "rollout_audit" in names

def _rollout_read_connection(
    db_path: Optional[Path],
    readonly: bool,
) -> sqlite3.Connection:
    """Open a connection for rollout reads.

    ``readonly=True`` follows the #102 diagnostic contract: the database
    must already exist (``mode=ro`` + ``query_only``), and no schema is
    created, migrated or repaired. Observing state must not manufacture
    state, so a pre-rollout database simply has no rollout tables and
    reads as "no staged bucket" — never as a side effect of looking.
    """
    if not readonly:
        conn = _facade().get_db_connection(Path(db_path) if db_path else None)
        _ensure_rollout_schema(conn)
        return conn
    conn = _facade().get_readonly_db_connection(db_path)
    if not _rollout_tables_present(conn):
        conn.close()
        raise ReadonlySchemaError(
            "not compatible with rollout state: rollout tables absent "
            "(read-only path never creates them)")
    return conn

@dataclass(frozen=True)
class RolloutSnapshot:
    """The full identity of one bucket's staged state at a point in time.

    ``exists`` is part of the identity, not a detail. An absent row and a
    row holding 0 are different worlds: the absent bucket still runs at
    the canary config percentage, the explicit-off bucket does not. A
    compare-and-swap that only looks at ``percentage`` cannot tell them
    apart, so a promotion that read "absent" would silently overwrite an
    emergency rollback that wrote an explicit 0 in the meantime.
    """

    exists: bool
    percentage: int

@dataclass(frozen=True)
class RolloutWrite:
    """One requested state change, decided from a :class:`RolloutSnapshot`."""

    new_percentage: int
    #: Percentage that was actually being diverted, for the audit fact.
    previous_percentage: int
    action: str
    reason: str
    source: str
    algorithm_version: str
    created_at: float

def read_rollout_snapshot(
    agent: str,
    node: str,
    task_type: str,
    db_path: Optional[Path] = None,
    *,
    readonly: bool = False,
) -> RolloutSnapshot:
    """Read one bucket's staged state in a single query.

    One row read yields both fields, so a caller can never observe an
    ``exists``/``percentage`` pair stitched together from two different
    points in time.
    """
    conn = _rollout_read_connection(db_path, readonly)
    try:
        row = conn.execute(
            "SELECT percentage FROM rollout_state "
            "WHERE agent = ? AND node = ? AND task_type = ?",
            (agent, node, task_type or ""),
        ).fetchone()
        if row is None:
            return RolloutSnapshot(exists=False, percentage=0)
        return RolloutSnapshot(exists=True, percentage=int(row["percentage"]))
    finally:
        conn.close()

def get_rollout_stage(
    agent: str,
    node: str,
    task_type: str,
    db_path: Optional[Path] = None,
    *,
    readonly: bool = False,
) -> int:
    """Read the current staged percentage for one bucket (0 when absent).

    Presence-independent convenience view. Anything that decides whether
    to write must use :func:`read_rollout_snapshot` instead.
    """
    return _facade().read_rollout_snapshot(
        agent, node, task_type, db_path, readonly=readonly).percentage

def list_rollout_states(
    db_path: Optional[Path] = None,
    *,
    readonly: bool = False,
) -> List[Dict[str, Any]]:
    """List every bucket with an explicit staged row (absent means off)."""
    conn = _rollout_read_connection(db_path, readonly)
    try:
        rows = conn.execute(
            "SELECT agent, node, task_type, percentage, "
            "updated_at, updated_by, reason FROM rollout_state "
            "ORDER BY agent, node, task_type",
        ).fetchall()
        return [
            {
                "bucket_key": rollout_bucket_key(
                    row["agent"], row["node"], row["task_type"] or ""),
                "agent": row["agent"],
                "node": row["node"],
                "task_type": row["task_type"] or "",
                "percentage": int(row["percentage"]),
                "updated_at": float(row["updated_at"]),
                "updated_by": row["updated_by"] or "",
                "reason": row["reason"] or "",
            }
            for row in rows
        ]
    finally:
        conn.close()

def list_rollout_audit(
    agent: Optional[str] = None,
    node: Optional[str] = None,
    task_type: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
    *,
    readonly: bool = False,
) -> List[Dict[str, Any]]:
    """Newest-first immutable rollout history, optionally one bucket."""
    conn = _rollout_read_connection(db_path, readonly)
    try:
        query = (
            "SELECT id, agent, node, task_type, "
            "previous_percentage, new_percentage, action, reason, source, "
            "algorithm_version, created_at FROM rollout_audit WHERE 1=1"
        )
        params: List[Any] = []
        if agent is not None:
            query += " AND agent = ?"
            params.append(str(agent))
        if node is not None:
            query += " AND node = ?"
            params.append(str(node))
        if task_type is not None:
            query += " AND task_type = ?"
            params.append(str(task_type))
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [
            {
                "id": int(row["id"]),
                "bucket_key": rollout_bucket_key(
                    row["agent"], row["node"], row["task_type"] or ""),
                "recommended_agent": row["agent"],
                "agent": row["agent"],
                "node": row["node"],
                "task_type": row["task_type"] or "",
                "previous_percentage": int(row["previous_percentage"]),
                "new_percentage": int(row["new_percentage"]),
                "action": row["action"],
                "reason": row["reason"] or "",
                "source": row["source"] or "",
                "algorithm_version": row["algorithm_version"] or "",
                "created_at": float(row["created_at"]),
            }
            for row in conn.execute(query, tuple(params)).fetchall()
        ]
    finally:
        conn.close()

def rollout_stage_known(
    agent: str,
    node: str,
    task_type: str,
    db_path: Optional[Path] = None,
    *,
    readonly: bool = False,
) -> bool:
    """True when the bucket has an explicit staged row (off counts)."""
    return _facade().read_rollout_snapshot(
        agent, node, task_type, db_path, readonly=readonly).exists

def _decode_rollout_audit_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "bucket_key": rollout_bucket_key(
            row["agent"], row["node"], row["task_type"] or ""),
        "recommended_agent": row["agent"],
        "agent": row["agent"],
        "node": row["node"],
        "task_type": row["task_type"] or "",
        "previous_percentage": int(row["previous_percentage"]),
        "new_percentage": int(row["new_percentage"]),
        "action": row["action"],
        "reason": row["reason"] or "",
        "source": row["source"] or "",
        "algorithm_version": row["algorithm_version"] or "",
        "created_at": float(row["created_at"]),
    }

def transact_rollout_stage(
    *,
    agent: str,
    node: str,
    task_type: str,
    expected: RolloutSnapshot,
    write: Optional[RolloutWrite],
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Verify a snapshot and apply one change inside a single transaction.

    The whole read-decide-write cycle is one boundary:

    ``BEGIN IMMEDIATE`` -> read the live snapshot -> require it to equal
    ``expected`` -> upsert state -> append exactly one audit row ->
    ``COMMIT``.

    Two properties the previous implementation did not have:

    1. **The CAS covers presence, not just the number.** ``expected``
       carries ``exists`` alongside ``percentage``, so a promotion that
       observed an absent bucket cannot commit over an emergency
       rollback that created an explicit 0 row in the meantime.
    2. **A no-op needs the same CAS.** Callers pass ``write=None`` when
       nothing needs to change; the transaction still verifies the
       snapshot, so "already off" can never be reported from a stale
       read taken before a concurrent promotion.

    ``expected=None`` skips verification for callers that deliberately
    read inside their own transaction. Returns the new audit row, or
    ``None`` when ``write`` was ``None`` (nothing was written).
    """
    conn = _facade().get_db_connection(Path(db_path) if db_path else None)
    try:
        _ensure_rollout_schema(conn)
        key = rollout_bucket_key(agent, node, task_type or "")
        conn.execute("BEGIN IMMEDIATE;")
        try:
            current = conn.execute(
                "SELECT percentage FROM rollout_state "
                "WHERE agent = ? AND node = ? AND task_type = ?",
                (agent, node, task_type or ""),
            ).fetchone()
            live = (
                RolloutSnapshot(exists=True, percentage=int(current["percentage"]))
                if current is not None
                else RolloutSnapshot(exists=False, percentage=0)
            )
            if expected is not None and live != expected:
                raise ValueError(
                    f"rollout conflict: bucket {key} is now "
                    f"{'stage=' + str(live.percentage) if live.exists else 'absent'}"
                    f", expected "
                    f"{'stage=' + str(expected.percentage) if expected.exists else 'absent'}"
                    "; reload state and retry")
            audit_id: Optional[int] = None
            if write is not None:
                conn.execute(
                    "INSERT INTO rollout_state "
                    "(agent, node, task_type, percentage, "
                    "updated_at, updated_by, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(agent, node, task_type) DO UPDATE SET "
                    "percentage = excluded.percentage, "
                    "updated_at = excluded.updated_at, "
                    "updated_by = excluded.updated_by, "
                    "reason = excluded.reason",
                    (agent, node, task_type or "",
                     int(write.new_percentage), float(write.created_at),
                     str(write.source), str(write.reason)),
                )
                cur = conn.execute(
                    "INSERT INTO rollout_audit "
                    "(bucket_key, agent, node, task_type, previous_percentage, "
                    "new_percentage, action, reason, source, algorithm_version, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, agent, node, task_type or "",
                     int(write.previous_percentage), int(write.new_percentage),
                     str(write.action), str(write.reason), str(write.source),
                     str(write.algorithm_version), float(write.created_at)),
                )
                audit_id = int(cur.lastrowid)
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        if audit_id is None:
            return None
        row = conn.execute(
            "SELECT id, agent, node, task_type, "
            "previous_percentage, new_percentage, action, reason, source, "
            "algorithm_version, created_at FROM rollout_audit WHERE id = ?",
            (audit_id,),
        ).fetchone()
        return _decode_rollout_audit_row(row)
    finally:
        conn.close()
