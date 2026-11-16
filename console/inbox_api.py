"""REST API handlers for User Task Inbox endpoints."""

import concurrent.futures
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from herdr.inbox.detector import (
    CONFIDENCE_THRESHOLD,
    detect_project_and_workspace,
    validate_project_and_workspace,
)
from herdr.inbox.dispatcher import InboxDispatcher
from herdr.inbox.store import InboxStore

logger = logging.getLogger(__name__)

_store_instance: Optional[InboxStore] = None
_dispatcher_instance: Optional[InboxDispatcher] = None

# P2-03: Bounded single-worker sweep executor to prevent unbounded thread explosion
_SWEEP_LOCK = threading.Lock()
_SWEEP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="inbox_api_sweep"
)


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
        if not _SWEEP_LOCK.acquire(blocking=False):
            # A sweep is already actively running; coalesce
            return
        try:
            get_inbox_dispatcher().dispatch_sweep(async_launch=True)
        except Exception as e:
            logger.debug(f"Background sweep error: {e}")
        finally:
            _SWEEP_LOCK.release()

    try:
        _SWEEP_EXECUTOR.submit(_run)
    except Exception:
        pass


def _resolve_current_user(requested_user: Optional[str] = None) -> str:
    """Resolve trusted server-side user identity.

    Ensures server-side identity isolation so tasks are scoped to the actual
    operator rather than arbitrary client-spoofed identities.
    Only admin or root server identities may act on behalf of requested_user.
    """
    server_user = os.environ.get("HAFLOW_USER") or os.environ.get("USER") or "default"
    if server_user in ("root", "admin"):
        if requested_user and str(requested_user).strip():
            return str(requested_user).strip()
    return server_user


def _check_task_ownership(task: Optional[Dict[str, Any]], current_user: str) -> None:
    if not task:
        return
    t_user = task.get("user_id")
    if t_user and t_user != current_user and current_user not in ("root", "admin"):
        raise PermissionError(f"Task '{task.get('id')}' belongs to user '{t_user}'; access denied for '{current_user}'")


def api_inbox_tasks(q: Dict[str, Any]) -> Dict[str, Any]:
    """GET /api/inbox/tasks"""
    status = q.get("status", [None])[0] if isinstance(q.get("status"), list) else q.get("status")
    project_id = q.get("project_id", [None])[0] if isinstance(q.get("project_id"), list) else q.get("project_id")
    workspace_id = q.get("workspace_id", [None])[0] if isinstance(q.get("workspace_id"), list) else q.get("workspace_id")
    priority = q.get("priority", [None])[0] if isinstance(q.get("priority"), list) else q.get("priority")
    user_id_param = q.get("user_id", [None])[0] if isinstance(q.get("user_id"), list) else q.get("user_id")
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

    server_user = _resolve_current_user()
    is_admin = server_user in ("root", "admin")
    if user_id_param == "all":
        effective_user = None if is_admin else server_user
    elif user_id_param and is_admin:
        effective_user = str(user_id_param).strip()
    else:
        effective_user = server_user

    tasks, total = get_inbox_store().list_tasks(
        status=status or None,
        project_id=project_id or None,
        workspace_id=workspace_id or None,
        priority=priority or None,
        user_id=effective_user,
        limit=limit,
        offset=offset,
    )
    return {"ok": True, "tasks": tasks, "total": total}


def api_inbox_task(task_id: str) -> Dict[str, Any]:
    """GET /api/inbox/task?id=..."""
    task = get_inbox_store().get_task(task_id)
    if not task:
        return {"ok": False, "error": "not_found", "status_code": 404}
    _check_task_ownership(task, _resolve_current_user())
    return {"ok": True, "task": task}


