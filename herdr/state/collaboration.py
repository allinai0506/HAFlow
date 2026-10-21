"""Persist collaboration events and their lifecycle transitions.

Collaboration rows retain workflow/run identity, peer task references, and
working-context links. They are coordination records, not trajectory facts,
observations, findings, or replacement working-context snapshots.

Writes keep the original caller-owned connection contract: a supplied
connection remains under the caller's transaction; a locally opened one is
committed and closed here. Context attachment and state changes retain the
same validation and update order as the compatibility facade baseline.

Cross-domain and patchable calls resolve through ``_facade()`` at call time.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from ._facade import _facade


def _decode_collaboration_row(row) -> Dict[str, Any]:
    def _decode_ref_list(raw: Any) -> Tuple[List[Any], bool]:
        """Decode a refs JSON column; corrupt payloads fail closed to []."""
        if raw is None or raw == "":
            return [], False
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return [], True
        if isinstance(decoded, list):
            return decoded, False
        return [], True

    artifact_refs, artifacts_corrupt = _decode_ref_list(row["artifact_refs_json"])
    evidence_refs, evidence_corrupt = _decode_ref_list(row["evidence_refs_json"])
    context_refs, context_corrupt = _decode_ref_list(row["context_refs_json"])
    return {
        "event_id": row["event_id"],
        "identity_key": row["identity_key"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "from_task_id": row["from_task_id"],
        "to_task_id": row["to_task_id"],
        "from_agent": row["from_agent"] or "",
        "to_agent": row["to_agent"] or "",
        "from_pane_id": row["from_pane_id"],
        "to_pane_id": row["to_pane_id"],
        "type": row["type"],
        "summary": row["summary"] or "",
        "artifact_refs": artifact_refs,
        "evidence_refs": evidence_refs,
        "context_refs": context_refs,
        # A corrupt refs column must never break compilation for every later
        # context: refs fail closed to [] and the corruption is marked so the
        # source version still moves instead of silently matching.
        "source_truncated": bool(artifacts_corrupt or evidence_corrupt or context_corrupt),
        "requires_response": bool(row["requires_response"]),
        "status": row["status"],
        "source_fact_id": row["source_fact_id"],
        "created_at": row["created_at"],
        "dispatched_at": row["dispatched_at"],
        "acknowledged_at": row["acknowledged_at"],
        "completed_at": row["completed_at"],
        "handoff_created_at": row["handoff_created_at"],
        "handoff_dispatched_at": row["handoff_dispatched_at"],
        "handoff_acknowledged_at": row["handoff_acknowledged_at"],
        "handoff_completed_at": row["handoff_completed_at"],
    }

def create_collaboration_event(
    event: Dict[str, Any], db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create or return the canonical CollaborationEvent (idempotent).

    Dedupe key is (run_id, from_task_id, to_task_id, type, source_fact_id).
    The losing concurrent writer returns the stored canonical row.
    """
    from herdr.collaboration import create_handoff

    candidate = create_handoff(
        run_id=str(event.get("run_id") or ""),
        workflow_id=str(event.get("workflow_id") or ""),
        from_task_id=str(event.get("from_task_id") or ""),
        from_agent=str(event.get("from_agent") or ""),
        to_task_id=str(event.get("to_task_id") or ""),
        to_agent=str(event.get("to_agent") or ""),
        summary=str(event.get("summary") or ""),
        artifact_refs=list(event.get("artifact_refs") or []),
        evidence_refs=list(event.get("evidence_refs") or []),
        context_refs=list(event.get("context_refs") or []),
        source_fact_id=str(event.get("source_fact_id") or ""),
        event_type=str(event.get("type") or "HANDOFF"),
        requires_response=bool(event.get("requires_response", True)),
    )
    if event.get("from_pane_id"):
        candidate["from_pane_id"] = event.get("from_pane_id")
    if event.get("to_pane_id"):
        candidate["to_pane_id"] = event.get("to_pane_id")
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        existing = conn.execute(
            "SELECT * FROM collaboration_events WHERE identity_key = ?",
            (candidate["identity_key"],),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT;")
            return _decode_collaboration_row(existing)
        conn.execute(
            """
            INSERT INTO collaboration_events (
                event_id, identity_key, run_id, workflow_id,
                from_task_id, to_task_id, from_agent, to_agent,
                from_pane_id, to_pane_id, type, summary,
                artifact_refs_json, evidence_refs_json, context_refs_json,
                requires_response, status, source_fact_id,
                created_at, handoff_created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO NOTHING
            """,
            (
                candidate["event_id"], candidate["identity_key"],
                candidate["run_id"], candidate["workflow_id"],
                candidate["from_task_id"], candidate["to_task_id"],
                candidate["from_agent"], candidate["to_agent"],
                candidate["from_pane_id"], candidate["to_pane_id"],
                candidate["type"], candidate["summary"],
                json.dumps(candidate["artifact_refs"], ensure_ascii=False),
                json.dumps(candidate["evidence_refs"], ensure_ascii=False),
                json.dumps(candidate["context_refs"], ensure_ascii=False),
                1 if candidate["requires_response"] else 0,
                "created", candidate["source_fact_id"],
                candidate["created_at"], candidate["handoff_created_at"],
            ),
        )
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE identity_key = ?",
            (candidate["identity_key"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("collaboration insert did not produce a canonical row")
        conn.execute("COMMIT;")
        return _decode_collaboration_row(row)
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def get_collaboration_event(
    event_id: str, db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _decode_collaboration_row(row) if row else None
    finally:
        conn.close()

def attach_working_context_ref(
    event_id: str, context_id: str, db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Attach a compiled target snapshot to a not-yet-dispatched handoff."""
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        event = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (str(event_id),),
        ).fetchone()
        context = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?", (str(context_id),),
        ).fetchone()
        if event is None or context is None:
            raise ValueError("handoff or working context does not exist")
        if str(context["task_id"]) != str(event["to_task_id"]):
            raise ValueError("working context task does not match handoff target")
        if str(context["workflow_id"] or "") != str(event["workflow_id"] or ""):
            raise ValueError("working context workflow does not match handoff")
        if str(context["run_scope"]) != str(event["run_id"]):
            raise ValueError("working context scope does not match handoff")
        context_payload = json.loads(context["payload_json"] or "{}")
        event_source_ref = f"collaboration:{event_id}"
        if event_source_ref not in set(context_payload.get("source_refs") or []):
            raise ValueError("working context does not contain the handoff fact")
        if event["status"] != "created":
            raise ValueError("cannot attach context to a dispatched handoff")
        refs = [str(context_id)]
        conn.execute(
            "UPDATE collaboration_events SET context_refs_json = ? WHERE event_id = ?",
            (json.dumps(refs, ensure_ascii=False), str(event_id)),
        )
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (str(event_id),),
        ).fetchone()
        conn.commit()
        return _decode_collaboration_row(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

def list_collaboration_events(
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM collaboration_events WHERE 1=1"
        params: List[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if task_id is not None:
            query += " AND (from_task_id = ? OR to_task_id = ?)"
            params.extend([task_id, task_id])
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC, event_id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [_decode_collaboration_row(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()

def _transition_collaboration_event(
    event_id: str, to_status: str, time_field: Optional[str],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    from herdr.collaboration import is_valid_transition

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"collaboration event '{event_id}' not found")
        old = row["status"]
        if old == to_status:
            conn.execute("COMMIT;")
            return _decode_collaboration_row(row)
        if not is_valid_transition(old, to_status):
            raise ValueError(f"cannot transition collaboration event from {old} to {to_status}")
        now = time.time()
        handoff_field = {
            "dispatched": "handoff_dispatched_at",
            "acknowledged": "handoff_acknowledged_at",
            "completed": "handoff_completed_at",
        }.get(to_status)
        if time_field and handoff_field:
            conn.execute(
                f"""UPDATE collaboration_events SET status = ?,
                    {time_field} = ?, {handoff_field} = ?
                    WHERE event_id = ?""",
                (to_status, now, now, event_id),
            )
        else:
            conn.execute(
                "UPDATE collaboration_events SET status = ? WHERE event_id = ?",
                (to_status, event_id),
            )
        updated = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        conn.execute("COMMIT;")
        return _decode_collaboration_row(updated)
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def mark_collaboration_dispatched(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "dispatched", "dispatched_at", db_path)

def mark_collaboration_acknowledged(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "acknowledged", "acknowledged_at", db_path)

def mark_collaboration_completed(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "completed", "completed_at", db_path)

def mark_collaboration_failed(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "failed", None, db_path)
