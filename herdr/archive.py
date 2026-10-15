#!/usr/bin/env python3
"""Task Archive Query (herdr/archive.py).

Functional core for the console archive/query list: presentation-only
filtering, sorting and pagination over task records supplied by the caller.
Persistence and I/O stay in the imperative shell (console / StateStore).
"""

import math
from typing import Any, Dict, List, Optional

ARCHIVED_STATUSES = ("cleaned", "superseded", "failed")

ACTIVE_STATUSES = (
    "pending",
    "dispatched",
    "working",
    "blocked",
    "agent_done",
    "rework",
    "paused",
    "interrupted",
)

STATUS_GROUPS: Dict[str, Optional[tuple]] = {
    "archived": ARCHIVED_STATUSES,
    "active": ACTIVE_STATUSES,
    "all": None,
}

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_SEARCH_FIELDS = (
    "task_id",
    "goal",
    "workflow_id",
    "project_name",
    "node",
    "node_label",
    "stage",
    "stage_label",
)


def _norm(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def _recent(task: Dict[str, Any]) -> float:
    raw = (
        task.get("updated_at")
        or task.get("last_activity_at")
        or task.get("created_at")
        or 0
    )
    try:
        seconds = float(raw)
        # Corrupt nonfinite values have no chronological meaning. Python NaN
        # keys make sort order depend on inventory order; use one finite fallback
        # in both the in-memory reference and SQLite pagination path.
        return seconds if math.isfinite(seconds) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_seconds(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _matches(
    task: Dict[str, Any],
    project_id: str,
    workflow_id: str,
    agent: str,
    statuses: Optional[tuple],
    keyword: str,
) -> bool:
    if project_id and task.get("project_id") != project_id:
        return False
    if workflow_id and workflow_id not in _norm(task.get("workflow_id")):
        return False
    if agent and task.get("agent") != agent:
        return False
    if statuses is not None and task.get("status") not in statuses:
        return False
    if keyword:
        haystack = " ".join(_norm(task.get(field)) for field in _SEARCH_FIELDS)
        if keyword not in haystack:
            return False
    return True


def summarize_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Compact projection of a task record for archive list rendering."""
    created = task.get("created_at")
    updated = task.get("updated_at") or task.get("last_activity_at") or created or 0
    created_s = _to_seconds(created)
    updated_s = _to_seconds(updated)
    duration = None
    if created_s is not None and updated_s is not None:
        duration = max(0.0, updated_s - created_s)
    node = task.get("node") or task.get("stage")
    return {
        "task_id": task.get("task_id"),
        "workflow_id": task.get("workflow_id"),
        "project_id": task.get("project_id"),
        "project_name": task.get("project_name"),
        "node": node,
        "node_label": task.get("node_label") or task.get("stage_label") or node,
        "agent": task.get("agent"),
        "status": task.get("status"),
        "stage_verdict": task.get("stage_verdict") or "",
        "goal": task.get("goal") or "",
        "last_result": task.get("last_result") or "",
        "superseded_by": task.get("superseded_by") or "",
        "created_at": created,
        "updated_at": updated,
        "duration_seconds": duration,
    }


def query_archived_tasks(
    tasks: List[Dict[str, Any]],
    project_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    agent: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> Dict[str, Any]:
    """Filter, sort (most recently updated first) and paginate task records.

    status accepts a group alias ("archived" / "active" / "all") or an exact
    status name; empty/None defaults to the "archived" group.
    """
    status_key = _norm(status) or "archived"
    if status_key in STATUS_GROUPS:
        statuses = STATUS_GROUPS[status_key]
    else:
        statuses = (status_key,)

    project_key = str(project_id).strip() if project_id else ""
    workflow_key = _norm(workflow_id)
    agent_key = str(agent).strip() if agent else ""
    keyword = _norm(q)

    matched = [
        task
        for task in tasks or []
        if _matches(task, project_key, workflow_key, agent_key, statuses, keyword)
    ]
    matched.sort(key=lambda t: (-_recent(t), str(t.get("task_id") or "")))

    try:
        offset_i = max(0, int(offset))
    except (TypeError, ValueError):
        offset_i = 0
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = DEFAULT_LIMIT
    limit_i = max(1, min(MAX_LIMIT, limit_i))

    page = matched[offset_i : offset_i + limit_i]
    return {
        "total": len(matched),
        "count": len(page),
        "limit": limit_i,
        "offset": offset_i,
        "status": status_key,
        "items": [summarize_task(task) for task in page],
    }


def query_archived_tasks_sqlite(
    db_path=None,
    project_id=None,
    workflow_id=None,
    agent=None,
    status=None,
    q=None,
    limit=DEFAULT_LIMIT,
    offset=0,
):
    """Query one read-only SQLite snapshot; decode full records only for the page.

    SQLite's lower()/LIKE implement ASCII folding and wildcard matching, which
    differ from the established Python substring contract. Small scalar UDFs
    retain Unicode normalization and Python timestamp fallback without loading
    every task payload into Python. Values and filters are always parameters.
    """
    import json
    from . import state_db

    # Reuse the public core's input normalization and pagination contract.
    result = query_archived_tasks([], status=status, limit=limit, offset=offset)
    status_key = result['status']
    statuses = STATUS_GROUPS.get(status_key, (status_key,))
    project_key = str(project_id).strip() if project_id else ''
    workflow_key = _norm(workflow_id)
    agent_key = str(agent).strip() if agent else ''
    keyword = _norm(q)
    # These columns override payload values in state_db._decode_task_row.
    columns = {'task_id', 'workflow_id', 'node', 'stage', 'agent', 'status',
               'goal', 'created_at', 'updated_at'}

    def value(field):
        return field if field in columns else f"json_extract(payload_json, '$.{field}')"

    def normalized(field):
        expr = value(field)
        kind = "NULL" if field in columns else f"json_type(payload_json, '$.{field}')"
        return f'archive_norm({expr}, {kind})'

    def scalar_norm(raw, kind):
        if kind in ('array', 'object'):
            raw = json.loads(raw)
        elif kind == 'true':
            raw = True
        elif kind == 'false':
            raw = False
        return _norm(raw)

    def recent(updated, activity, created):
        return _recent({'updated_at': updated, 'last_activity_at': activity, 'created_at': created})

    clauses, params = [], []
    if project_key:
        clauses.append(f'{value("project_id")} = ?')
        params.append(project_key)
    if workflow_key:
        clauses.append(f'instr({normalized("workflow_id")}, ?) > 0')
        params.append(workflow_key)
    if agent_key:
        clauses.append('agent = ?')
        params.append(agent_key)
    if statuses is not None:
        clauses.append('status IN (' + ','.join('?' for _ in statuses) + ')')
        params.extend(statuses)
    if keyword:
        haystack = " || ' ' || ".join(normalized(field) for field in _SEARCH_FIELDS)
        clauses.append(f'instr(({haystack}), ?) > 0')
        params.append(keyword)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    conn = state_db.get_readonly_db_connection(db_path)
    try:
        conn.create_function('archive_norm', 2, scalar_norm, deterministic=True)
        conn.create_function('archive_recent', 3, recent, deterministic=True)
        conn.execute('BEGIN')
        result['total'] = conn.execute('SELECT count(*) FROM tasks' + where, params).fetchone()[0]
        ordering = f' ORDER BY archive_recent(updated_at, {value("last_activity_at")}, created_at) DESC, task_id ASC'
        rows = conn.execute('SELECT * FROM tasks' + where + ordering + ' LIMIT ? OFFSET ?',
                            [*params, result['limit'], result['offset']]).fetchall()
        result['items'] = [summarize_task(state_db._decode_task_row(row)) for row in rows]
        result['count'] = len(result['items'])
        return result
    finally:
        conn.close()
