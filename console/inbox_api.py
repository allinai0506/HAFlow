"""Console HTTP API handlers for User Task Inbox."""

import threading
from typing import Any, Dict, Optional

from herdr.inbox.detector import (
    detect_project_and_workspace,
    validate_project_and_workspace,
)
from herdr.inbox.dispatcher import InboxDispatcher
from herdr.inbox.store import InboxStore
from herdr.inbox.transitions import InvalidInboxTransitionError

_store_instance: Optional[InboxStore] = None
_dispatcher_instance: Optional[InboxDispatcher] = None


def get_inbox_store() -> InboxStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = InboxStore()
    return _store_instance


def get_inbox_dispatcher() -> InboxDispatcher:
    global _dispatcher_instance
    if _dispatcher_instance is None:
        _dispatcher_instance = InboxDispatcher(store=get_inbox_store())
    return _dispatcher_instance


def _trigger_background_sweep():
    def _run():
        try:
            get_inbox_dispatcher().dispatch_sweep()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def api_inbox_tasks(q: Dict[str, Any]) -> Dict[str, Any]:
    """GET /api/inbox/tasks"""
    status = q.get("status", [None])[0] if isinstance(q.get("status"), list) else q.get("status")
    project_id = q.get("project_id", [None])[0] if isinstance(q.get("project_id"), list) else q.get("project_id")
    workspace_id = q.get("workspace_id", [None])[0] if isinstance(q.get("workspace_id"), list) else q.get("workspace_id")
    priority = q.get("priority", [None])[0] if isinstance(q.get("priority"), list) else q.get("priority")
    limit_str = q.get("limit", [100])[0] if isinstance(q.get("limit"), list) else q.get("limit", 100)
    offset_str = q.get("offset", [0])[0] if isinstance(q.get("offset"), list) else q.get("offset", 0)

    try:
        limit = int(limit_str)
    except (ValueError, TypeError):
        limit = 100
    try:
        offset = int(offset_str)
    except (ValueError, TypeError):
        offset = 0

    tasks, total = get_inbox_store().list_tasks(
        status=status or None,
        project_id=project_id or None,
        workspace_id=workspace_id or None,
        priority=priority or None,
        limit=limit,
        offset=offset,
    )
    return {"ok": True, "tasks": tasks, "total": total}


def api_inbox_task(task_id: str) -> Dict[str, Any]:
    """GET /api/inbox/task?id=..."""
    task = get_inbox_store().get_task(task_id)
    if not task:
        return {"ok": False, "error": "not_found", "status_code": 404}
    return {"ok": True, "task": task}


def api_inbox_create_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/tasks"""
    b = b or {}
    title = str(b.get("title") or "").strip()
    instruction = str(b.get("instruction") or "").strip()
    project_id = str(b.get("project_id") or "").strip() or None
    workspace_id = str(b.get("workspace_id") or "").strip() or None
    priority = str(b.get("priority") or "NORMAL").strip().upper()
    auto_confirm = bool(b.get("auto_confirm", False))
    source_type = str(b.get("source_type") or "manual").strip()
    source_id = str(b.get("source_id") or "").strip() or None
    idempotency_key = str(b.get("idempotency_key") or "").strip() or None

    if auto_confirm:
        if not project_id or not workspace_id:
            raise ValueError("project_id and workspace_id must be provided when auto_confirm is enabled")
        ok, err, _ = validate_project_and_workspace(project_id, workspace_id)
        if not ok:
            raise ValueError(f"Project and workspace validation failed: {err}")

    task = get_inbox_store().create_task(
        title=title,
        instruction=instruction,
        project_id=project_id,
        workspace_id=workspace_id,
        priority=priority,
        auto_confirm=auto_confirm,
        source_type=source_type,
        source_id=source_id,
        idempotency_key=idempotency_key,
    )

    if task.get("status") == "PENDING":
        _trigger_background_sweep()

    return {"ok": True, "task": task, "task_id": task["id"], "status": task["status"]}


def api_inbox_confirm_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/confirm"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    project_id = str(b.get("project_id") or "").strip()
    workspace_id = str(b.get("workspace_id") or "").strip()

    ok, err, _ = validate_project_and_workspace(project_id, workspace_id)
    if not ok:
        raise ValueError(f"Invalid scope: {err}")

    task = get_inbox_store().confirm_task_scope(
        task_id=task_id,
        project_id=project_id,
        workspace_id=workspace_id,
    )
    _trigger_background_sweep()
    return {"ok": True, "task": task}


def api_inbox_set_priority(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/priority"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    priority = str(b.get("priority") or "").strip().upper()
    task = get_inbox_store().set_task_priority(task_id, priority)
    _trigger_background_sweep()
    return {"ok": True, "task": task}


def api_inbox_reorder_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/reorder"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    direction = str(b.get("direction") or "").strip().lower()
    task = get_inbox_store().reorder_pending_task(task_id, direction)
    return {"ok": True, "task": task}


def api_inbox_cancel_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/cancel"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    reason = str(b.get("reason") or "Cancelled by user").strip()
    task = get_inbox_store().cancel_task(task_id, reason=reason)
    return {"ok": True, "task": task}


def api_inbox_retry_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/retry"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    task = get_inbox_store().retry_task(task_id)
    _trigger_background_sweep()
    return {"ok": True, "task": task}


def api_inbox_detect(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/detect"""
    b = b or {}
    instruction = str(b.get("instruction") or "").strip()
    title = str(b.get("title") or "").strip()
    res = detect_project_and_workspace(instruction=instruction, title=title)
    return {"ok": True, **res}


def api_inbox_stats(q: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET /api/inbox/stats"""
    user_id = q.get("user_id", [None])[0] if q and isinstance(q.get("user_id"), list) else (q.get("user_id") if q else None)
    stats = get_inbox_store().get_inbox_stats(user_id=user_id)
    return {"ok": True, **stats}


def api_inbox_sweep() -> Dict[str, Any]:
    """POST /api/inbox/sweep"""
    res = get_inbox_dispatcher().dispatch_sweep()
    return {"ok": True, "sweep": res}
