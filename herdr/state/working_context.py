"""working context persistence; extracted without changing behavior."""

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from ._facade import _facade


def _decode_working_context_row(row: sqlite3.Row) -> Dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    metrics = json.loads(row["metrics_json"] or "{}")
    payload.update({
        "context_id": row["context_id"],
        "run_scope": row["run_scope"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "task_id": row["task_id"],
        "node_id": row["node_id"],
        "agent_role": row["agent_role"],
        "context_fingerprint": row["context_fingerprint"],
        "source_version": row["source_version"],
        "source_watermark": int(row["source_watermark"] or 0),
        "metrics": metrics,
        "compiled_at": row["compiled_at"],
    })
    return payload

def save_working_context(
    context: Dict[str, Any], db_path: Optional[Path] = None,
    *, fingerprint_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append an immutable WorkingContext, reusing the latest equal fingerprint."""
    required = (
        "context_id", "run_scope", "run_id", "workflow_id", "task_id",
        "agent_role", "context_fingerprint",
    )
    if any(not context.get(key) for key in required):
        raise ValueError("WorkingContext identity and fingerprint are required")
    if not re.fullmatch(r"wc_[A-Za-z0-9_-]+", str(context["context_id"])):
        raise ValueError("working context context_id is invalid")
    def _normalize_json_value(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): _normalize_json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_normalize_json_value(item) for item in value]
        return value

    context = _normalize_json_value(context)
    try:
        from herdr.observation import _redact_value
        context = _normalize_json_value(_redact_value(context))
    except ImportError:
        context = _normalize_json_value(context)
    if context.get("agent_role") not in {"developer", "reviewer", "tester", "coordinator"}:
        raise ValueError("working context agent_role is invalid")
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in context.get(field_name) or []:
            if not isinstance(item, dict) or not item.get("source_ref"):
                raise ValueError(f"working context item in {field_name} requires source_ref")
            if not _facade()._valid_context_source_ref(item["source_ref"]):
                raise ValueError(f"working context item in {field_name} has invalid source_ref")
            if field_name == "verification":
                value = item.get("value")
                if not isinstance(value, Mapping):
                    raise ValueError("working context verification value must be an object")
                if not isinstance(value, dict):
                    item["value"] = value = dict(value)
                if isinstance(value, Mapping):
                    for key in ("passed", "verification_passed", "requirements_satisfied"):
                        if key in value and value[key] is not None and not isinstance(value[key], bool):
                            raise ValueError("working context verification values must be strict booleans")
                    if (
                        "passed" in value
                        and "verification_passed" in value
                        and value["passed"] is not None
                        and value["verification_passed"] is not None
                        and value["passed"] != value["verification_passed"]
                    ):
                        raise ValueError("working context verification aliases must agree")
            for ref in item.get("evidence_refs") or []:
                if not _facade()._valid_context_source_ref(ref):
                    raise ValueError(f"working context item in {field_name} has invalid evidence_ref")
    for ref in context.get("source_refs") or []:
        if not _facade()._valid_context_source_ref(ref):
            raise ValueError("working context source_refs contains an invalid reference")
    for ref in (context.get("goal_source_ref"), context.get("next_action_source_ref")):
        if ref and not _facade()._valid_context_source_ref(ref):
            raise ValueError("working context scalar source_ref is invalid")
    for ref in (context.get("current_state_refs") or {}).values():
        if not _facade()._valid_context_source_ref(ref):
            raise ValueError("working context current_state_refs contains an invalid reference")
    if not re.fullmatch(r"[0-9a-f]{64}", str(context.get("context_fingerprint") or "")):
        raise ValueError("working context fingerprint is invalid")
    if fingerprint_config is None:
        fingerprint_config = context.get("_fingerprint_config")
    if not isinstance(fingerprint_config, dict):
        raise ValueError("working context requires fingerprint configuration")
    from herdr.context_projection import _config as normalize_fingerprint_config
    from herdr.context_projection import MAX_STORAGE_CONTEXT_CHARS
    fingerprint_config = normalize_fingerprint_config(fingerprint_config)
    max_chars = int(fingerprint_config.get("max_chars", 20000))
    if max_chars < 1 or len(json.dumps(context, ensure_ascii=False)) > max_chars:
        raise ValueError("working context exceeds fingerprint configuration budget")
    max_items = int(fingerprint_config.get("max_items", 20000))
    item_fields = (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    )
    total_items = sum(
        len(context.get(field) or [])
        for field in item_fields
    )
    if max_items < 1 or total_items > max_items:
        raise ValueError("working context exceeds fingerprint item budget")
    kind_caps = fingerprint_config.get("max_items_per_kind") or {}
    for field in item_fields:
        cap = kind_caps.get(field)
        if cap is not None and len(context.get(field) or []) > int(cap):
            raise ValueError(f"working context exceeds fingerprint cap for {field}")
    from herdr.context_models import WorkingContext, _hash
    from herdr.context_projection import _fingerprint_payload
    try:
        fingerprint_candidate = WorkingContext.from_mapping(context)
        expected_fingerprint = _hash(_fingerprint_payload(fingerprint_candidate, fingerprint_config))
    except Exception as exc:
        raise ValueError("working context fingerprint input is invalid") from exc
    if str(expected_fingerprint) != str(context.get("context_fingerprint") or ""):
        raise ValueError("working context fingerprint does not match payload")
    all_source_refs = set(context.get("source_refs") or [])
    all_source_refs.update(str(ref) for ref in (context.get("goal_source_ref"), context.get("next_action_source_ref")) if ref)
    all_source_refs.update(str(ref) for ref in (context.get("current_state_refs") or {}).values())
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in context.get(field_name) or []:
            all_source_refs.add(str(item.get("source_ref") or ""))
            all_source_refs.update(str(ref) for ref in item.get("evidence_refs") or [])
    declared_source_refs = set(context.get("source_refs") or [])
    goal_source_ref = str(context.get("goal_source_ref") or "")
    if (
        not goal_source_ref
        or goal_source_ref.startswith(("policy:", "artifact:", "evidence:"))
    ):
        raise ValueError("working context requires a stored goal_source_ref")
    source_backed = True
    if source_backed:
        source_clock_value = (context.get("metrics") or {}).get("source_clock")
        if (
            isinstance(source_clock_value, bool)
            or not isinstance(source_clock_value, int)
            or source_clock_value < 0
        ):
            raise ValueError("source-backed working context requires a non-negative integer source_clock")
        from herdr.context_models import _payload_digest
        if (context.get("metrics") or {}).get("payload_digest") != _payload_digest(context):
            raise ValueError("working context payload digest does not match fingerprint input")
    payload_json = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    if len(json.dumps(context, ensure_ascii=False)) > MAX_STORAGE_CONTEXT_CHARS:
        raise ValueError("working context exceeds the storage size limit")
    metrics_json = json.dumps(
        context.get("metrics") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        existing_id = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?",
            (str(context["context_id"]),),
        ).fetchone()
        if existing_id is not None:
            if (
                str(existing_id["run_scope"]) != str(context["run_scope"])
                or str(existing_id["task_id"]) != str(context["task_id"])
                or str(existing_id["agent_role"]) != str(context["agent_role"])
                or str(existing_id["workflow_id"] or "") != str(context.get("workflow_id") or "")
            ):
                raise ValueError("context_id is already used by another WorkingContext scope")
            from herdr.context_models import _payload_digest
            existing_mapping = _decode_working_context_row(existing_id)
            if (
                str(existing_id["context_fingerprint"]) != str(context["context_fingerprint"])
                or _payload_digest(existing_mapping) != _payload_digest(context)
            ):
                raise ValueError("context_id is already used by a different WorkingContext payload")
            conn.commit()
            return existing_mapping
        _facade()._validate_context_source_existence(conn, context)
        if not all_source_refs.issubset(declared_source_refs):
            raise ValueError("working context source_refs does not cover all provenance")
        # Relevant-source TOCTOU guard: heads are only advanced by compile-time
        # registration, so a relevant write landing after the compiler's last
        # fresh read would otherwise persist a stale immutable snapshot with a
        # matching head. Recompute the lightweight relevant source version
        # inside this write transaction (we hold the write lock, so no other
        # writer can commit between this check and the INSERT below) and
        # require it to match the candidate. Unrelated sibling writes do not
        # change the relevant version and pass through. planned_links travel
        # in metrics so handoff compiles recompute the same closure.
        from herdr.context_sources import _read_source_snapshot
        guard_links = []
        for link in ((context.get("metrics") or {}).get("planned_links") or []):
            if not isinstance(link, Mapping):
                continue
            from_id = str(link.get("from_task_id") or "")
            to_id = str(link.get("to_task_id") or "")
            if from_id and to_id:
                guard_links.append({"from_task_id": from_id, "to_task_id": to_id})
        try:
            guard_snapshot = _read_source_snapshot(
                workflow_id=str(context.get("workflow_id") or ""),
                task_id=str(context.get("task_id") or ""),
                db_path=db_path,
                planned_links=guard_links or None,
            )
        except Exception:
            conn.commit()
            result = dict(context)
            result["_stale_snapshot"] = True
            return result
        if str(guard_snapshot.get("source_version") or "") != str(context.get("source_version") or ""):
            conn.commit()
            result = dict(context)
            result["_stale_snapshot"] = True
            return result
        # NOTE: staleness is decided by the task-specific source version/head
        # (relevant-aware: current task + dependency closure + handoff peers)
        # plus the compiler's fresh re-read version check. The execution-wide
        # source_clock stays in metrics as an informational watermark but is
        # intentionally NOT a save gate: any source write in the execution
        # (including unrelated sibling tasks) bumps it, so gating on it marks
        # relevant candidates stale and forces futile retries.
        source_head = conn.execute(
            """
            SELECT source_version, revision
            FROM working_context_source_heads
            WHERE run_scope = ? AND workflow_id = ? AND task_id = ?
            """,
            (
                str(context["run_scope"]),
                str(context.get("workflow_id") or ""),
                str(context["task_id"]),
            ),
        ).fetchone()
        if source_head is not None and (
            str(source_head["source_version"]) != str(context.get("source_version") or "")
            or int(source_head["revision"] or 0) != int(context.get("source_watermark") or 0)
        ):
            conn.commit()
            result = dict(context)
            result["_stale_snapshot"] = True
            return result
        latest = conn.execute(
            """SELECT * FROM working_contexts
               WHERE task_id = ? AND agent_role = ?
                 AND run_scope = ? AND workflow_id IS ?
               ORDER BY source_watermark DESC, compiled_at DESC, rowid DESC LIMIT 1""",
            (
                str(context["task_id"]), str(context["agent_role"]),
                str(context["run_scope"]), context.get("workflow_id"),
            ),
        ).fetchone()
        if latest is not None and latest["context_fingerprint"] == context["context_fingerprint"]:
            conn.commit()
            return _decode_working_context_row(latest)
        # A late request with an older logical revision is still retained as
        # immutable history, but cannot become the latest snapshot.
        candidate_time = float(context.get("compiled_at") or time.time())
        candidate_watermark = int(context.get("source_watermark") or 0)
        latest_watermark = int(latest["source_watermark"] or 0) if latest is not None else 0
        stale_candidate = bool(
            latest is not None and (
                latest_watermark > candidate_watermark
                or (latest_watermark == candidate_watermark and float(latest["compiled_at"] or 0.0) > candidate_time)
            )
        )
        conn.execute(
            """INSERT INTO working_contexts (
                context_id, run_scope, run_id, workflow_id, task_id, node_id,
                agent_role, context_fingerprint, source_version, source_watermark,
                payload_json, metrics_json, compiled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                context["context_id"], context["run_scope"], context.get("run_id"),
                context.get("workflow_id"), context["task_id"], context.get("node_id"),
                context["agent_role"], context["context_fingerprint"],
                context.get("source_version"), int(context.get("source_watermark") or 0),
                payload_json, metrics_json, candidate_time,
            ),
        )
        row = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?",
            (str(context["context_id"]),),
        ).fetchone()
        if row is None:
            raise RuntimeError("working context insert was not readable")
        conn.commit()
        result = _decode_working_context_row(row)
        if stale_candidate:
            result["_stale_snapshot"] = True
        return result
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

