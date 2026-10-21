"""events persistence; extracted without changing behavior."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from ._facade import _facade


def record_event(
    event: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Append a canonical WorkflowEvent and return the normalized event."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    try:
        event_type = event.get("event_type")
        if not event_type:
            raise ValueError("event_type is required")

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")

        normalized = {
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": event_type,
            "timestamp": float(event["timestamp"]) if event.get("timestamp") is not None else time.time(),
            "payload": payload,
            "source": event.get("source") or "system",
            "run_id": event.get("run_id"),
        }

        cur = conn.execute("""
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            normalized["workflow_id"],
            normalized["node_id"],
            normalized["task_id"],
            normalized["agent_id"],
            normalized["event_type"],
            json.dumps(normalized["payload"], ensure_ascii=False),
            normalized["timestamp"],
            normalized["source"],
            normalized["run_id"],
        ))
        normalized["id"] = cur.lastrowid
        return normalized
    finally:
        if should_close:
            conn.close()

def record_event_if_absent(
    event: Dict[str, Any],
    *,
    identity_field: str,
    identity_value: str,
    compare_fields: Optional[Tuple[str, ...]] = None,
    scan_limit: int = 200,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append a WorkflowEvent only if its payload identity is not already stored.

    Exactly-once for a fact whose identity lives *inside* ``payload_json``.
    The identity cannot be a UNIQUE column there, so the check and the insert
    share one SQLite write transaction instead: ``BEGIN IMMEDIATE`` takes the
    write lock before the pre-read, so a second writer cannot observe "absent"
    and insert a duplicate. This is the same write lock the repository already
    uses for ``interventions``, ``collaboration_events`` and
    ``observation_receipts`` — no new table, no new lock system.

    ``compare_fields`` guards the case where the same identity arrives with
    different content: that is a caller error, not a replay, and it is rejected
    rather than silently absorbed (which would file one decision under another
    decision's identity and hide the change from the audit trail).

    Returns {"status": "created" | "exists" | "rejected", "event": ...}.
    """
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    if not identity_value:
        return {"status": "rejected", "reason": "missing_identity"}

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            rows = conn.execute(
                "SELECT id, timestamp, payload_json FROM events "
                "WHERE workflow_id IS ? AND event_type = ? "
                "ORDER BY id DESC LIMIT ?",
                (event.get("workflow_id"), event.get("event_type"),
                 int(scan_limit)),
            ).fetchall()
            for row in rows:
                stored = json.loads(row["payload_json"] or "{}")
                if str(stored.get(identity_field) or "") != str(identity_value):
                    continue
                if compare_fields and any(
                        stored.get(k) != payload.get(k) for k in compare_fields):
                    conn.execute("COMMIT;")
                    return {
                        "status": "rejected",
                        "reason": "identity_content_mismatch",
                        "event_id": row["id"],
                    }
                conn.execute("COMMIT;")
                return {
                    "status": "exists",
                    "event": {
                        "id": row["id"],
                        "workflow_id": event.get("workflow_id"),
                        "node_id": event.get("node_id"),
                        "event_type": event.get("event_type"),
                        "timestamp": row["timestamp"],
                        "payload": stored,
                        "source": event.get("source") or "system",
                    },
                }
            recorded = _facade().record_event(event, conn=conn)
            conn.execute("COMMIT;")
            return {"status": "created", "event": recorded}
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
    finally:
        conn.close()

def list_events(
    workflow_id: Optional[str] = None,
    node_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
    desc: bool = False,
) -> List[Dict[str, Any]]:
    """List WorkflowEvents in chronological order with optional filters.

    ``desc=True`` returns newest-first and makes ``limit`` select the newest
    N events (the default ASC + LIMIT would return the oldest N).
    """
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM events WHERE 1=1"
        params: List[Any] = []
        # Keep the legacy WorkflowEvent API behavior stable: trajectory rows
        # are queried through list_trajectory_events, unless explicitly asked
        # for source="trajectory".
        if source == "trajectory":
            query += " AND source = 'trajectory'"
        elif source is None:
            query += " AND (source IS NULL OR source != 'trajectory')"
        for column, value in (
            ("workflow_id", workflow_id),
            ("node_id", node_id),
            ("task_id", task_id),
            ("agent_id", agent_id),
            ("event_type", event_type),
            ("source", source),
        ):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY timestamp {direction}, id {direction}"
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
                "payload": json.loads(row["payload_json"] or "{}"),
                "source": row["source"] or "unknown",
                "run_id": row["run_id"],
            })
        return results
    finally:
        conn.close()

def aggregate_run_metric_rows(run_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Return scalar, run-scoped metric facts without loading evidence content."""
    conn = _facade().get_db_connection(db_path)
    try:
        scope_rows = []
        for task_identity in conn.execute(
            """SELECT task_id, workflow_id,
                      COALESCE(NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.run_id') END, ''), 'run_' || task_id) AS run_id,
                      COALESCE(NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.workflow_run_id') END, ''), NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.execution_id') END, ''), workflow_id, '') AS scope,
                      CASE WHEN NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.workflow_run_id') END, '') IS NOT NULL OR NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.execution_id') END, '') IS NOT NULL
                           THEN 1 ELSE 0 END AS explicit_scope
               FROM tasks
              WHERE COALESCE(NULLIF(CASE WHEN json_valid(payload_json) THEN json_extract(payload_json, '$.run_id') END, ''), 'run_' || task_id) = ?
                AND (payload_json IS NULL OR json_valid(payload_json))""",
            (str(run_id),),
        ).fetchall():
            scope_rows.append({
                "task_id": task_identity["task_id"],
                "workflow_id": task_identity["workflow_id"],
                "run_id": str(task_identity["run_id"] or ""),
                "scope": str(task_identity["scope"] or ""),
                "explicit_scope": bool(task_identity["explicit_scope"]),
            })
        identity_ambiguous = len(scope_rows) > 1
        allowed_task_ids = {str(row["task_id"] or "") for row in scope_rows}
        allowed_workflow_ids = {str(row["workflow_id"] or "") for row in scope_rows}
        if not identity_ambiguous:
            source_identity_rows = conn.execute(
                """
                SELECT 'events' AS source_table, run_id, task_id, workflow_id
                  FROM events WHERE run_id = ? AND source = 'trajectory'
                UNION ALL
                SELECT 'observations', run_id, task_id, workflow_id
                  FROM observations WHERE run_id = ?
                UNION ALL
                SELECT 'eval_results', run_id, task_id, workflow_id
                  FROM eval_results WHERE run_id = ?
                UNION ALL
                SELECT 'trajectory_findings', run_id, task_id, workflow_id
                  FROM trajectory_findings WHERE run_id = ?
                UNION ALL
                SELECT 'context_packs', run_id, task_id, workflow_id
                  FROM context_packs WHERE run_id = ?
                """,
                (run_id, run_id, run_id, run_id, run_id),
            ).fetchall()
            observed_task_ids: set[str] = set()
            observed_workflow_ids: set[str] = set()
            if not scope_rows and source_identity_rows:
                identity_ambiguous = True
            allowed_run_ids = {
                str(row["run_id"] or "") for row in scope_rows if row["run_id"]
            }
            for row in source_identity_rows:
                source_run = str(row["run_id"] or "")
                task_id = str(row["task_id"] or "")
                workflow_id = str(row["workflow_id"] or "")
                if task_id:
                    observed_task_ids.add(task_id)
                if workflow_id:
                    observed_workflow_ids.add(workflow_id)
                if scope_rows and task_id and task_id not in allowed_task_ids:
                    identity_ambiguous = True
                    break
                if scope_rows and workflow_id and workflow_id not in allowed_workflow_ids:
                    identity_ambiguous = True
                    break
                if scope_rows and not task_id and workflow_id and workflow_id not in allowed_workflow_ids:
                    identity_ambiguous = True
                    break
                if scope_rows and not task_id and not workflow_id and source_run not in allowed_run_ids:
                    identity_ambiguous = True
                    break
            if not scope_rows and (
                len(observed_task_ids) > 1 or len(observed_workflow_ids) > 1
            ):
                identity_ambiguous = True
        if identity_ambiguous:
            return {
                "trajectory_events": 0, "started_at": None, "finished_at": None,
                "run_completed": 0, "run_failed": 0, "verification_total": 0,
                "verification_passed": 0, "verification_failed": 0,
                "task_started": 0, "artifact_created": 0, "agent_done": 0,
                "task_id": None, "workflow_id": None, "observations_created": 0,
                "observation_bytes": 0, "findings_created": 0, "context_packs_created": 0,
                "working_context_compiles": 0, "working_context_reused": 0,
                "working_context_changed": 0, "latest_working_context": None,
                "latest_context": None, "identity_ambiguous": True,
            }
        event_row = conn.execute(
            """
            SELECT COUNT(*) AS trajectory_events,
                   MIN(timestamp) AS started_at,
                   MAX(CASE WHEN event_type IN ('run_completed', 'run_failed') THEN timestamp END)
                       AS finished_at,
                   SUM(CASE WHEN event_type = 'run_completed' THEN 1 ELSE 0 END) AS run_completed,
                   SUM(CASE WHEN event_type = 'run_failed' THEN 1 ELSE 0 END) AS run_failed,
                   SUM(CASE WHEN event_type = 'verification_completed' THEN 1 ELSE 0 END)
                       AS verification_total,
                   SUM(CASE WHEN event_type = 'verification_completed'
                            THEN CASE WHEN json_valid(payload_json)
                                      THEN CASE WHEN (
                                          json_type(payload_json, '$.verification.passed') = 'false'
                                          OR json_type(payload_json, '$.verification_passed') = 'false'
                                          OR json_type(payload_json, '$.verification.verification_passed') = 'false'
                                          OR json_type(payload_json, '$.passed') = 'false'
                                      ) THEN 0
                                                WHEN (
                                          json_type(payload_json, '$.verification.passed') = 'true'
                                          OR json_type(payload_json, '$.verification_passed') = 'true'
                                          OR json_type(payload_json, '$.verification.verification_passed') = 'true'
                                          OR json_type(payload_json, '$.passed') = 'true'
                                      ) THEN 1 ELSE 0 END
                                      ELSE 0 END
                            ELSE 0 END) AS verification_passed,
                   SUM(CASE WHEN event_type = 'verification_completed'
                            THEN CASE WHEN json_valid(payload_json)
                                      THEN CASE WHEN (
                                          json_type(payload_json, '$.verification.passed') = 'false'
                                          OR json_type(payload_json, '$.verification_passed') = 'false'
                                          OR json_type(payload_json, '$.verification.verification_passed') = 'false'
                                          OR json_type(payload_json, '$.passed') = 'false'
                                      ) THEN 1 ELSE 0 END
                                      ELSE 0 END
                            ELSE 0 END) AS verification_failed
                   ,SUM(CASE WHEN event_type = 'task_started' THEN 1 ELSE 0 END) AS task_started
                   ,SUM(CASE WHEN event_type = 'artifact_created' THEN 1 ELSE 0 END) AS artifact_created
                   ,SUM(CASE WHEN event_type = 'agent_done' THEN 1 ELSE 0 END) AS agent_done
              FROM events
             WHERE run_id = ? AND source = 'trajectory'
            """,
            (run_id,),
        ).fetchone()
        if len(scope_rows) == 1:
            authoritative_scope = scope_rows[0]
            identity = {
                "task_id": authoritative_scope["task_id"],
                "workflow_id": authoritative_scope["workflow_id"],
            }
            metric_workflow_id = str(authoritative_scope["workflow_id"] or "") or None
        else:
            identity = conn.execute(
                """
                SELECT
                  (SELECT task_id FROM events
                    WHERE run_id = ? AND source = 'trajectory' AND task_id IS NOT NULL
                    ORDER BY sequence ASC, id ASC LIMIT 1) AS task_id,
                  (SELECT workflow_id FROM events
                    WHERE run_id = ? AND source = 'trajectory' AND workflow_id IS NOT NULL
                    ORDER BY sequence ASC, id ASC LIMIT 1) AS workflow_id
                """,
                (run_id, run_id),
            ).fetchone()
            metric_workflow_id = (
                identity["workflow_id"] if identity is not None else None
            )
        observations = conn.execute(
            """
            SELECT COUNT(*) AS observations_created,
                   COALESCE(SUM(size_bytes), 0) AS observation_bytes
              FROM observations
             WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        findings = conn.execute(
            "SELECT COUNT(*) AS findings_created FROM trajectory_findings WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        context_packs = conn.execute(
            "SELECT COUNT(*) AS context_packs_created FROM context_packs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        latest_context = conn.execute(
            """
            SELECT * FROM context_packs
             WHERE run_id = ?
             ORDER BY created_at DESC, rowid DESC
             LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        working_context_metrics = conn.execute(
            """
            SELECT COUNT(*) AS metric_count,
                   COALESCE(SUM(reused), 0) AS reused_count,
                   COALESCE(SUM(changed), 0) AS changed_count
              FROM working_context_metric_events
             WHERE run_id = ?
               AND workflow_id IS ?
            """,
            (run_id, metric_workflow_id),
        ).fetchone()
        working_context_snapshots = conn.execute(
            """
            SELECT COUNT(*) AS snapshot_count
              FROM working_contexts
             WHERE run_id = ?
               AND workflow_id IS ?
            """,
            (run_id, metric_workflow_id),
        ).fetchone()
        latest_working_context = conn.execute(
            """
            SELECT * FROM working_contexts
             WHERE run_id = ?
               AND workflow_id IS ?
             ORDER BY source_watermark DESC, compiled_at DESC, rowid DESC
             LIMIT 1
            """,
            (run_id, metric_workflow_id),
        ).fetchone()
        working_context_compile_count = max(
            int(working_context_metrics["metric_count"] or 0),
            int(working_context_snapshots["snapshot_count"] or 0),
        )
        working_context_reuse_count = int(working_context_metrics["reused_count"] or 0)
        working_context_change_count = int(working_context_metrics["changed_count"] or 0)
        return {
            "trajectory_events": int(event_row["trajectory_events"] or 0),
            "started_at": event_row["started_at"],
            "finished_at": event_row["finished_at"],
            "run_completed": int(event_row["run_completed"] or 0),
            "run_failed": int(event_row["run_failed"] or 0),
            "verification_total": int(event_row["verification_total"] or 0),
            "verification_passed": int(event_row["verification_passed"] or 0),
            "verification_failed": int(event_row["verification_failed"] or 0),
            "task_started": int(event_row["task_started"] or 0),
            "artifact_created": int(event_row["artifact_created"] or 0),
            "agent_done": int(event_row["agent_done"] or 0),
            "task_id": identity["task_id"] if identity else None,
            "workflow_id": metric_workflow_id,
            "observations_created": int(observations["observations_created"] or 0),
            "observation_bytes": int(observations["observation_bytes"] or 0),
            "findings_created": int(findings["findings_created"] or 0),
            "context_packs_created": int(context_packs["context_packs_created"] or 0),
            "working_context_compiles": working_context_compile_count,
            "working_context_reused": working_context_reuse_count,
            "working_context_changed": working_context_change_count,
            "latest_working_context": dict(latest_working_context) if latest_working_context else None,
            "latest_context": dict(latest_context) if latest_context else None,
        }
    finally:
        conn.close()