def api_inbox_create_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/tasks"""
    b = b or {}
    title = str(b.get("title") or "").strip()
    instruction = str(b.get("instruction") or "").strip()
    project_id = str(b.get("project_id") or "").strip() or None
    workspace_id = str(b.get("workspace_id") or "").strip() or None
    user_id = _resolve_current_user(b.get("user_id"))
    priority = str(b.get("priority") or "NORMAL").strip().upper()
    auto_confirm = bool(b.get("auto_confirm", False))
    raw_source = b.get("confirmation_source") or b.get("confirmed_by")
    confirmation_source = str(raw_source).strip().lower() if raw_source else ""
    source_type = str(b.get("source_type") or "manual").strip().lower()
    source_id = str(b.get("source_id") or "").strip() or None
    idempotency_key = str(b.get("idempotency_key") or "").strip() or None

    # Strict scope and user confirmation enforcement (P1-03)
    if auto_confirm:
        # Automated sources (webhook, timer, detector, system) can NEVER auto-confirm to PENDING
        # Even manual sources must explicitly provide confirmation_source="user" or confirmed_by="user"
        if source_type != "manual" or confirmation_source != "user":
            auto_confirm = False
        else:
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
        user_id=user_id,
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

    existing = get_inbox_store().get_task(task_id)
    if not existing:
        return {"ok": False, "error": "not_found", "status_code": 404}
    _check_task_ownership(existing, _resolve_current_user())

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
    existing = get_inbox_store().get_task(task_id)
    if not existing:
        return {"ok": False, "error": "not_found", "status_code": 404}
    _check_task_ownership(existing, _resolve_current_user())

    task = get_inbox_store().set_task_priority(task_id, priority)
    _trigger_background_sweep()
    return {"ok": True, "task": task}


def api_inbox_reorder_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/reorder"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    direction = str(b.get("direction") or "").strip().lower()
    existing = get_inbox_store().get_task(task_id)
    if not existing:
        return {"ok": False, "error": "not_found", "status_code": 404}
    _check_task_ownership(existing, _resolve_current_user())

    task = get_inbox_store().reorder_pending_task(task_id, direction)
    return {"ok": True, "task": task}


def api_inbox_cancel_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/cancel"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    reason = str(b.get("reason") or "Cancelled by user").strip()
    existing = get_inbox_store().get_task(task_id)
    if not existing:
        return {"ok": False, "error": "not_found", "status_code": 404}
    _check_task_ownership(existing, _resolve_current_user())

    task = get_inbox_store().cancel_task(task_id, reason=reason)
    return {"ok": True, "task": task}


def api_inbox_retry_task(b: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/inbox/task/retry"""
    b = b or {}
    task_id = str(b.get("task_id") or "").strip()
    existing = get_inbox_store().get_task(task_id)
    if not existing:
        return {"ok": False, "error": "not_found", "status_code": 404}
    _check_task_ownership(existing, _resolve_current_user())

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
    user_id_param = q.get("user_id", [None])[0] if q and isinstance(q.get("user_id"), list) else (q.get("user_id") if q else None)
    server_user = _resolve_current_user()
    is_admin = server_user in ("root", "admin")
    if user_id_param == "all":
        effective_user = None if is_admin else server_user
    elif user_id_param and is_admin:
        effective_user = str(user_id_param).strip()
    else:
        effective_user = server_user

    stats = get_inbox_store().get_inbox_stats(user_id=effective_user)
    return {"ok": True, **stats}


def api_inbox_sweep() -> Dict[str, Any]:
    """POST /api/inbox/sweep"""
    res = get_inbox_dispatcher().dispatch_sweep()
    server_user = _resolve_current_user()
    is_admin = server_user in ("root", "admin")
    if is_admin:
        return {"ok": True, "sweep": res}

    # P2-01: Non-admin users cannot inspect other users' task instructions or titles
    def _filter_user_tasks(task_list):
        return [
            t for t in (task_list or [])
            if isinstance(t, dict) and t.get("user_id") == server_user
        ]

    sanitized_busy = []
    for bw in (res.get("busy_workspaces") or []):
        if isinstance(bw, dict):
            sanitized_busy.append({
                "workspace_id": bw.get("workspace_id"),
                "reason": "Workspace is currently busy",
            })

    return {
        "ok": True,
        "sweep": {
            "recovered": _filter_user_tasks(res.get("recovered")),
            "reconciled": _filter_user_tasks(res.get("reconciled")),
            "launched": _filter_user_tasks(res.get("launched")),
            "busy_workspaces": sanitized_busy,
        },
    }
