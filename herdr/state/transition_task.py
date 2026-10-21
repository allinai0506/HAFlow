"""transition task persistence; extracted without changing behavior."""

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional
from herdr.transitions import ACTIVE_TASK_STATUSES, COMPLETED_TASK_STATUSES, TERMINAL_TASK_STATUSES, validate_task_transition
from ._facade import _facade
from .constants import PROTECTED_TASK_METADATA_FIELDS, RESERVED_EVENT_METADATA_FIELDS


def transition_task(
    task_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
    expected_status: Optional[str] = None,
    expected_version: Optional[int] = None,
    expected_updated_at: Optional[float] = None,
    intent: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically validate and transition a task status, appending a canonical WorkflowEvent."""
    should_close = False
    if conn is None:
        conn = _facade().get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Task '{task_id}' not found")

        old_status = row["status"]
        current_version = int(row["version"] or 0)
        mismatch = None
        if expected_status is not None and old_status != expected_status:
            mismatch = {
                "reason": "cas_mismatch",
                "field": "status",
                "expected": expected_status,
                "actual": old_status,
            }
        elif expected_version is not None and current_version != int(expected_version):
            mismatch = {
                "reason": "cas_mismatch",
                "field": "version",
                "expected": int(expected_version),
                "actual": current_version,
            }
        elif expected_updated_at is not None:
            try:
                actual_updated = float(row["updated_at"] or 0)
                if actual_updated != float(expected_updated_at):
                    mismatch = {
                        "reason": "cas_mismatch",
                        "field": "updated_at",
                        "expected": float(expected_updated_at),
                        "actual": actual_updated,
                    }
            except (TypeError, ValueError):
                mismatch = {
                    "reason": "cas_mismatch",
                    "field": "updated_at",
                    "expected": expected_updated_at,
                    "actual": row["updated_at"],
                }
        if mismatch is not None:
            current = _facade()._decode_task_row(row)
            if should_close:
                conn.execute("ROLLBACK;")
            return {
                "ok": False,
                "accepted": False,
                "task_id": task_id,
                "reason": mismatch["reason"],
                "mismatch": mismatch,
                "current": current,
                "current_status": old_status,
                "current_version": current_version,
            }

        effective_intent = intent
        if effective_intent is None and source == "delivery-repair":
            effective_intent = "delivery-repair"

        if (old_status == "completed" and to_status == "rework" and not force) or effective_intent == "delivery-repair":
            from herdr.delivery_rework import validate_repair_transition
            validate_repair_transition(_facade()._decode_task_row(row), dict(metadata or {}), source, conn)
        else:
            validate_task_transition(old_status, to_status, force=force, intent=effective_intent)

        meta = dict(metadata or {})
        if effective_intent:
            meta["intent"] = effective_intent

        if to_status == "superseded" and old_status != "superseded":
            # New invalidation never silently deletes an output obligation.
            # An explicit abandon is the only opt-out; old rows stay compatible.
            meta.setdefault("replacement_pending", True)
        forbidden = set(meta.keys()) & PROTECTED_TASK_METADATA_FIELDS
        if forbidden:
            raise ValueError(f"Cannot overwrite protected task fields via metadata: {sorted(forbidden)}")
        forbidden_event = set(meta.keys()) & RESERVED_EVENT_METADATA_FIELDS
        if forbidden_event:
            raise ValueError(f"Cannot overwrite reserved event fields via metadata: {sorted(forbidden_event)}")

        now = time.time()
        payload = json.loads(row["payload_json"] or "{}")
        task_dict = dict(payload)
        task_dict.update({
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "node": row["node"],
            "stage": row["stage"],
            "agent": row["agent"],
            "status": old_status,
            "stage_verdict": row["stage_verdict"],
            "stage_verdict_note": row["stage_verdict_note"],
            "pane_id": row["pane_id"],
            "goal": row["goal"],
            "blocker": row["blocker"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": current_version,
        })

        if force:
            task_dict["forced"] = True
        for k, v in meta.items():
            task_dict[k] = v

        # A new execution attempt must never consume the prior attempt's receipt.
        if to_status in {"rework", "pending"} and old_status != to_status:
            task_dict.pop("completion_epoch", None)
            task_dict.pop("completion_token_hash", None)

        task_dict["status"] = to_status
        task_dict["updated_at"] = now
        task_dict["last_activity_at"] = now

        if to_status in ACTIVE_TASK_STATUSES and not task_dict.get("started_at"):
            task_dict["started_at"] = now

        if to_status in COMPLETED_TASK_STATUSES and not task_dict.get("last_result"):
            task_dict["last_result"] = to_status

        # Append to status_history in payload (meta unpacked first, canonical fields last)
        status_history = list(task_dict.get("status_history") or [])
        status_history_entry = {
            **meta,
            "from": old_status,
            "to": to_status,
            "reason": reason,
            "source": source,
            "timestamp": now,
        }
        if force:
            status_history_entry["forced"] = True
        status_history.append(status_history_entry)
        task_dict["status_history"] = status_history

        _facade().save_task(task_dict, db_path=None, conn=conn)
        canonical_row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        canonical_task = _facade()._decode_task_row(canonical_row)

        event_payload = {
            **meta,
            "from_status": old_status,
            "to_status": to_status,
            "reason": reason,
            "forced": force,
        }
        event = _facade().record_event(
            {
                "workflow_id": task_dict.get("workflow_id"),
                "node_id": task_dict.get("node") or task_dict.get("stage"),
                "task_id": task_id,
                "agent_id": task_dict.get("agent"),
                "event_type": "task_transition",
                "payload": event_payload,
                "source": source,
                "run_id": task_dict.get("run_id"),
                "timestamp": now,
            },
            conn=conn,
        )

        if should_close:
            conn.execute("COMMIT;")

        # Outcome choke point: a terminal transition settles here when the
        # eval already landed (the reverse order is covered by the
        # record_eval_result hook). Best-effort, never breaks transitions.
        if (
            should_close
            and str(to_status) in TERMINAL_TASK_STATUSES
            and _facade()._outcome_autofinalize_enabled()
        ):
            try:
                from herdr.execution_outcome import try_autofinalize_for_task
            except ImportError:  # pragma: no cover - script-style fallback
                from herdr.execution_outcome import try_autofinalize_for_task
            try:
                try_autofinalize_for_task(str(task_id), db_path=db_path)
            except Exception:
                pass

        return {
            "ok": True,
            "accepted": True,
            "task_id": task_id,
            "workflow_id": task_dict.get("workflow_id"),
            "old_status": old_status,
            "new_status": to_status,
            "reason": reason,
            "source": source,
            "event_id": event.get("id"),
            "current_version": int(canonical_task.get("version") or 0),
            "task": canonical_task,
        }
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

def compare_and_set_task_transition(
    task_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    expected_status: Optional[str] = None,
    expected_version: Optional[int] = None,
    expected_updated_at: Optional[float] = None,
    db_path: Optional[Path] = None,
    intent: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically transition only when the observed task epoch still matches.

    The status and monotonic version are checked inside the same
    ``BEGIN IMMEDIATE`` transaction as the task update and canonical event.
    A rejected compare returns an authoritative snapshot; it never raises a
    stale write or appends a transition event.
    """
    return transition_task(
        task_id=task_id,
        to_status=to_status,
        reason=reason,
        source=source,
        metadata=metadata,
        force=force,
        db_path=db_path,
        expected_status=expected_status,
        expected_version=expected_version,
        expected_updated_at=expected_updated_at,
        intent=intent,
    )

def compare_and_set_completion_transition(
    task_id: str,
    *,
    reason: str = "completion_sentinel",
    source: str = "herdr-controller",
    metadata: dict[str, Any] | None = None,
    expected_status: str | None = None,
    expected_version: int | None = None,
    expected_updated_at: float | None = None,
    now: float | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Atomically validate a durable completion observation and transition.

    The observation check, task CAS, canonical transition event, and removal of
    the consumed observation all happen under one SQLite write transaction.
    A Controller therefore cannot commit a ready result after the marker has
    vanished, and two Controllers can consume one observation only once.
    """
    from herdr import completion as completion_policy

    conn = _facade().get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        task_row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task_row is None:
            raise ValueError(f"Task '{task_id}' not found")
        current = _facade()._decode_task_row(task_row)
        current_status = str(current.get("status") or "")
        current_version = int(current.get("version") or 0)
        mismatch = None
        if expected_status is not None and current_status != expected_status:
            mismatch = {
                "reason": "cas_mismatch",
                "field": "status",
                "expected": expected_status,
                "actual": current_status,
            }
        elif expected_version is not None and current_version != int(expected_version):
            mismatch = {
                "reason": "cas_mismatch",
                "field": "version",
                "expected": int(expected_version),
                "actual": current_version,
            }
        elif expected_updated_at is not None:
            try:
                actual_updated = float(task_row["updated_at"] or 0)
                expected_float = float(expected_updated_at)
            except (TypeError, ValueError):
                actual_updated = task_row["updated_at"]
                expected_float = expected_updated_at
            if actual_updated != expected_float:
                mismatch = {
                    "reason": "cas_mismatch",
                    "field": "updated_at",
                    "expected": expected_updated_at,
                    "actual": actual_updated,
                }
        if mismatch is not None:
            conn.execute("ROLLBACK;")
            return {
                "ok": False,
                "accepted": False,
                "task_id": task_id,
                "reason": mismatch["reason"],
                "mismatch": mismatch,
                "current": current,
                "current_status": current_status,
                "current_version": current_version,
            }

        observation_row = conn.execute(
            "SELECT * FROM completion_observations WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        observation = (
            _facade()._decode_completion_observation(observation_row)
            if observation_row is not None
            else None
        )
        effective_now = float(now if now is not None else time.time())
        if not math.isfinite(effective_now):
            conn.execute("ROLLBACK;")
            return {
                "ok": False,
                "accepted": False,
                "task_id": task_id,
                "reason": "completion_clock_invalid",
                "current": current,
                "current_status": current_status,
                "current_version": current_version,
            }
        rejection = None
        if current_status not in {"dispatched", "working"}:
            rejection = "completion_status_inactive"
        elif observation is None:
            rejection = "completion_observation_missing"
        elif observation.get("epoch_changed"):
            rejection = "completion_epoch_unstable"
        elif observation.get("observed_status") != current_status:
            rejection = "observation_status_stale"
        elif int(observation.get("observed_version") or 0) != current_version:
            rejection = "observation_version_stale"
        elif not observation.get("marker_present"):
            rejection = "completion_marker_absent"
        elif observation.get("vanished"):
            rejection = "completion_marker_vanished"
        elif observation.get("first_seen_at") is None:
            rejection = "completion_first_sample_missing"
        elif int(observation.get("consecutive_samples") or 0) < 2:
            rejection = "completion_confirmation_missing"
        elif not completion_policy.sample_interval_satisfied(
            observation.get("first_seen_at"),
            observation.get("last_sample_at"),
        ):
            rejection = "completion_sample_interval_short"
        elif not completion_policy.observation_age_satisfied(
            observation.get("last_observed_at"), effective_now
        ):
            rejection = "completion_observation_stale"
        elif observation.get("agent_status") not in {"idle", "done"}:
            rejection = "agent_not_idle"
        else:
            started = current.get("started_at") or current.get("created_at") or effective_now
            try:
                elapsed = effective_now - float(started)
            except (TypeError, ValueError):
                elapsed = 0.0
            if elapsed < completion_policy.min_completion_seconds():
                rejection = "completion_elapsed_short"

        if rejection is None and observation is not None:
            observed_updated = observation.get("observed_updated_at")
            if observed_updated is not None and task_row["updated_at"] is not None:
                try:
                    if float(observed_updated) != float(task_row["updated_at"]):
                        rejection = "observation_epoch_stale"
                except (TypeError, ValueError):
                    rejection = "observation_epoch_stale"

        if rejection is not None:
            conn.execute("ROLLBACK;")
            return {
                "ok": False,
                "accepted": False,
                "task_id": task_id,
                "reason": rejection,
                "current": current,
                "current_status": current_status,
                "current_version": current_version,
                "observation": observation,
            }

        transition_metadata = dict(metadata or {})
        transition_metadata.setdefault("completion_confirmations", observation.get("consecutive_samples"))
        transition_metadata.setdefault("completion_version", observation.get("observed_version"))
        transition_metadata.setdefault("completion_first_seen_at", observation.get("first_seen_at"))
        transition_metadata.setdefault("completion_last_sample_at", observation.get("last_sample_at"))
        result = transition_task(
            task_id=task_id,
            to_status="agent_done",
            reason=reason,
            source=source,
            metadata=transition_metadata,
            force=False,
            db_path=None,
            conn=conn,
            expected_status=expected_status or current_status,
            expected_version=(
                current_version if expected_version is None else int(expected_version)
            ),
            expected_updated_at=expected_updated_at,
        )
        if not result.get("accepted", True):
            conn.execute("ROLLBACK;")
            return result
        conn.execute(
            "DELETE FROM completion_observations WHERE task_id = ?", (task_id,)
        )
        conn.execute("COMMIT;")
        result["observation"] = observation
        return result
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except (OSError, sqlite3.Error):
            pass
        raise
    finally:
        conn.close()
