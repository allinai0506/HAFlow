"""Read-only card for the latest prompt a task actually handed to transport."""

from pathlib import Path

from herdr.prompt_ledger import _public
from herdr.state_db import get_readonly_db_connection

_LIMIT = 500
_LABELS = {
    "GENERATED": "已生成，尚未发送",
    "DISPATCH_PENDING": "即将发送",
    "DISPATCHED": "传输命令已接受",
    "DELIVERY_VERIFIED": "终端通道已收下全文",
    "DELIVERY_FAILED": "投递失败",
    "DELIVERY_UNVERIFIED": "已开始发送，结果无法核验",
}


def project_task_prompt(store, task_id, workflow_id=None):
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    workflow_id = str(workflow_id).strip() if workflow_id else None
    rows = _latest_rows(store, task_id, workflow_id)
    card = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "available": True,
        "rounds": len(rows),
        "rounds_capped": len(rows) == _LIMIT,
        "latest": None,
        "previous_prompt_id": None,
        "compare": None,
    }
    if not rows:
        return card
    latest = rows[0]
    card["latest"] = _card_row(latest)
    if len(rows) < 2:
        return card
    previous = rows[1]
    card["previous_prompt_id"] = previous["prompt_id"]
    purged = bool(latest.get("content_purged") or previous.get("content_purged"))
    card["compare"] = {
        "left_id": previous["prompt_id"],
        "right_id": latest["prompt_id"],
        "equal": None if purged else latest.get("prompt_sha256") == previous.get("prompt_sha256"),
        "access": "reveal_required",
    }
    return card


def _latest_rows(store, task_id, workflow_id):
    """Read snapshots without get_state_store or schema migration.

    A task-drawer GET must not take the write lock that init_db uses.
    """
    path = getattr(store, "db_path", None)
    path = Path(path if path is not None else store)
    clauses, params = ["task_id=?"], [task_id]
    if workflow_id:
        clauses.append("workflow_id=?")
        params.append(workflow_id)
    params.append(_LIMIT)
    sql = (
        "SELECT * FROM prompt_snapshots WHERE " + " AND ".join(clauses)
        + " ORDER BY created_at DESC, prompt_id DESC LIMIT ?"
    )
    conn = get_readonly_db_connection(path)
    try:
        raw = [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()
    return [_public(row, reveal=False) for row in raw]


def _card_row(row):
    status = row.get("dispatch_status") or ""
    return {
        "prompt_id": row.get("prompt_id"),
        "dispatch_status": status,
        "status_label": _LABELS.get(status, status or "状态未知"),
        "agent_name": row.get("agent_name") or "UNKNOWN",
        "agent_kind": row.get("agent_kind") or "",
        "prompt_sha256": row.get("prompt_sha256") or "",
        "prompt_excerpt": row.get("prompt_excerpt") or "",
        "created_at": row.get("created_at"),
        "content_purged": bool(row.get("content_purged")),
        "model_receipt": "UNOBSERVABLE",
    }
