"""Read-only inputs for one execution-quality assessment.

Outcome, trace, steer, and intervention rows stay in their existing stores.
This module opens only ``get_readonly_db_connection`` and does not migrate
or create the database.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .execution_trace import (
    MAX_LIMIT,
    STORED_LIMIT,
    _PROJECTABLE_EVENT_TYPES,
    _tests_row_has_verdict,
    _verification_verdicts,
    assemble_trace,
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
    signals = _human_rows(task_id, db_path)
    if not owning:
        events, truncated = _events_for_run(task, run_id, db_path)
        notes = [{"code": "historical_run_trace_limited", "message": "只读取该运行已归属的账本事件。"}]
        if truncated:
            notes.append({"code": "native_truncated", "message": "执行轨迹已截断，仅显示已读取的部分。"})
        return _bundle(outcome, events, [], notes, signals, allow_legacy=False, truncated=truncated)
    stored, notes = read_stored_events(task, db_path)
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
    return _bundle(outcome, events, native, notes, signals, allow_legacy=True, truncated=truncated)


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
    conn = get_readonly_db_connection(db_path)
    try:
        marks = ",".join("?" for _ in _PROJECTABLE_EVENT_TYPES)
        rows = conn.execute(
            f"""
            SELECT id, task_id, agent_id, event_type, timestamp, source, run_id, sequence, payload_json
            FROM events
            WHERE task_id = ? AND run_id = ? AND event_type IN ({marks})
            ORDER BY timestamp ASC, id ASC
            LIMIT ?
            """,
            (task.get("task_id"), run_id, *_PROJECTABLE_EVENT_TYPES, STORED_LIMIT),
        ).fetchall()
    finally:
        conn.close()
    decoded = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
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
    view = dict(task)
    view["run_id"] = run_id
    verdicts = _verification_verdicts(decoded)
    projected = []
    for row in decoded:
        if _tests_row_has_verdict(row, verdicts):
            continue
        event = project_stored_event(row, view)
        if event is not None:
            projected.append(event)
    return merge_trace_events(projected), len(decoded) >= STORED_LIMIT


def _human_rows(task_id: str, db_path) -> Dict[str, List[Dict[str, str]]]:
    conn = get_readonly_db_connection(db_path)
    try:
        steers = []
        for row in conn.execute(
            "SELECT steer_id, operator, payload_json FROM steering_items WHERE task_id = ?",
            (task_id,),
        ):
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
            for row in conn.execute(
                "SELECT intervention_id, run_id, action FROM interventions WHERE task_id = ?",
                (task_id,),
            )
        ]
    finally:
        conn.close()
    return {"steers": steers, "interventions": interventions}
