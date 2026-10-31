"""Startup-window lifecycle primitives: deadline, bounded reason, orphan reclaim.

Pure core: this module must not import ``herdr.projects`` or
``services.controller`` so the Factory, the Controller watchdog and the state
layer can all depend on it without creating a cycle.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from herdr import state_db
from herdr.liveness import FIXTURE_NAME_MARKERS, TEMP_PATH_MARKERS
from herdr.state.transition_gate import transition_workflow
from herdr.transitions import InvalidTransitionError

STARTUP_DEADLINE_ENV = "HERDR_WORKFLOW_STARTUP_DEADLINE_SECONDS"
DEFAULT_STARTUP_DEADLINE_SECONDS = 1800
# Deep Preflight subprocess timeout is 210s; the deadline must stay above it.
MINIMUM_STARTUP_DEADLINE_SECONDS = 211
MAX_STARTUP_REASON_BYTES = 2048
SOURCE_FACTORY = "herdr-factory"
SOURCE_WATCHDOG = "controller-startup-watchdog"

_warned_invalid_deadline = False


def startup_deadline_seconds(
    env: Mapping[str, str] | None = None,
    warn: Callable[[str], None] | None = None,
) -> int:
    """Resolve the startup deadline; invalid/too-small/overflow falls back once."""
    global _warned_invalid_deadline
    env = os.environ if env is None else env
    raw = str(env.get(STARTUP_DEADLINE_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_STARTUP_DEADLINE_SECONDS
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value < MINIMUM_STARTUP_DEADLINE_SECONDS:
        if not _warned_invalid_deadline:
            _warned_invalid_deadline = True
            emitter = warn or (lambda msg: print(msg, file=sys.stderr))
            emitter(
                f"[STARTUP WATCHDOG CONFIG] invalid {STARTUP_DEADLINE_ENV}={raw!r}; "
                f"falling back to {DEFAULT_STARTUP_DEADLINE_SECONDS}s"
            )
        return DEFAULT_STARTUP_DEADLINE_SECONDS
    return value


def _truncate_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def bound_startup_reason(value: object, limit: int = MAX_STARTUP_REASON_BYTES) -> str:
    """Redact then bound to ``limit`` UTF-8 bytes (never split a code point)."""
    text = str(value or "").strip()
    # Delayed import: the Controller's steady-state sweep must not load the
    # review-bot dependency graph.
    from herdr.pr_review_bot import redact_sensitive_content

    return _truncate_utf8(redact_sensitive_content(text), limit)


def describe_exception(exc: BaseException) -> str:
    detail = str(exc).strip() or exc.__class__.__name__
    return bound_startup_reason(f"{type(exc).__name__}: {detail}")


def registration_receipt_owns(receipt: Mapping[str, Any] | None, workflow_id: str) -> bool:
    """True only for the receipt returned after this call's registration commit.

    A row that already uses ``workflow_id`` is not ownership. The receipt is the
    dict ``register_workflow`` returns once its write transaction has committed:
    ``ok``, the same workflow id, and the ``workflow_created`` event id. Two
    replay shapes describe pre-existing registrations and are rejected: a
    ``deduplicated`` receipt replays an existing creation event, and a
    ``from_status`` of anything but fresh/pending means the row already lived
    before this call (legacy rows may lack the creation event). Accepting
    either would let a force retry claim — and on preflight failure converge —
    someone else's live startup.
    """
    if not isinstance(receipt, Mapping):
        return False
    if receipt.get("ok") is not True:
        return False
    if receipt.get("workflow_id") != workflow_id:
        return False
    if receipt.get("deduplicated") is True:
        return False
    from_status = receipt.get("from_status")
    if isinstance(from_status, str) and from_status.strip() not in ("", "pending"):
        return False
    event_id = receipt.get("event_id")
    if isinstance(event_id, bool) or event_id is None:
        return False
    if isinstance(event_id, int):
        return event_id > 0
    if isinstance(event_id, str):
        return bool(event_id.strip())
    return False


def startup_failure_metadata(stage: str, reason: str) -> dict[str, Any]:
    return {
        "outcome": "startup_failed",
        "startup_ready": False,
        "failed_stage": stage if stage in {"preflight", "startup"} else "startup",
        "startup_failure_reason": bound_startup_reason(reason),
    }


def is_startup_orphan(
    record: Mapping[str, Any],
    task_count: int,
    now: float,
    deadline_seconds: int,
    *,
    foreign: bool = False,
) -> bool:
    """Strict predicate: running + explicit ``startup_ready is False`` + no tasks + expired."""
    if foreign:
        return False
    if record.get("status") != "running":
        return False
    if record.get("startup_ready") is not False:
        return False
    try:
        if int(task_count) != 0:
            return False
        created_at = float(record.get("created_at") or 0)
    except (TypeError, ValueError):
        return False
    return created_at < float(now) - int(deadline_seconds)


def _read_startup_row(conn, workflow_id: str):
    row = conn.execute(
        "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    if row is None:
        return None, None
    task_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()[0]
    record = json.loads(row["metadata_json"] or "{}")
    record.update({
        "workflow_id": row["workflow_id"],
        "status": row["status"],
        "created_at": row["created_at"],
    })
    return record, task_count


def _condemn(conn, workflow_id, failed_stage, reason, source):
    try:
        result = transition_workflow(
            workflow_id,
            "completed",
            reason=reason,
            source=source,
            metadata=startup_failure_metadata(failed_stage, reason),
            expected_status="running",
            conn=conn,
        )
    except InvalidTransitionError:
        conn.execute("ROLLBACK")
        return {"ok": False, "reason": "state_changed"}
    conn.execute("COMMIT")
    return {"ok": True, **result}


def converge_startup_failure(
    store,
    workflow_id: str,
    failed_stage: str,
    reason: str,
    *,
    source: str = SOURCE_FACTORY,
) -> dict[str, Any]:
    """Factory convergence: running + not-ready + zero tasks, no deadline/foreign check."""
    conn = state_db.get_db_connection(Path(store.db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        record, task_count = _read_startup_row(conn, workflow_id)
        if record is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        if (record.get("status") != "running"
                or record.get("startup_ready") is not False
                or task_count != 0):
            conn.execute("ROLLBACK")
            return {"ok": False, "reason": "condition_not_met"}
        bounded = bound_startup_reason(reason)
        return _condemn(conn, workflow_id, failed_stage, bounded, source)
    except BaseException:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


_FOREIGN_MARKERS = tuple(TEMP_PATH_MARKERS) + tuple(FIXTURE_NAME_MARKERS)
_FOREIGN_FILTER_SQL = "".join(
    "\n  AND CASE WHEN json_valid(w.metadata_json)"
    " THEN COALESCE(json_extract(w.metadata_json, '$.workflow_file'), '')"
    " ELSE '' END NOT LIKE ?"
    for _ in _FOREIGN_MARKERS
)

_CURSOR_CLAUSE_SQL = (
    "\n  AND (w.created_at > ? OR (w.created_at = ? AND w.workflow_id > ?))"
)

_CANDIDATE_SQL_TEMPLATE = """
SELECT w.workflow_id
FROM workflows AS w
WHERE w.status = 'running'
  AND CASE WHEN json_valid(w.metadata_json)
           THEN json_type(w.metadata_json, '$.startup_ready') END = 'false'
  AND w.created_at < ?{cursor_clause}{foreign_filter}
  AND NOT EXISTS (
      SELECT 1 FROM tasks AS t WHERE t.workflow_id = w.workflow_id
  )
