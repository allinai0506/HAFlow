"""outcome route persistence; extracted without changing behavior."""

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from ._facade import _facade
from .connection import _open_shadow_read_connection
from .constants import OUTCOME_LOOKUP_CHUNK, OUTCOME_PER_AGENT_LIMIT, OUTCOME_SCHEMA_VERSION, ROUTE_DECISION_DEFAULT_LIMIT, ROUTE_DECISION_MAX_LIMIT, _OUTCOME_COLUMNS, _route_decision_bucket_exprs


def _outcome_autofinalize_enabled() -> bool:
    """Kill-switch for write-path auto-finalization.

    Tests seed historical timestamps and finalize explicitly; production
    leaves the default on so terminal task/eval writes settle outcomes.
    """
    return os.environ.get("HERDR_OUTCOME_AUTOFINALIZE", "1") != "0"

def _decode_outcome_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "outcome_id": row["outcome_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "agent": row["agent"],
        "node": row["node"],
        "task_type": row["task_type"] or "",
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "wall_time_seconds": row["wall_time_seconds"],
        "final_status": row["final_status"],
        "requirements_satisfied": bool(row["requirements_satisfied"]),
        "verification_passed": bool(row["verification_passed"]),
        "qualified_success": bool(row["qualified_success"]),
        "rework_count": int(row["rework_count"] or 0),
        "blocked_count": int(row["blocked_count"] or 0),
        "human_intervention_count": int(row["human_intervention_count"] or 0),
        "recorded_at": float(row["recorded_at"]),
        "source_eval_id": row["source_eval_id"],
        "source_eval_revision": int(row["source_eval_revision"]),
        "source_task_version": int(row["source_task_version"] or 0),
        "schema_version": int(row["schema_version"] or 0),
    }

def insert_execution_outcome(
    outcome: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Immutable insert of one settled execution outcome.

    There is deliberately no update path. A repeat insert for the same
    (task_id, run_id) is an idempotent no-op returning the stored row.
    """
    required = (
        "outcome_id", "run_id", "task_id", "workflow_id", "agent", "node",
        "final_status", "source_eval_id", "source_eval_revision",
        "recorded_at",
    )
    missing = [key for key in required if outcome.get(key) in (None, "")]
    if missing:
        raise ValueError(f"outcome missing required fields: {missing}")

    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True
    try:
        conn.execute(
            "INSERT INTO agent_execution_outcomes "
            f"({_OUTCOME_COLUMNS}) VALUES ("
            + ", ".join(["?"] * 22) + ") "
            "ON CONFLICT(task_id, run_id) DO NOTHING",
            (
                str(outcome["outcome_id"]),
                str(outcome["run_id"]),
                str(outcome["task_id"]),
                str(outcome.get("workflow_id") or ""),
                str(outcome["agent"]),
                str(outcome["node"]),
                str(outcome.get("task_type") or ""),
                outcome.get("started_at"),
                outcome.get("finished_at"),
                outcome.get("wall_time_seconds"),
                str(outcome["final_status"]),
                1 if outcome.get("requirements_satisfied") else 0,
                1 if outcome.get("verification_passed") else 0,
                1 if outcome.get("qualified_success") else 0,
                int(outcome.get("rework_count") or 0),
                int(outcome.get("blocked_count") or 0),
                int(outcome.get("human_intervention_count") or 0),
                float(outcome["recorded_at"]),
                str(outcome["source_eval_id"]),
                int(outcome["source_eval_revision"]),
                int(outcome.get("source_task_version") or 0),
                int(outcome.get("schema_version") or OUTCOME_SCHEMA_VERSION),
            ),
        )
        if should_close:
            conn.commit()
        row = conn.execute(
            "SELECT * FROM agent_execution_outcomes "
            "WHERE task_id = ? AND run_id = ?",
            (str(outcome["task_id"]), str(outcome["run_id"])),
        ).fetchone()
        if row is None:
            raise RuntimeError("outcome insert was not readable")
        return _decode_outcome_row(row)
    finally:
        if should_close:
            conn.close()

def get_execution_outcome(
    task_id: str,
    run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one immutable outcome by identity; absent pairs return None."""
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_execution_outcomes "
            "WHERE task_id = ? AND run_id = ?",
            (str(task_id), str(run_id)),
        ).fetchone()
        return _decode_outcome_row(row) if row is not None else None
    finally:
        conn.close()

