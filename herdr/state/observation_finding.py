"""observation finding persistence; extracted without changing behavior."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from ._facade import _facade


def _decode_finding_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "finding_id": row["finding_id"],
        "finding_key": row["finding_key"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "node": row["node"],
        "agent": row["agent"],
        "agent_session_id": row["agent_session_id"],
        "finding_type": row["finding_type"],
        "severity": row["severity"],
        "status": row["status"] or "open",
        "summary": row["summary"],
        "suspected_cause": row["suspected_cause"],
        "recommended_action": row["recommended_action"],
        "confidence": row["confidence"],
        "evidence": json.loads(row["evidence_json"] or "[]"),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }

def upsert_trajectory_finding(
    finding: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Record/refresh one analysis finding and return the canonical persisted row.

    Findings are deliberately separated from trajectory events: the events
    ledger records what happened, this table records what HAFlow suspects it
    means. ``finding_key`` is the dedup contract (UNIQUE):

    - first observation inserts the row;
    - later observations of the SAME episode (same key) refresh the analysis
      fields in place (severity/summary/evidence/... escalating as the chain
      grows) while ``finding_id``/``created_at`` stay canonical;
    - a concurrent observer that lost the insert race re-reads and returns the
      canonical row, so every caller converges on one ``finding_id``;
    - a lower-severity observation never downgrades a stored row.

    Returns None only when the row cannot be read back at all.
    """
    finding_key = finding.get("finding_key")
    finding_id = finding.get("finding_id")
    run_id = finding.get("run_id")
    finding_type = finding.get("finding_type")
    if not finding_key or not finding_id or not run_id or not finding_type:
        raise ValueError("finding_id, finding_key, run_id and finding_type are required")
    metadata = dict(finding.get("metadata") or {})
    for relation_key in ("supersedes", "superseded_by"):
        if finding.get(relation_key) is not None and relation_key not in metadata:
            metadata[relation_key] = finding[relation_key]

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trajectory_findings (
                finding_id, finding_key, run_id, task_id, workflow_id, node,
                agent, agent_session_id, finding_type, severity, status,
                summary, suspected_cause, recommended_action, confidence,
                evidence_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(finding_key) DO UPDATE SET
                task_id = excluded.task_id,
                workflow_id = excluded.workflow_id,
                node = excluded.node,
                agent = excluded.agent,
                agent_session_id = excluded.agent_session_id,
                severity = excluded.severity,
                status = excluded.status,
                summary = excluded.summary,
                suspected_cause = excluded.suspected_cause,
                recommended_action = excluded.recommended_action,
                confidence = excluded.confidence,
                evidence_json = excluded.evidence_json,
                metadata_json = excluded.metadata_json
            WHERE CASE excluded.severity
                      WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END
                  >= CASE trajectory_findings.severity
                      WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END;
            """,
            (
                finding_id,
                finding_key,
                run_id,
                finding.get("task_id"),
                finding.get("workflow_id"),
                finding.get("node"),
                finding.get("agent"),
                finding.get("agent_session_id"),
                finding_type,
                finding.get("severity") or "warning",
                finding.get("status") or "open",
                finding.get("summary"),
                finding.get("suspected_cause"),
                finding.get("recommended_action"),
                float(finding.get("confidence") or 0.0),
                json.dumps(finding.get("evidence") or [], ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                float(finding["created_at"]) if finding.get("created_at") is not None else time.time(),
            ),
        )
        # Whether this call inserted, updated, or lost a race, the canonical
        # row is the single answer every caller must return.
        row = conn.execute(
            "SELECT * FROM trajectory_findings WHERE finding_key = ?", (finding_key,)
        ).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        conn.close()

def get_trajectory_finding(
    finding_key: str,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one finding by its dedup key (None when absent)."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True
    try:
        row = conn.execute(
            "SELECT * FROM trajectory_findings WHERE finding_key = ?", (finding_key,)
        ).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()

def get_trajectory_finding_by_id(
    finding_id: str,
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one finding by its public identity for reference verification."""
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM trajectory_findings WHERE finding_id = ?"
        params: List[Any] = [finding_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        row = conn.execute(query, params).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        conn.close()

def list_trajectory_findings(
    run_id: Optional[str] = None,
    finding_type: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List analysis findings in stable creation order."""
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM trajectory_findings WHERE 1=1"
        params: List[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if finding_type is not None:
            query += " AND finding_type = ?"
            params.append(finding_type)
        query += " ORDER BY created_at ASC, rowid ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_finding_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()

def list_trajectory_findings_bounded(
    run_id: str,
    limit: int,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return the most important recent findings with SQL-side bounding."""
    conn = _facade().get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM trajectory_findings
               WHERE run_id = ?
               ORDER BY CASE severity
                          WHEN 'critical' THEN 3
                          WHEN 'warning' THEN 2
                          ELSE 1
                        END DESC,
                        created_at DESC, rowid DESC
               LIMIT ?""",
            (run_id, int(limit)),
        ).fetchall()
        return [_decode_finding_row(row) for row in rows]
    finally:
        conn.close()

def _finding_source_fingerprint(findings: List[Dict[str, Any]]) -> str:
    payload = json.dumps(findings, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _observation_source_fingerprint(observations: List[Dict[str, Any]]) -> str:
    payload = json.dumps(observations, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _compact_observation_metadata(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "observation_id": row["observation_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "sha256": row["sha256"],
            "excerpt": row.get("excerpt"),
        }
        for row in rows
    ]

def _context_source_version_in_conn(
    conn: sqlite3.Connection,
    run_id: str,
    task_id: Optional[str],
    finding_limit: int,
    observation_limit: int = 20,
) -> Dict[str, Any]:
    sequence_row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
        (run_id,),
    ).fetchone()
    task_updated_at = None
    if task_id:
        task_row = conn.execute(
            "SELECT updated_at FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task_row is not None:
            task_updated_at = task_row["updated_at"]
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
        (run_id, int(finding_limit)),
    ).fetchall()
    findings = [_decode_finding_row(row) for row in finding_rows]
    observation_rows = conn.execute(
        """SELECT * FROM observations
           WHERE run_id = ?
           ORDER BY created_at DESC, observation_id DESC
           LIMIT ?""",
        (run_id, int(observation_limit)),
    ).fetchall()
    observations = _compact_observation_metadata(
        [_decode_observation_row(row) for row in observation_rows]
    )
    observations.reverse()
    return {
        "task_id": task_id,
        "trajectory_sequence": int(sequence_row["sequence"] or 0),
        "task_updated_at": task_updated_at,
        "finding_fingerprint": _finding_source_fingerprint(findings),
        "finding_limit": int(finding_limit),
        "observation_fingerprint": _observation_source_fingerprint(observations),
        "observation_limit": int(observation_limit),
    }

def get_context_source_version(
    run_id: str,
    task_id: Optional[str],
    finding_limit: int,
    observation_limit: int = 20,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read the compact source watermark/revisions from one SQLite snapshot."""
    conn = _facade().get_db_connection(db_path)
    try:
        return _context_source_version_in_conn(conn, run_id, task_id, finding_limit, observation_limit)
    finally:
        conn.close()

def _decode_observation_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "observation_id": row["observation_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "content_ref": row["content_ref"],
        "media_type": row["media_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "excerpt": row["excerpt"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }

def insert_observation(
    observation: Dict[str, Any],
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Insert immutable Observation metadata and return the inserted row."""
    should_close = conn is None
    conn = conn or _facade().get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO observations (
                observation_id, run_id, task_id, workflow_id, source_type,
                source_ref, content_ref, media_type, size_bytes, sha256,
                excerpt, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation["observation_id"],
                observation["run_id"],
                observation.get("task_id"),
                observation.get("workflow_id"),
                observation["source_type"],
                observation["source_ref"],
                observation["content_ref"],
                observation["media_type"],
                int(observation["size_bytes"]),
                observation["sha256"],
                observation.get("excerpt"),
                json.dumps(observation.get("metadata") or {}, ensure_ascii=False),
                float(observation["created_at"]),
            ),
        )
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?",
            (observation["observation_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("observation insert was not readable")
        return _decode_observation_row(row)
    finally:
        if should_close:
            conn.close()

def get_observation(
    observation_id: str,
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Read one immutable Observation metadata row."""
    should_close = conn is None
    conn = conn or _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return _decode_observation_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()

def find_observation_by_dedup(
    run_id: str,
    source_type: str,
    source_ref: str,
    sha256: str,
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Find the canonical row for an Observation deduplication key."""
    should_close = conn is None
    conn = conn or _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM observations
            WHERE run_id = ? AND source_type = ? AND source_ref = ? AND sha256 = ?
            """,
            (run_id, source_type, source_ref, sha256),
        ).fetchone()
        return _decode_observation_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()

def list_observations(
    *,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: Optional[int] = None,
    desc: bool = False,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List Observation metadata with access-pattern-aligned filters."""
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM observations WHERE 1=1"
        params: List[Any] = []
        for column, value in (("run_id", run_id), ("task_id", task_id), ("source_type", source_type)):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY created_at {direction}, observation_id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_observation_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()