ORDER BY w.created_at ASC, w.workflow_id ASC
LIMIT ?
"""

CANDIDATE_SQL = _CANDIDATE_SQL_TEMPLATE.format(
    cursor_clause="", foreign_filter=_FOREIGN_FILTER_SQL)


def candidate_query_params(
    now: float,
    deadline_seconds: int,
    limit: int,
    *,
    after: tuple[float, str] | None = None,
    exclude_foreign_paths: bool = True,
) -> tuple:
    params: list[Any] = [float(now) - int(deadline_seconds)]
    if after is not None:
        params.extend([float(after[0]), float(after[0]), str(after[1])])
    if exclude_foreign_paths:
        params.extend(f"%{marker}%" for marker in _FOREIGN_MARKERS)
    params.append(max(1, min(int(limit), 100)))
    return tuple(params)


def find_startup_orphan_candidates(
    db_path,
    now: float,
    deadline_seconds: int,
    limit: int = 100,
    *,
    exclude_foreign_paths: bool = True,
    after: tuple[float, str] | None = None,
) -> list[str]:
    """SQL-level candidate query; ``after`` is a bounded keyset cursor for backlog progress."""
    try:
        conn = state_db.get_readonly_db_connection(Path(db_path))
    except FileNotFoundError:
        return []
    sql = _CANDIDATE_SQL_TEMPLATE.format(
        cursor_clause=_CURSOR_CLAUSE_SQL if after is not None else "",
        foreign_filter=_FOREIGN_FILTER_SQL if exclude_foreign_paths else "",
    )
    params = candidate_query_params(
        now, deadline_seconds, limit,
        after=after, exclude_foreign_paths=exclude_foreign_paths)
    try:
        rows = conn.execute(sql, params).fetchall()
        return [row["workflow_id"] for row in rows if row["workflow_id"]]
    finally:
        conn.close()


def reclaim_startup_orphan(
    store,
    workflow_id: str,
    now: float,
    deadline_seconds: int,
    *,
    foreign_check: Callable[[dict], bool] | None = None,
    source: str = SOURCE_WATCHDOG,
) -> dict[str, Any]:
    """Re-read authoritative fields inside ``BEGIN IMMEDIATE``, then reclaim once."""
    if foreign_check is None:
        from herdr.liveness import workflow_is_foreign
        foreign_check = workflow_is_foreign

    conn = state_db.get_db_connection(Path(store.db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        record, task_count = _read_startup_row(conn, workflow_id)
        if record is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        if not is_startup_orphan(
            record, task_count, now, deadline_seconds,
            foreign=bool(foreign_check(record)),
        ):
            conn.execute("ROLLBACK")
            return {"ok": False, "reason": "condition_not_met"}

        reason = bound_startup_reason(
            f"startup deadline exceeded: created_at={record.get('created_at')} "
            f"deadline={deadline_seconds}s tasks={task_count}"
        )
        return _condemn(conn, workflow_id, "startup", reason, source)
    except BaseException:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
