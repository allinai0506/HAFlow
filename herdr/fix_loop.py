#!/opt/homebrew/bin/python3
"""Fix-loop dead-stall recovery decisions (pure core, no I/O).

The controller owns threads, queues, panes and persistence; this module
only answers three questions with plain data in and plain data out:

- budget/fingerprint: may this blocked gate invalidate + retry again,
  or must it escalate to a human?
- latch: may the sweep advance past a node the fix-loop just invalidated,
  or must it wait for a genuine redo?
- redelivery: is a persisted fix-loop notification due, and has the
  coordinator already handled it by dispatching follow-up work?
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

COMPLETED_LIKE = frozenset(
    {"completed", "committed", "integrated", "cleanup_ready", "cleaned"}
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def fix_loop_exhausted(loop_count: Any, max_loops: Any) -> bool:
    """True once the retry budget is spent; invalidating further is churn."""
    count = _safe_int(loop_count, 0)
    limit = _safe_int(max_loops, 0)
    if limit <= 0:
        return False
    return count >= limit


def verdict_fingerprint(
    suggested_branch: Any, blockers: Optional[List[Dict[str, Any]]]
) -> str:
    """Stable identity of one blocked verdict (order-independent)."""
    parts = [str(suggested_branch or "")]
    for blocker in sorted(
        (b or {} for b in (blockers or [])),
        key=lambda b: str(b.get("task_id") or ""),
    ):
        parts.append(str(blocker.get("task_id") or ""))
        parts.append(str(blocker.get("note") or ""))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"vfp-{digest[:16]}"


def is_repeat_verdict(fingerprint: Any, stored: Any) -> bool:
    """Same verdict as last loop: retrying cannot produce new information."""
    if not fingerprint or not stored:
        return False
    return str(fingerprint) == str(stored)


def _node_tasks(tasks: Optional[List[Dict[str, Any]]], node_id: str):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if node_id in (task.get("node"), task.get("stage")):
            yield task


def latch_blocks_advance(
    tasks: Optional[List[Dict[str, Any]]],
    node_id: str,
    latch_ts: Any,
) -> bool:
    """True while an invalidated node awaits a genuine redo.

    Only a non-superseded task that reached a completed-like status AFTER
    the invalidation counts as redo; stale completions and superseded rows
    never clear the latch.
    """
    try:
        since = float(latch_ts or 0)
    except (TypeError, ValueError):
        return False
    if since <= 0:
        return False
    for task in _node_tasks(tasks, node_id):
        if task.get("status") == "superseded" or task.get("superseded_by"):
            continue
        if task.get("status") not in COMPLETED_LIKE:
            continue
        try:
            updated = float(task.get("updated_at") or 0)
        except (TypeError, ValueError):
            continue
        if updated > since:
            return False
    return True


def redelivery_due(episode: Optional[Dict[str, Any]], now: float) -> bool:
    """A persisted fix-loop note is due once its backoff has passed."""
    if not isinstance(episode, dict):
        return False
    if "next_retry_at" not in episode:
        return False
    try:
        return float(episode.get("next_retry_at") or 0) <= float(now)
    except (TypeError, ValueError):
        return False


def redelivery_handled(
    tasks: Optional[List[Dict[str, Any]]],
    retry_node: str,
    first_seen_at: Any,
) -> bool:
    """True when follow-up work for the retry node appeared after the note."""
    try:
        seen = float(first_seen_at or 0)
    except (TypeError, ValueError):
        return False
    if seen <= 0:
        return False
    for task in _node_tasks(tasks, retry_node):
        if task.get("status") == "superseded" or task.get("superseded_by"):
            continue
        try:
            updated = float(task.get("updated_at") or task.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if updated > seen:
            return True
    return False


def summarize_fix_loop_item(item: Dict[str, Any], budget: int = 2000) -> Dict[str, Any]:
    """Bounded, JSON-safe snapshot of a fix-loop item for attention detail."""
    blockers = []
    for blocker in (item or {}).get("blockers") or []:
        if not isinstance(blocker, dict):
            continue
        note = str(blocker.get("note") or "")
        if len(note) > 300:
            note = note[:300] + "…"
        blockers.append(
            {"task_id": str(blocker.get("task_id") or ""), "note": note}
        )
    summary = {
        "workflow_id": str((item or {}).get("workflow_id") or ""),
        "gate_stage": str((item or {}).get("gate_stage") or ""),
        "retry_node": str((item or {}).get("retry_node") or ""),
        "loop_count": _safe_int((item or {}).get("loop_count")),
        "max_loops": _safe_int((item or {}).get("max_loops")),
        "suggested_branch": str((item or {}).get("suggested_branch") or ""),
        "exhausted": bool((item or {}).get("exhausted")),
        "blockers": blockers,
    }
    try:
        text = json.dumps(summary, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"workflow_id": summary["workflow_id"], "blockers": []}
    for _ in range(8):
        if len(text) <= budget:
            break
        if summary["blockers"]:
            longest = max(
                range(len(summary["blockers"])),
                key=lambda i: len(summary["blockers"][i]["note"]),
            )
            note = summary["blockers"][longest]["note"]
            if len(note) > 50:
                summary["blockers"][longest]["note"] = note[: len(note) // 2] + "…"
            else:
                summary["blockers"].pop(longest)
        else:
            break
        text = json.dumps(summary, ensure_ascii=False)
    return summary