def register_working_context_source(
    *,
    run_scope: str,
    workflow_id: Optional[str],
    task_id: str,
    source_version: str,
    db_path: Optional[Path] = None,
) -> int:
    """Register a task-specific source projection and return its revision."""
    if not run_scope or not source_version:
        raise ValueError("source scope and version are required")
    if not task_id:
        raise ValueError("task-specific source head requires task_id")
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        normalized_workflow_id = str(workflow_id or "")
        normalized_task_id = str(task_id)
        row = conn.execute(
            """
            SELECT source_version, revision
            FROM working_context_source_heads
            WHERE run_scope = ? AND workflow_id = ? AND task_id = ?
            """,
            (str(run_scope), normalized_workflow_id, normalized_task_id),
        ).fetchone()
        if row is not None and row["source_version"] == str(source_version):
            revision = int(row["revision"] or 0)
        else:
            revision = int(row["revision"] or 0) + 1 if row is not None else 1
            conn.execute(
                """
                INSERT INTO working_context_source_heads
                    (run_scope, workflow_id, task_id, source_version, revision, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_scope, workflow_id, task_id) DO UPDATE SET
                    source_version = excluded.source_version,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    str(run_scope),
                    normalized_workflow_id,
                    normalized_task_id,
                    str(source_version),
                    revision,
                    time.time(),
                ),
            )
        conn.commit()
        return revision
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()

def working_context_source_is_current(
    context: Dict[str, Any], db_path: Optional[Path] = None,
) -> bool:
    """Check that a candidate still belongs to the current source generation."""
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT source_version, revision
            FROM working_context_source_heads
            WHERE run_scope = ? AND workflow_id = ? AND task_id = ?
            """,
            (
                str(context.get("run_scope") or ""),
                str(context.get("workflow_id") or ""),
                str(context.get("task_id") or ""),
            ),
        ).fetchone()
        return bool(
            row is not None
            and row["source_version"] == str(context.get("source_version") or "")
            and int(row["revision"] or 0) == int(context.get("source_watermark") or 0)
        )
    finally:
        conn.close()

