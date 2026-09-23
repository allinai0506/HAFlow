"""Eval and replay persistence (herdr/eval_store.py).

Eval results are point-in-time facts for one run revision. Replay specs are
lineage edges from a source run to a replay run. Both tables deliberately
carry no foreign keys so workflow deletion never removes audit history.

Design rules:
- Eval is distinct from Metrics aggregation; this module never imports it.
- Nulls stay null; malformed JSON degrades to None without raising.
- Recording a replay spec never writes trajectory or task state.
- Revision allocation uses BEGIN IMMEDIATE with max+1 and conflict retry.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import state_db

_EVAL_RETRY_LIMIT = 5


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _encode_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _decode_json_nullable(raw: Any) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _decode_eval_row(row: sqlite3.Row) -> Dict[str, Any]:
    names = set(row.keys())
    warnings = _decode_json_nullable(row["warnings_json"]) if "warnings_json" in names else None
    return {
        "eval_id": row["eval_id"],
        "run_id": row["run_id"],
        "revision": int(row["revision"]),
        "evidence": _decode_json_nullable(row["evidence_json"]),
        "requirements_satisfied": _decode_bool(row["requirements_satisfied"]) if "requirements_satisfied" in names else None,
        "verification_passed": _decode_bool(row["verification_passed"]) if "verification_passed" in names else None,
        "human_intervention_count": row["human_intervention_count"] if "human_intervention_count" in names else None,
        "final_status": row["final_status"] if "final_status" in names else None,
        "warnings": list(warnings) if isinstance(warnings, list) else [],
        "task_id": row["task_id"] if "task_id" in names else None,
        "workflow_id": row["workflow_id"] if "workflow_id" in names else None,
        "created_at": float(row["created_at"]),
    }


def _decode_replay_row(row: sqlite3.Row) -> Dict[str, Any]:
    names = set(row.keys())
    snapshot = row["snapshot_path"] if "snapshot_path" in names else None
    policy = _decode_json_nullable(row["policy_json"]) if "policy_json" in names else None
    return {
        "spec_id": row["spec_id"],
        "source_run_id": row["source_run_id"],
        "replay_run_id": row["replay_run_id"],
        "workflow_id": row["workflow_id"],
        "definition": _decode_json_nullable(row["definition_json"]),
        "lineage": _decode_json_nullable(row["lineage_json"]),
        "snapshot": snapshot,
        "snapshot_path": snapshot,
        "frozen_config_ref": snapshot,
        "policy": policy,
        "policy_override": policy,
        "created_at": float(row["created_at"]),
    }


def _encode_bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _decode_bool(raw: Any) -> bool | None:
    if raw is None:
        return None
    return bool(raw)


def _ensure_eval_replay_schema(conn: sqlite3.Connection) -> None:
    try:
        state_db._ensure_eval_replay_columns(conn)
    except AttributeError:
        return


def _open(db_path: Optional[Path] = None) -> sqlite3.Connection:
    return state_db.get_db_connection(db_path)


def record_eval_result(
    run_id: str,
    *,
    revision: Optional[int] = None,
    evidence: Optional[Any] = None,
    eval_id: Optional[str] = None,
    created_at: Optional[float] = None,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    requirements_satisfied: bool | None = None,
    verification_passed: bool | None = None,
    human_intervention_count: int | None = None,
    final_status: str | None = None,
    warnings: list | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
) -> Dict[str, Any]:
    """Record one eval fact; same (run_id, revision) returns the existing row."""
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    if revision is not None and (not isinstance(revision, int) or revision < 1):
        raise ValueError("revision must be a positive int")
    for field, value in (
        ("requirements_satisfied", requirements_satisfied),
        ("verification_passed", verification_passed),
    ):
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{field} must be a bool or None")
    if human_intervention_count is not None and (
        not isinstance(human_intervention_count, int)
        or isinstance(human_intervention_count, bool)
        or human_intervention_count < 0
    ):
        raise ValueError("human_intervention_count must be a non-negative int or None")

    if conn is not None:
        return _record_eval_in_conn(
            conn,
            run_id,
            revision=revision,
            evidence=evidence,
            eval_id=eval_id,
            created_at=created_at,
            requirements_satisfied=requirements_satisfied,
            verification_passed=verification_passed,
            human_intervention_count=human_intervention_count,
            final_status=final_status,
            warnings=warnings,
            task_id=task_id,
            workflow_id=workflow_id,
        )

    last_error: Optional[Exception] = None
    for _ in range(_EVAL_RETRY_LIMIT):
        owned = _open(db_path)
        try:
            _ensure_eval_replay_schema(owned)
            owned.execute("BEGIN IMMEDIATE;")
            try:
                result = _record_eval_in_conn(
                    owned,
                    run_id,
                    revision=revision,
                    evidence=evidence,
                    eval_id=eval_id,
                    created_at=created_at,
                    requirements_satisfied=requirements_satisfied,
                    verification_passed=verification_passed,
                    human_intervention_count=human_intervention_count,
                    final_status=final_status,
                    warnings=warnings,
                    task_id=task_id,
                    workflow_id=workflow_id,
                )
            except sqlite3.IntegrityError as exc:
                last_error = exc
                try:
                    owned.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                continue
            owned.execute("COMMIT;")
            return result
        except sqlite3.IntegrityError as exc:
            last_error = exc
            try:
                owned.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            continue
        finally:
            owned.close()
    # A lost race resolves to the winner row when the caller fixed revision.
    if revision is not None:
        existing = get_eval_result(run_id, revision, db_path=db_path)
        if existing is not None:
            return existing
    if last_error is not None:
        raise last_error
    raise RuntimeError("eval record failed without a database error")


def _record_eval_in_conn(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    revision: Optional[int],
    evidence: Optional[Any],
    eval_id: Optional[str],
    created_at: Optional[float],
    requirements_satisfied: bool | None = None,
    verification_passed: bool | None = None,
    human_intervention_count: int | None = None,
    final_status: str | None = None,
    warnings: list | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
) -> Dict[str, Any]:
    target = revision
    if target is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS max_rev "
            "FROM eval_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        target = int(row["max_rev"] or 0) + 1
    existing = conn.execute(
        "SELECT * FROM eval_results WHERE run_id = ? AND revision = ?",
        (run_id, int(target)),
    ).fetchone()
    if existing is not None:
        return _decode_eval_row(existing)
    now = float(created_at) if created_at is not None else time.time()
    conn.execute(
        "INSERT INTO eval_results "
        "(eval_id, run_id, revision, verdict, scores_json, "
        "evidence_json, requirements_satisfied, verification_passed, "
        "human_intervention_count, final_status, warnings_json, "
        "task_id, workflow_id, created_at) "
        "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eval_id or _new_id("eval"),
            run_id,
            int(target),
            _encode_json(evidence),
            _encode_bool(requirements_satisfied),
            _encode_bool(verification_passed),
            human_intervention_count,
            final_status,
            _encode_json(list(warnings) if warnings is not None else []),
            task_id,
            workflow_id,
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM eval_results WHERE run_id = ? AND revision = ?",
        (run_id, int(target)),
    ).fetchone()
    if row is None:
        raise RuntimeError("eval insert was not readable")
    return _decode_eval_row(row)


def get_eval_result(
    run_id: str,
    revision: int,
    *,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one eval row; absent rows return None."""
    if not str(run_id or "").strip():
        raise ValueError("run_id is required")
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM eval_results WHERE run_id = ? AND revision = ?",
            (str(run_id), int(revision)),
        ).fetchone()
        return _decode_eval_row(row) if row is not None else None
    finally:
        conn.close()


