"""Read-only inputs for one execution-quality assessment.

Outcome, trace, steer, and intervention rows stay in their existing stores.
This module opens only ``get_readonly_db_connection`` and does not migrate
or create the database.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

from .execution_trace import (
    MAX_LIMIT,
    STORED_LIMIT,
    _PROJECTABLE_EVENT_TYPES,
    _corrupt_payload_note,
    _receipts_for_scope,
    _row_evidence_id,
    _tests_row_has_verdict,
    _verification_verdicts,
    assemble_trace,
    decode_stored_payload,
    merge_trace_events,
    project_stored_event,
    read_stored_events,
    read_trace_tasks,
)
from .native_session_trace import load_native_actions
from .state_db import get_readonly_db_connection, read_execution_outcome
from .trajectory import run_id_for_task


_NATIVE_AGENTS = frozenset({"codex", "claude", "opencode"})
_PAGE_CAP = 10
_HUMAN_CAP = 200


def load_quality_inputs(task_id: str, run_id: str, *, db_path, roots, budget_s: float) -> Dict[str, Any]:
    """Return the facts ``assess_quality`` judges. Missing task is an empty trace."""
    outcome = read_execution_outcome(task_id, run_id, db_path)
    found = read_trace_tasks(db_path, task_id)
    if not found:
        return _bundle(
            outcome, [], [],
            [{"code": "trajectory_unavailable", "message": "任务不存在。"}],
            {"steers": [], "interventions": []},
            allow_legacy=False, truncated=False,
        )
    task = found[0]
    persisted = str(task.get("run_id") or "")
    owning = persisted == run_id if persisted else run_id == run_id_for_task(task)
    signals, human_note = _human_rows(task_id, run_id, db_path)
    if not owning:
        events, truncated, corrupt_notes = _events_for_run(task, run_id, db_path)
        notes = [{"code": "historical_run_trace_limited", "message": "只读取该运行已归属的账本事件。"}]
        notes.extend(corrupt_notes)
        if truncated:
            notes.append({"code": "native_truncated", "message": "执行轨迹已截断，仅显示已读取的部分。"})
        if human_note:
            notes.append(human_note)
        return _bundle(outcome, events, [], notes, signals, allow_legacy=False, truncated=truncated)
    stored, notes = read_stored_events(task, db_path)
    if human_note:
        notes = list(notes) + [human_note]
    agent = str(task.get("agent") or (task.get("runtime") or {}).get("agent") or "")
    if agent in _NATIVE_AGENTS:
        native, native_notes = load_native_actions(task, found[1:], roots, budget_s=budget_s)
    else:
        native, native_notes = [], []
    notes = list(notes) + list(native_notes)
    events = []
    cursor = None
    truncated = False
    for _ in range(_PAGE_CAP):
        page = assemble_trace(
            task, stored, native, degradation=notes, limit=MAX_LIMIT, cursor=cursor,
        )
        events.extend(page["events"])
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
        if not cursor:
            truncated = True
            break
    else:
        truncated = True
    return _bundle(outcome, events, native, notes, signals, allow_legacy=not persisted, truncated=truncated)


def _bundle(outcome, events, actions, degradation, signals, *, allow_legacy, truncated):
    return {
        "outcome": outcome,
        "events": events,
        "actions": actions,
        "degradation": degradation,
        "human_signals": signals,
        "allow_legacy_unscoped": allow_legacy,
        "trace_truncated": truncated,
    }


def _events_for_run(task: Dict[str, Any], run_id: str, db_path):
    unresolved = set()
    receipts_saturated = False
    try:
        conn = get_readonly_db_connection(db_path)
    except FileNotFoundError:
        return [], False, [{"code": "trajectory_unavailable", "message": "任务轨迹数据库暂不可用。"}]
    try:
        try:
            conn.execute("PRAGMA query_only=ON;")
            task_id = task.get("task_id")
            marks = ",".join("?" for _ in _PROJECTABLE_EVENT_TYPES)
            rows = conn.execute(
                f"""
                SELECT id, task_id, agent_id, event_type, timestamp, source, run_id, sequence, payload_json
                FROM events
                WHERE task_id = ? AND run_id = ? AND event_type IN ({marks})
                ORDER BY timestamp ASC, id ASC
                LIMIT ?
                """,
                (task_id, run_id, *_PROJECTABLE_EVENT_TYPES, STORED_LIMIT),
            ).fetchall()
            truncated = len(rows) >= STORED_LIMIT
            if truncated:
                seen = {row["id"] for row in rows}
                receipt_rows = _receipts_for_scope(conn, task_id, "run_id = ?", (run_id,))
                supplemented = [row for row in receipt_rows if row["id"] not in seen]
                rows = list(rows) + supplemented
                seen.update(row["id"] for row in supplemented)
                if len(receipt_rows) >= STORED_LIMIT:
                    receipts_saturated = True
                    # Fetch corrupt receipts separately so they cannot consume the
                    # per-evidence budget or make json_extract abort the batch.
                    corrupt_rows = conn.execute(
                        """
                        SELECT id, task_id, agent_id, event_type, timestamp, source, run_id, sequence, payload_json
                        FROM events
                        WHERE task_id = ? AND run_id = ? AND event_type = 'verification_completed'
                          AND payload_json IS NOT NULL AND payload_json != ''
                          AND CASE WHEN json_valid(payload_json)
                                   THEN json_type(payload_json) != 'object' ELSE 1 END
                        ORDER BY timestamp ASC, id ASC LIMIT ?
                        """,
                        (task_id, run_id, STORED_LIMIT),
                    ).fetchall()
                    for corrupt_row in corrupt_rows:
                        if corrupt_row["id"] not in seen:
                            rows.append(corrupt_row)
                            seen.add(corrupt_row["id"])
                    window_ev_ids = set()
                    covered_ev_ids = set()
                    for r in rows:
                        p, c = decode_stored_payload(r["payload_json"])
                        if not c:
                            ev = _row_evidence_id({"payload": p})
                            if ev:
                                if r["event_type"] == "tests_completed":
                                    window_ev_ids.add(ev)
                                elif r["event_type"] == "verification_completed":
                                    covered_ev_ids.update(_verification_verdicts([{
                                        "event_type": r["event_type"], "payload": p,
                                    }]))
                    uncovered = sorted(window_ev_ids - covered_ev_ids)
                    unresolved.update(uncovered)
                    for ev_id in uncovered[:20]:
                        extra_rows = conn.execute(
                            """
                            SELECT id, task_id, agent_id, event_type, timestamp, source, run_id, sequence, payload_json
                            FROM events
                            WHERE task_id = ? AND run_id = ? AND event_type = 'verification_completed'
                              AND CASE WHEN json_valid(payload_json) THEN
                                  (json_extract(payload_json, '$.verification.evidence_id') = ?
                                   OR json_extract(payload_json, '$.evidence_id') = ?) ELSE 0 END
                            ORDER BY timestamp ASC, id ASC
                            LIMIT 10
                            """,
                            (task_id, run_id, ev_id, ev_id),
                        ).fetchall()
                        if len(extra_rows) < 10:
                            unresolved.discard(ev_id)
                        for extra in extra_rows:
                            if extra["id"] not in seen:
                                rows.append(extra)
                                seen.add(extra["id"])
        except sqlite3.Error:
            return [], False, [{"code": "trajectory_unavailable", "message": "任务轨迹数据库暂不可用。"}]
    finally:
        conn.close()
    decoded = []
    corrupt_ids = []
    for row in rows:
        payload, corrupt = decode_stored_payload(row["payload_json"])
        if corrupt:
            corrupt_ids.append(row["id"])
            continue
        decoded.append({
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "source": row["source"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "payload": payload,
        })
    notes = []
    if corrupt_ids:
        notes.append(_corrupt_payload_note(corrupt_ids))
    unresolved_rows = {
        row["id"] for row in decoded
        if receipts_saturated and row["event_type"] == "tests_completed"
        and (_row_evidence_id(row) in unresolved or not _row_evidence_id(row))
    }
    if unresolved_rows:
        notes.append({
            "code": "verification_receipts_unresolved",
            "message": "部分测试证据超出回执核实预算，保持未知。",
            "details": {"unresolved_count": len(unresolved_rows)},
        })
    view = dict(task)
    view["run_id"] = run_id
    verdicts = _verification_verdicts(decoded)
    projected = []
    for row in decoded:
        if row["id"] in unresolved_rows or _tests_row_has_verdict(row, verdicts):
            continue
        event = project_stored_event(row, view)
        if event is not None:
            projected.append(event)
    return merge_trace_events(projected), truncated, notes


def _human_rows(task_id: str, run_id: str, db_path):
    limit = _HUMAN_CAP + 1
    conn = get_readonly_db_connection(db_path)
    try:
        steer_rows = conn.execute(
            "SELECT steer_id, operator, payload_json FROM steering_items "
            "WHERE task_id = ? "
            "AND CASE WHEN json_valid(payload_json) "
            "THEN json_extract(payload_json, '$.run_id') END = ? "
            "ORDER BY created_at ASC, steer_id ASC LIMIT ?",
            (task_id, run_id, limit),
        ).fetchall()
        intervention_rows = conn.execute(
            "SELECT intervention_id, run_id, action FROM interventions "
            "WHERE task_id = ? AND run_id = ? "
            "ORDER BY requested_at ASC, intervention_id ASC LIMIT ?",
            (task_id, run_id, limit),
        ).fetchall()
    finally:
        conn.close()
    truncated = len(steer_rows) > _HUMAN_CAP or len(intervention_rows) > _HUMAN_CAP
    steers = []
    for row in steer_rows[:_HUMAN_CAP]:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        steers.append({
            "ref": str(row["steer_id"] or ""),
            "run_id": str(payload.get("run_id") or ""),
            "operator": row["operator"] if row["operator"] is not None else "",
            "decision_id": str(payload.get("decision_id") or ""),
            "kind": str(payload.get("kind") or ""),
        })
    interventions = [
        {
            "ref": str(row["intervention_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "action": str(row["action"] or ""),
        }
        for row in intervention_rows[:_HUMAN_CAP]
    ]
    note = None
    if truncated:
        note = {
            "code": "human_signals_truncated",
            "message": "本次运行的人工记录超过读取上限。",
        }
    return {"steers": steers, "interventions": interventions}, note