def read_execution_outcome(
    task_id: str,
    run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read one settled outcome without creating or migrating the database.

    ``get_execution_outcome`` opens a writable connection and may create the
    file. Quality assessment must not use that path.
    """
    conn = _facade().get_readonly_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_execution_outcomes "
            "WHERE task_id = ? AND run_id = ?",
            (str(task_id), str(run_id)),
        ).fetchone()
        return _decode_outcome_row(row) if row is not None else None
    finally:
        conn.close()

def query_execution_outcomes(
    *,
    agents: Optional[List[str]],
    node: str,
    task_type: str,
    before: float,
    exclude_run_id: Optional[str] = None,
    per_agent_limit: Optional[int] = None,
    lookback_floor: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read settled outcomes: one newest-first slice per candidate agent.

    The router hot path only ever touches this table. Bucket equality on
    (agent, node, task_type) plus recorded_at ordering is served by
    idx_outcomes_bucket; no JSON extraction, no ownership joins, no
    mutable-state reconstruction at query time.
    """
    capped = int(per_agent_limit) if per_agent_limit is not None else OUTCOME_PER_AGENT_LIMIT
    if capped < 1:
        raise ValueError("per_agent_limit must be a positive int")
    if not str(node or "").strip():
        raise ValueError("node is required")
    scopes: List[Optional[str]] = (
        [str(agent) for agent in agents] if agents is not None else [None]
    )
    conn = _facade().get_db_connection(db_path)
    try:
        outcomes: List[Dict[str, Any]] = []
        for agent in scopes:
            query = (
                f"SELECT {_OUTCOME_COLUMNS} FROM agent_execution_outcomes "
                "WHERE node = ? AND task_type = ? AND recorded_at < ?"
            )
            params: List[Any] = [str(node), str(task_type or ""), float(before)]
            if agent is not None:
                query += " AND agent = ?"
                params.append(str(agent))
            else:
                query += " AND agent IS NOT NULL AND agent <> ''"
            if exclude_run_id:
                query += " AND run_id <> ?"
                params.append(str(exclude_run_id))
            if lookback_floor is not None:
                query += " AND recorded_at >= ?"
                params.append(float(lookback_floor))
            query += " ORDER BY recorded_at DESC, outcome_id DESC LIMIT ?"
            params.append(capped)
            for row in conn.execute(query, tuple(params)).fetchall():
                outcomes.append(_decode_outcome_row(row))
        return outcomes
    finally:
        conn.close()


#: Default newest-first window for shadow-evaluation decision reads.

def batch_get_execution_outcomes(
    pairs: List[Any],
    db_path: Optional[Path] = None,
) -> Dict[Any, Dict[str, Any]]:
    """Point-lookup many (task_id, run_id) outcomes in chunked queries.

    Shadow Evaluation's bounded read path: always observe-only. The
    connection comes from ``_open_shadow_read_connection`` (``mode=ro`` +
    ``query_only`` + capability check), so this lookup never creates,
    initializes or migrates the database it reads.

    One SELECT per chunk (no N+1 per-decision round trips). Missing
    pairs are absent from the result; callers treat absence as
    "no settled outcome", never as failure.
    """
    normalized: List[Any] = []
    seen = set()
    for pair in pairs or []:
        key = (str(pair[0]), str(pair[1]))
        if key[0] and key[1] and key not in seen:
            seen.add(key)
            normalized.append(key)
    found: Dict[Any, Dict[str, Any]] = {}
    if not normalized:
        return found
    conn = _open_shadow_read_connection(db_path)
    try:
        for offset in range(0, len(normalized), OUTCOME_LOOKUP_CHUNK):
            chunk = normalized[offset:offset + OUTCOME_LOOKUP_CHUNK]
            placeholders = ", ".join(["(?, ?)"] * len(chunk))
            params: List[Any] = []
            for task_id, run_id in chunk:
                params.extend([task_id, run_id])
            for row in conn.execute(
                f"SELECT {_OUTCOME_COLUMNS} FROM agent_execution_outcomes "
                f"WHERE (task_id, run_id) IN ({placeholders})",
                tuple(params),
            ).fetchall():
                decoded = _decode_outcome_row(row)
                found[(decoded["task_id"], decoded["run_id"])] = decoded
        return found
    finally:
        conn.close()

@dataclass(frozen=True)
class ExactDecisionBucket:
    """Source-level scoping for a single decision bucket.

    A caller that asks for one exact ``recommended_agent x node x
    task_type`` canary bucket must not have its scan budget consumed by
    every other bucket or by shadow decisions: the budget exists to bound
    the work, not to be spent on rows that provably cannot match. These
    predicates are therefore applied in SQL, before the budget counts
    anything.

    Provided as a whole because it is only meaningful together: scoping
    by recommended agent without the mode would still let shadow
    decisions consume the budget.
    """

    mode: str
    recommended_agent: str
    node: Optional[str] = None
    task_type: Optional[str] = None

def _exact_bucket_sql(bucket: ExactDecisionBucket) -> Tuple[str, List[Any]]:
    """Build the WHERE fragment that scopes a read to one exact bucket.

    The fragments reuse :func:`_route_decision_bucket_exprs` verbatim so
    the read is served by ``idx_events_route_decision_bucket`` instead
    of scanning route_decision history: a sparse bucket must cost its
    own rows, not its siblings'. Malformed payloads evaluate to NULL
    through the CASE wrapper and never match, exactly as the pre-index
    standalone ``json_valid`` clause behaved.
    """
    mode_expr, agent_expr, node_expr, task_type_expr = (
        _route_decision_bucket_exprs())
    clauses: List[str] = []
    params: List[Any] = []
    if bucket.mode == "canary":
        clauses.append(f"{mode_expr} = 'canary'")
    else:
        clauses.append(f"{mode_expr} != 'canary'")
    clauses.append(f"{agent_expr} = ?")
    params.append(str(bucket.recommended_agent))
    if bucket.node is not None:
        clauses.append(f"{node_expr} = ?")
        params.append(str(bucket.node))
    if bucket.task_type is not None:
        clauses.append(f"{task_type_expr} = ?")
        params.append(str(bucket.task_type))
    return " AND ".join(clauses), params

def _route_decisions_sql(
    *,
    limit: int,
    since: Optional[float] = None,
    before: Optional[float] = None,
    before_id: Optional[int] = None,
    exact_bucket: Optional[ExactDecisionBucket] = None,
) -> Tuple[str, List[Any]]:
    """The exact statement ``query_route_decisions`` executes.

    Separate from execution so a test can ``EXPLAIN QUERY PLAN`` the
    byte-identical SQL the read path issues — asserting against a
    paraphrased query would prove nothing about the production plan.
    """
    query = (
        "SELECT id, workflow_id, node_id, task_id, agent_id, timestamp, "
        "source, run_id, payload_json FROM events "
        "WHERE event_type = 'route_decision'"
    )
    params: List[Any] = []
    if since is not None:
        query += " AND timestamp >= ?"
        params.append(float(since))
    if before is not None:
        if before_id is not None:
            query += " AND (timestamp < ? OR (timestamp = ? AND id < ?))"
            params.extend([float(before), float(before), int(before_id)])
        else:
            query += " AND timestamp < ?"
            params.append(float(before))
    if exact_bucket is not None:
        scope_sql, scope_params = _exact_bucket_sql(exact_bucket)
        query += f" AND {scope_sql}"
        params.extend(scope_params)
    query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return query, params

def query_route_decisions(
    *,
    limit: Optional[int] = None,
    since: Optional[float] = None,
    before: Optional[float] = None,
    before_id: Optional[int] = None,
    db_path: Optional[Path] = None,
    exact_bucket: Optional[ExactDecisionBucket] = None,
) -> List[Dict[str, Any]]:
    """Read frozen route_decision events, newest-first, bounded.

    Shadow Evaluation's bounded read path: always observe-only. The
    connection comes from ``_open_shadow_read_connection`` (``mode=ro`` +
    ``query_only`` + capability check), so this query never creates,
    initializes or migrates the database it reads.

    Only the ``route_decision`` event type. A plain scan is served by
    ``idx_events_type(event_type, timestamp)``; an ``exact_bucket`` read
    is served by ``idx_events_route_decision_bucket``, the partial
    expression index built from the same CASE-wrapped extraction
    expressions the WHERE clause uses, so a sparse bucket costs its own
    rows instead of a JSON-evaluating scan over all of history.
    Payloads are decoded in Python. Callers must pass an explicit
    ``limit`` for large histories; the hard cap keeps evaluation off
    the dispatch hot path and far from unbounded loads.

    ``before_id`` pairs with ``before`` as an exact keyset cursor
    ((timestamp, id) paging): ``timestamp < before OR (timestamp =
    before AND id < before_id)``. Without it, ``before`` stays a plain
    timestamp floor for backward compatibility.

    ``exact_bucket`` narrows the scan to one decision bucket in SQL, so
    a caller's row budget is spent only on rows that can match instead of
    being consumed by unrelated buckets. It is opt-in: callers that rely
    on counting the rows the filter removed (the shadow collection's
    ``skipped_canary_events``) must leave it unset.
    """
    capped = ROUTE_DECISION_DEFAULT_LIMIT if limit is None else int(limit)
    if capped < 1:
        raise ValueError("limit must be a positive int")
    capped = min(capped, ROUTE_DECISION_MAX_LIMIT)
    query, params = _route_decisions_sql(
        limit=capped, since=since, before=before, before_id=before_id,
        exact_bucket=exact_bucket)
    conn = _open_shadow_read_connection(db_path)
    try:
        decisions: List[Dict[str, Any]] = []
        for row in conn.execute(query, tuple(params)).fetchall():
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            decisions.append({
                "decision_event_id": int(row["id"]),
                "decision_at": float(row["timestamp"]),
                "workflow_id": row["workflow_id"] or "",
                "node_id": row["node_id"] or "",
                "task_id": row["task_id"] or "",
                "agent_id": row["agent_id"] or "",
                "source": row["source"] or "",
                "run_id": row["run_id"] or "",
                "payload": payload if isinstance(payload, dict) else {},
            })
        return decisions
    finally:
        conn.close()