def get_latest_eval_result(
    run_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch the max-revision eval row; absent runs return None."""
    if not str(run_id or "").strip():
        raise ValueError("run_id is required")
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM eval_results WHERE run_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (str(run_id),),
        ).fetchone()
        return _decode_eval_row(row) if row is not None else None
    finally:
        conn.close()


def list_eval_results(
    run_id: Optional[str] = None,
    *,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List eval rows ordered by revision for one run, or all runs."""
    conn = _open(db_path)
    try:
        query = "SELECT * FROM eval_results"
        params: List[Any] = []
        if run_id is not None:
            query += " WHERE run_id = ?"
            params.append(str(run_id))
        query += " ORDER BY run_id ASC, revision ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(query, tuple(params)).fetchall()
        return [_decode_eval_row(row) for row in rows]
    finally:
        conn.close()


def get_max_eval_revision(
    run_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[int]:
    """Return the max revision for a run; absent runs return None."""
    if not str(run_id or "").strip():
        raise ValueError("run_id is required")
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(revision) AS max_rev FROM eval_results WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        value = row["max_rev"] if row is not None else None
        return int(value) if value is not None else None
    finally:
        conn.close()


def record_replay_spec(
    source_run_id: str,
    replay_run_id: str,
    *,
    workflow_id: Optional[str] = None,
    definition: Optional[Any] = None,
    lineage: Optional[Any] = None,
    spec_id: Optional[str] = None,
    created_at: Optional[float] = None,
    db_path: Optional[Path] = None,
    snapshot: str | None = None,
    snapshot_path: str | None = None,
    frozen_config_ref: str | None = None,
    policy: dict | None = None,
    policy_override: dict | None = None,
) -> Dict[str, Any]:
    """Record one replay edge; same replay_run_id returns the existing row."""
    source_run_id = str(source_run_id or "").strip()
    replay_run_id = str(replay_run_id or "").strip()
    if not source_run_id or not replay_run_id:
        raise ValueError("source_run_id and replay_run_id are required")
    if source_run_id == replay_run_id:
        raise ValueError("source_run_id and replay_run_id must differ")
    resolved_snapshot = snapshot if snapshot is not None else (
        snapshot_path if snapshot_path is not None else frozen_config_ref
    )
    resolved_policy = policy if policy is not None else policy_override
    if resolved_policy is not None and not isinstance(resolved_policy, dict):
        raise ValueError("policy must be a mapping or None")
    conn = _open(db_path)
    try:
        _ensure_eval_replay_schema(conn)
        existing = conn.execute(
            "SELECT * FROM replay_specs WHERE replay_run_id = ?",
            (replay_run_id,),
        ).fetchone()
        if existing is not None:
            return _decode_replay_row(existing)
        now = float(created_at) if created_at is not None else time.time()
        try:
            conn.execute(
                "INSERT INTO replay_specs "
                "(spec_id, source_run_id, replay_run_id, workflow_id, "
                "definition_json, lineage_json, snapshot_path, policy_json, "
                "created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec_id or _new_id("rpl"),
                    source_run_id,
                    replay_run_id,
                    workflow_id,
                    _encode_json(definition),
                    _encode_json(lineage),
                    resolved_snapshot,
                    _encode_json(resolved_policy),
                    now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            winner = conn.execute(
                "SELECT * FROM replay_specs WHERE replay_run_id = ?",
                (replay_run_id,),
            ).fetchone()
            if winner is not None:
                return _decode_replay_row(winner)
            raise
        row = conn.execute(
            "SELECT * FROM replay_specs WHERE replay_run_id = ?",
            (replay_run_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("replay insert was not readable")
        return _decode_replay_row(row)
    finally:
        conn.close()


def get_replay_spec(
    replay_run_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one replay spec by replay run; absent runs return None."""
    if not str(replay_run_id or "").strip():
        raise ValueError("replay_run_id is required")
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM replay_specs WHERE replay_run_id = ?",
            (str(replay_run_id),),
        ).fetchone()
        return _decode_replay_row(row) if row is not None else None
    finally:
        conn.close()


def delete_replay_spec(replay_run_id: str, *, db_path: Path | None = None) -> bool:
    """Remove one replay edge during compensation before a successful return."""
    conn = _open(db_path)
    try:
        cur = conn.execute("DELETE FROM replay_specs WHERE replay_run_id = ?",
                           (str(replay_run_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_replay_spec_by_id(
    spec_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one replay spec by primary key; absent ids return None."""
    if not str(spec_id or "").strip():
        raise ValueError("spec_id is required")
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM replay_specs WHERE spec_id = ?",
            (str(spec_id),),
        ).fetchone()
        return _decode_replay_row(row) if row is not None else None
    finally:
        conn.close()


def list_replay_specs(
    source_run_id: Optional[str] = None,
    *,
    workflow_id: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List replay specs with optional source and workflow filters."""
    conn = _open(db_path)
    try:
        query = "SELECT * FROM replay_specs WHERE 1=1"
        params: List[Any] = []
        if source_run_id is not None:
            query += " AND source_run_id = ?"
            params.append(str(source_run_id))
        if workflow_id is not None:
            query += " AND workflow_id = ?"
            params.append(str(workflow_id))
        query += " ORDER BY created_at ASC, spec_id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(query, tuple(params)).fetchall()
        return [_decode_replay_row(row) for row in rows]
    finally:
        conn.close()


def get_replay_lineage(
    replay_run_id: str,
    *,
    db_path: Optional[Path] = None,
) -> List[str]:
    """Walk source edges back to the origin run.

    Returns run ids from the oldest ancestor to the given replay run.
    Absent replay runs return an empty list.
    """
    if not str(replay_run_id or "").strip():
        raise ValueError("replay_run_id is required")
    conn = _open(db_path)
    try:
        by_replay: Dict[str, str] = {}
        for row in conn.execute(
            "SELECT source_run_id, replay_run_id FROM replay_specs"
        ).fetchall():
            by_replay[str(row["replay_run_id"])] = str(row["source_run_id"])
    finally:
        conn.close()
    target = str(replay_run_id)
    if target not in by_replay:
        return []
    chain = [target]
    seen = {target}
    current = target
    for _ in range(100):
        source = by_replay.get(current)
        if source is None:
            break
        if source in seen:
            break
        chain.append(source)
        seen.add(source)
        current = source
        if current not in by_replay:
            break
    chain.reverse()
    return chain


def latest_run_verification_fact(
    run_id: str,
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Return the latest strict verification fact for one run.

    The fact delegates to the trajectory ledger rows. Only a dict payload
    carrying a boolean ``passed`` flag is accepted; missing flags, loose
    truthy values, and malformed payloads all return None.
    """
    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("run_id is required")
    owned = conn is None
    active = conn if conn is not None else _open(db_path)
    try:
        try:
            rows = state_db._trajectory_rows_in_conn(
                active,
                run_id,
                event_type="verification_completed",
                limit=1,
                desc=True,
            )
        except Exception:
            return None
        if not rows:
            return None
        row = rows[0]
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return None
        verification = payload.get("verification")
        if not isinstance(verification, dict):
            return None
        passed = verification.get("passed")
        # Strict check: only a real boolean counts. Loose truthy or
        # missing values from permissive variants are rejected.
        if not isinstance(passed, bool):
            return None
        return {
            "run_id": run_id,
            "event_id": row.get("event_id") or f"evt_{row.get('id')}",
            "sequence": row.get("sequence"),
            "passed": passed,
            "evidence_id": verification.get("evidence_id"),
            "observation_id": verification.get("observation_id"),
            "timestamp": row.get("timestamp"),
        }
    finally:
        if owned:
            active.close()


# Compatibility aliases for alternate caller spellings.
save_eval_result = record_eval_result
create_eval_result = record_eval_result
save_replay_spec = record_replay_spec
create_replay_spec = record_replay_spec


__all__ = [
    "create_eval_result",
    "create_replay_spec",
    "delete_replay_spec",
    "get_eval_result",
    "get_latest_eval_result",
    "get_max_eval_revision",
    "get_replay_lineage",
    "get_replay_spec",
    "get_replay_spec_by_id",
    "latest_run_verification_fact",
    "list_eval_results",
    "list_replay_specs",
    "record_eval_result",
    "record_replay_spec",
    "save_eval_result",
    "save_replay_spec",
]