def record_working_context_metric(
    context: Dict[str, Any],
    *,
    reused: bool,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record one compile invocation separately from immutable snapshots."""
    conn = _facade().get_db_connection(db_path)
    try:
        metric = {
            "metric_id": f"wcm_{uuid.uuid4().hex}",
            "context_id": str(context.get("context_id") or ""),
            "run_scope": str(context.get("run_scope") or ""),
            "run_id": context.get("run_id"),
            "workflow_id": context.get("workflow_id"),
            "task_id": str(context.get("task_id") or ""),
            "agent_role": str(context.get("agent_role") or ""),
            "reused": 1 if reused else 0,
            "changed": 0 if reused else 1,
            "created_at": time.time(),
        }
        if not metric["context_id"] or not metric["run_scope"] or not metric["task_id"]:
            raise ValueError("context metric identity is incomplete")
        conn.execute(
            """INSERT INTO working_context_metric_events (
                metric_id, context_id, run_scope, run_id, workflow_id, task_id,
                agent_role, reused, changed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(metric.values()),
        )
        conn.commit()
        return metric
    finally:
        conn.close()

def get_working_context(
    context_id: str, db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?", (str(context_id),),
        ).fetchone()
        return _decode_working_context_row(row) if row is not None else None
    finally:
        conn.close()

def get_latest_working_context(
    task_id: str,
    agent_role: Optional[str] = None,
    db_path: Optional[Path] = None,
    run_scope: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM working_contexts wc WHERE wc.task_id = ?"
        params: List[Any] = [str(task_id)]
        if agent_role is not None:
            query += " AND wc.agent_role = ?"
            params.append(str(agent_role))
        if run_scope is not None:
            query += " AND wc.run_scope = ?"
            params.append(str(run_scope))
        if workflow_id is not None:
            query += " AND wc.workflow_id IS ?"
            params.append(str(workflow_id))
        # source_watermark is only comparable within one source head, whose
        # identity is (run_scope, workflow_id, task_id). Across heads
        # (scope or workflow unpinned), wall-clock order decides; rows that
        # are stale within their own head are excluded so a late old write
        # can never become latest.
        if run_scope is not None and workflow_id is not None:
            query += " ORDER BY wc.source_watermark DESC, wc.compiled_at DESC, wc.rowid DESC LIMIT 1"
        else:
            query += """ AND wc.source_watermark = (
                SELECT MAX(w2.source_watermark) FROM working_contexts w2
                WHERE w2.task_id = wc.task_id
                  AND w2.run_scope = wc.run_scope
                  AND w2.workflow_id IS wc.workflow_id
                  AND w2.agent_role = wc.agent_role
            ) ORDER BY wc.compiled_at DESC, wc.rowid DESC LIMIT 1"""
        row = conn.execute(query, params).fetchone()
        return _decode_working_context_row(row) if row is not None else None
    finally:
        conn.close()

def list_working_contexts(
    task_id: str,
    agent_role: Optional[str] = None,
    db_path: Optional[Path] = None,
    run_scope: Optional[str] = None,
    workflow_id: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        query = "SELECT * FROM working_contexts WHERE task_id = ?"
        params: List[Any] = [str(task_id)]
        if agent_role is not None:
            query += " AND agent_role = ?"
            params.append(str(agent_role))
        if run_scope is not None:
            query += " AND run_scope = ?"
            params.append(str(run_scope))
        if workflow_id is not None:
            query += " AND workflow_id = ?"
            params.append(str(workflow_id))
        query += " ORDER BY compiled_at ASC, rowid ASC LIMIT ? OFFSET ?"
        params.extend([max(0, int(limit)), max(0, int(offset))])
        return [_decode_working_context_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()
