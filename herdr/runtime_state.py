#!/usr/bin/env python3
"""Herdr RuntimeState (herdr/runtime_state.py).

Functional Core: pure helpers that separate "where/how a node execution runs"
(RuntimeState) from "how far the task/workflow has progressed" (Task/Workflow
State, owned by transitions.py / state_db.py).

A RuntimeState only ever records evidence produced by a real execution
(Herdr workspace/tab/pane, agent session, cwd). It never creates runtimes,
never recovers them, and never influences task status transitions.

Persistence: embedded as ``task["runtime"]`` (schemaless ``payload_json``),
so no schema migration is needed and legacy tasks without ``runtime``
keep reading and transitioning exactly as before.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

RUNTIME_STATUSES = frozenset({
    "created",
    "running",
    "completed",
    "failed",
    "unavailable",
})

RUNTIME_FIELDS = (
    # agent: agent implementation/type, e.g. claude/opencode/qodercli.
    "agent",
    "agent_session_id",
    # agent_name: concrete Herdr-managed agent instance name
    # (e.g. "nexusarchive-54433229-coordinato"), not the agent type.
    "agent_name",
    "workspace_id",
    "tab_id",
    "pane_id",
    "cwd",
    # status: runtime lifecycle/availability (does the execution environment
    # still exist and is it usable), NOT the task orchestration status.
    # A paused task therefore still reports runtime "running": its pane and
    # agent session are alive, only the orchestration is on hold.
    "status",
    "started_at",
    "updated_at",
)

# Task lifecycle -> runtime lifecycle. Active task work means the runtime is
# in use; settled tasks release it as completed/failed; superseded tasks no
# longer represent a reusable environment.
_TASK_TO_RUNTIME = {
    "pending": "created",
    "dispatched": "running",
    "working": "running",
    "blocked": "running",
    "paused": "running",
    "agent_done": "running",
    "rework": "running",
    "interrupted": "running",
    "completed": "completed",
    "committed": "completed",
    "integrated": "completed",
    "cleanup_ready": "completed",
    "cleaned": "completed",
    "failed": "failed",
    "superseded": "unavailable",
}

# Tolerated camelCase aliases (e.g. from console/TS callers); always stored
# snake_case to match the existing task-record style.
_CAMEL_ALIASES = {
    "agentSessionId": "agent_session_id",
    "agentName": "agent_name",
    "workspaceId": "workspace_id",
    "tabId": "tab_id",
    "paneId": "pane_id",
    "startedAt": "started_at",
    "updatedAt": "updated_at",
}


def runtime_status_for_task_status(task_status: Optional[str]) -> Optional[str]:
    """Map a task lifecycle status onto a runtime status (None if unknown)."""
    if not task_status:
        return None
    return _TASK_TO_RUNTIME.get(str(task_status))


def _now() -> float:
    return time.time()


def normalize_runtime_state(raw: Any) -> Optional[Dict[str, Any]]:
    """Filter a raw dict down to the RuntimeState contract.

    Unknown keys are dropped, unknown statuses fall back to ``created``.
    Returns None when there is nothing worth persisting.
    """
    if not isinstance(raw, dict):
        return None
    merged: Dict[str, Any] = {}
    for key, value in raw.items():
        canon = _CAMEL_ALIASES.get(key, key)
        if canon in RUNTIME_FIELDS and value is not None and value != "":
            merged[canon] = value
    if merged.get("status") not in RUNTIME_STATUSES:
        merged["status"] = "created"
    now = _now()
    merged.setdefault("started_at", now)
    merged.setdefault("updated_at", merged["started_at"])
    identity = (
        "agent", "agent_session_id", "agent_name",
        "workspace_id", "tab_id", "pane_id", "cwd",
    )
    if not any(merged.get(k) for k in identity):
        return None
    return merged


def build_runtime_state(
    *,
    agent: Optional[str] = None,
    agent_session_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
    tab_id: Optional[str] = None,
    pane_id: Optional[str] = None,
    cwd: Optional[str] = None,
    status: str = "running",
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Build a RuntimeState from real execution evidence.

    Returns None when no placement evidence was provided, so callers never
    persist fabricated runtimes.
    """
    ts = now if now is not None else _now()
    return normalize_runtime_state({
        "agent": agent,
        "agent_session_id": agent_session_id,
        "agent_name": agent_name,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "pane_id": pane_id,
        "cwd": cwd,
        "status": status,
        "started_at": ts,
        "updated_at": ts,
    })


def get_task_runtime(task: Any) -> Optional[Dict[str, Any]]:
    """Read the RuntimeState embedded in a task record (None for legacy)."""
    if not isinstance(task, dict):
        return None
    runtime = task.get("runtime")
    if not isinstance(runtime, dict) or not runtime:
        return None
    return runtime


def transition_runtime(
    task: Any,
    to_task_status: str,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Compute the updated RuntimeState for a task status transition.

    Returns None when the task carries no RuntimeState (legacy data passes
    through untouched) or the target status is unknown.
    """
    runtime = get_task_runtime(task)
    if runtime is None:
        return None
    new_status = runtime_status_for_task_status(to_task_status)
    if new_status is None:
        return None
    updated = dict(runtime)
    updated["status"] = new_status
    updated["updated_at"] = now if now is not None else _now()
    return updated
