"""completion persistence; extracted without changing behavior."""

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional
from ._facade import _facade


def _decode_completion_observation(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "observed_status": row["observed_status"],
        "observed_version": int(row["observed_version"] or 0),
        "observed_updated_at": row["observed_updated_at"],
        "marker_present": bool(row["marker_present"]),
        "agent_status": row["agent_status"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "last_sample_at": row["last_sample_at"],
        "last_observed_at": row["last_observed_at"],
        "consecutive_samples": int(row["consecutive_samples"] or 0),
        "vanished": bool(row["vanished"]),
        "uncertain": bool(row["vanished"]),
        "epoch_changed": bool(row["epoch_changed"]),
    }

def observe_completion(
    task_id: str,
    *,
    marker_present: bool,
    agent_status: Optional[str],
    observed_at: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record one pane observation and return the durable confirmation state.

    Observations are serialized in SQLite.  A task rewrite (including a
    human ``blocked -> working`` reopen) resets the marker epoch, so an old
    screen can never be replayed as evidence for the new execution attempt.
    """
    from herdr import completion as completion_policy

    now = float(observed_at if observed_at is not None else time.time())
    if not math.isfinite(now):
        raise ValueError("observed_at must be finite")
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task_row is None:
            raise ValueError(f"Task '{task_id}' not found")
        task = _facade()._decode_task_row(task_row)
        current_status = str(task.get("status") or "")
        current_version = int(task.get("version") or 0)
        row = conn.execute(
            "SELECT * FROM completion_observations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        previous = _decode_completion_observation(row) if row is not None else None
        epoch_changed = bool(
            previous
            and (
                previous.get("observed_status") != current_status
                or previous.get("observed_version") != current_version
            )
        )
        marker = bool(marker_present)
        sample_stale = False
        if previous is not None:
            previous_observed = previous.get("last_observed_at")
            if previous_observed is None:
                previous_observed = previous.get("last_sample_at")
            if previous_observed is not None:
                try:
                    if float(previous_observed) > now or (
                        float(previous_observed) == now
                        and (
                            bool(previous.get("marker_present")) != marker
                            or previous.get("agent_status") != agent_status
                        )
                    ):
                        sample_stale = True
                except (TypeError, ValueError):
                    sample_stale = True
        if sample_stale:
            saved = conn.execute(
                "SELECT * FROM completion_observations WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            result = _decode_completion_observation(saved)
            try:
                elapsed = now - float(
                    task.get("started_at") or task.get("created_at") or now
                )
            except (TypeError, ValueError):
                elapsed = 0.0
            result.update({
                "ready": False,
                "stale_sample": True,
                "elapsed_seconds": elapsed,
                "task_status": current_status,
            })
            conn.execute("COMMIT;")
            return result
        first_seen = None
        last_seen = None
        last_sample = now
        consecutive = 0
        vanished = False
        was_present = bool(previous.get("marker_present")) if previous else False
        if previous is not None and epoch_changed:
            # A task rewrite starts a fresh epoch.  If the marker is already on
            # the pane at the first sample of that epoch, treat it as the first
            # *new-epoch* sample; the version binding prevents the old sample
            # from completing the reopened task.  Requiring an artificial
            # absent poll here would deadlock a restarted Controller when the
            # pane still contains the marker.
            if marker:
                first_seen = now
                last_seen = now
                last_sample = now
                consecutive = 1
            else:
                vanished = was_present
        elif previous is not None:
            first_seen = previous.get("first_seen_at")
            last_seen = previous.get("last_seen_at")
            last_sample = previous.get("last_sample_at")
            consecutive = int(previous.get("consecutive_samples") or 0)
            vanished = bool(previous.get("vanished"))
            if marker and not was_present:
                first_seen = now
                last_seen = now
                last_sample = now
                consecutive = 1
                vanished = False
            elif marker and was_present:
                # Repeated reads in one sweep do not count as a second poll.
                # A marker already resident at the first observation remains
                # residue until an explicit absent -> present cycle is seen.
                if first_seen is not None:
                    if last_sample is None:
                        last_sample = last_seen
                    try:
                        interval_ok = (
                            last_sample is None
                            or float(now) - float(last_sample)
                            >= completion_policy.min_sample_interval_seconds()
                        )
                    except (TypeError, ValueError):
                        interval_ok = False
                    if interval_ok and (
                        last_sample is None or float(now) >= float(last_sample)
                    ):
                        consecutive += 1
                        last_sample = now
                last_seen = now
            elif not marker and was_present:
                # A later appearance starts a new absent -> present cycle;
                # never revive the old confirmation run.
                first_seen = None
                last_seen = None
                last_sample = now
                consecutive = 0
                vanished = True
        elif marker:
            # A marker already visible at the first observation is residue.
            first_seen = None
            last_seen = now
            last_sample = now
            consecutive = 0
        if marker and first_seen is not None:
            last_seen = now
        elapsed = now - float(
            task.get("started_at")
            or task.get("created_at")
            or now
        )
        ready = bool(
            current_status in {"dispatched", "working"}
            and marker
            and first_seen is not None
            and consecutive >= 2
            and completion_policy.sample_interval_satisfied(first_seen, last_sample)
            and not vanished
            and agent_status in {"idle", "done"}
            and elapsed >= completion_policy.min_completion_seconds()
        )
        values = (
            task_id,
            task.get("workflow_id"),
            current_status,
            current_version,
            task.get("updated_at"),
            int(marker),
            agent_status,
            first_seen,
            last_seen,
            last_sample,
            now,
            consecutive,
            int(vanished),
            int(epoch_changed),
            now,
        )
        conn.execute(
            """INSERT INTO completion_observations (
                task_id, workflow_id, observed_status, observed_version,
                observed_updated_at, marker_present, agent_status,
                first_seen_at, last_seen_at, last_sample_at, last_observed_at,
                consecutive_samples, vanished, epoch_changed, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                workflow_id=excluded.workflow_id,
                observed_status=excluded.observed_status,
                observed_version=excluded.observed_version,
                observed_updated_at=excluded.observed_updated_at,
                marker_present=excluded.marker_present,
                agent_status=excluded.agent_status,
                first_seen_at=excluded.first_seen_at,
                last_seen_at=excluded.last_seen_at,
                last_sample_at=excluded.last_sample_at,
                last_observed_at=excluded.last_observed_at,
                consecutive_samples=excluded.consecutive_samples,
                vanished=excluded.vanished,
                epoch_changed=excluded.epoch_changed,
                updated_at=excluded.updated_at;
            """,
            values,
        )
        saved = conn.execute(
            "SELECT * FROM completion_observations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        result = _decode_completion_observation(saved)
        result.update({
            "ready": ready,
            "stale_sample": False,
            "uncertain": vanished,
            "elapsed_seconds": elapsed,
            "task_status": current_status,
        })
        conn.execute("COMMIT;")
        return result
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def _coerce_version(value: Any) -> Optional[int]:
    """Best-effort int coercion for version fields carried in payloads."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _blocker_sample_consumed(
    status_history: Any, sample_timestamp: float,
) -> bool:
    """Whether a working -> blocked transition happened after the sample.

    The Controller consumes a ``blocked_marker_observed`` sample by winning
    its version CAS; the transition appends a ``status_history`` entry whose
    ``to`` is ``blocked``.  A sample with no such later entry was never
    consumed — invalidating it (a metadata write bumping the version) must
    not silently drop a genuine blocker.
    """
    if isinstance(status_history, str):
        try:
            status_history = json.loads(status_history)
        except (TypeError, ValueError):
            return False
    if not isinstance(status_history, list):
        return False
    for entry in status_history:
        if not isinstance(entry, dict) or entry.get("to") != "blocked":
            continue
        try:
            if float(entry.get("timestamp") or 0) > float(sample_timestamp):
                return True
        except (TypeError, ValueError):
            continue
    return False

def observe_blocker_marker(
    task_id: str,
    *,
    marker_present: bool,
    event_payload: Optional[Dict[str, Any]] = None,
    observed_at: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Track durable BLOCKER marker presence; classify and record (C03c).

    The Controller can only version-check a ``blocked_marker_observed``
    sample, so the Sentinel must never re-sample screen residue: after a
    ``set-status`` reopen the stale marker would be re-stamped with the new
    version and re-block the task.  This records the marker presence of the
    latest patrol, classifies the sighting via
    ``completion.blocker_sample_action`` and — for a ``record`` verdict —
    appends the ``blocked_marker_observed`` event **in the same
    transaction**, so a presence commit can never outlive its sample write
    (PR #161 review, scenario B).  Only ``blocker_present`` and
    ``updated_at`` are written to the completion row, so the completion
    row's own epoch state is never disturbed.

    ``event_payload`` is the shell-assembled sample body (observed_version,
    observed_status, screen_sha256, ...); it is required whenever
    ``marker_present`` is true.
    """
    from herdr import completion as completion_policy

    now = float(observed_at if observed_at is not None else time.time())
    if not math.isfinite(now):
        raise ValueError("observed_at must be finite")
    if marker_present and not isinstance(event_payload, dict):
        raise ValueError("event_payload is required for a present blocker marker")
    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task_row is None:
            raise ValueError(f"Task '{task_id}' not found")
        row = conn.execute(
            "SELECT blocker_present FROM completion_observations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        last_present = (
            None
            if row is None or row["blocker_present"] is None
            else bool(row["blocker_present"])
        )
        prior_version = None
        prior_consumed = False
        prior_hash = None
        prior_timestamp = None
        if marker_present:
            prior = conn.execute(
                "SELECT payload_json, timestamp FROM events "
                "WHERE task_id = ? AND event_type = 'blocked_marker_observed' "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if prior is not None:
                prior_timestamp = float(prior["timestamp"])
                prior_payload = json.loads(prior["payload_json"] or "{}")
                prior_version = _coerce_version(
                    prior_payload.get("observed_version")
                )
                prior_hash = prior_payload.get("screen_sha256")
                prior_consumed = _blocker_sample_consumed(
                    json.loads(task_row["payload_json"] or "{}").get(
                        "status_history"
                    ),
                    prior_timestamp,
                )
        action = completion_policy.blocker_sample_action(
            marker_present=marker_present,
            last_blocker_present=last_present,
            current_version=_coerce_version(
                (event_payload or {}).get("observed_version")
            ),
            prior_event_version=prior_version,
            prior_event_consumed=prior_consumed,
            prior_event_screen_sha256=prior_hash,
            screen_sha256=(event_payload or {}).get("screen_sha256"),
        )
        if action == "record":
            _facade().record_event(
                {
                    "event_type": "blocked_marker_observed",
                    "payload": dict(event_payload or {}),
                    "workflow_id": task_row["workflow_id"],
                    "node_id": task_row["node"] or task_row["stage"],
                    "task_id": task_id,
                    "agent_id": task_row["agent"],
                    "source": "herdr-sentinel",
                    "timestamp": now,
                },
                conn=conn,
            )
        if last_present is None or last_present != marker_present:
            conn.execute(
                """INSERT INTO completion_observations
                    (task_id, blocker_present, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        blocker_present=excluded.blocker_present,
                        updated_at=excluded.updated_at;
                """,
                (task_id, int(marker_present), now),
            )
        conn.execute("COMMIT;")
        return {"task_id": task_id, "action": action}
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()

def get_completion_observation(
    task_id: str, db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = _facade().get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM completion_observations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return _decode_completion_observation(row) if row is not None else None
    finally:
        conn.close()

def clear_completion_observation(
    task_id: str,
    db_path: Optional[Path] = None,
    *,
    expected_status: str | None = None,
    expected_version: int | None = None,
) -> bool:
    """Clear an observation only when its observed task epoch still matches."""
    conn = _facade().get_db_connection(db_path)
    try:
        query = "DELETE FROM completion_observations WHERE task_id = ?"
        params: list[Any] = [task_id]
        if expected_status is not None:
            query += " AND observed_status = ?"
            params.append(str(expected_status))
        if expected_version is not None:
            query += " AND observed_version = ?"
            params.append(int(expected_version))
        conn.execute(query, params)
        return conn.execute("SELECT changes()").fetchone()[0] == 1
    finally:
        conn.close()

def clear_completion_observations_for_statuses(
    statuses: "Iterable[str]",
    db_path: Optional[Path] = None,
) -> int:
    """Delete completion samples of every task currently in one of ``statuses``.

    One statement, one connection: a task row can never consume its sample
    again once terminal, and the task table has no retention policy, so a
    per-task sweep would open one SQLite connection (4 PRAGMAs + schema lock)
    for every task ever created.  Measured on a 300-task database that costs
    770 ms per 3-second sweep and grows without bound; this form costs ~2 ms
    and is flat in the number of tasks.

    ``statuses`` comes from the caller (``herdr.transitions`` owns the set) so
    this layer never redefines the terminal-status authority.
    """
    wanted = sorted({str(status) for status in statuses if str(status)})
    if not wanted:
        return 0
    placeholders = ", ".join("?" for _ in wanted)
    conn = _facade().get_db_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM completion_observations WHERE task_id IN "
            "(SELECT task_id FROM tasks WHERE status IN (" + placeholders + "))",
            tuple(wanted),
        )
        return int(cursor.rowcount or 0)
    finally:
        conn.close()
