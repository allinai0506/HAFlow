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

try:
    from .direct_dispatch import lineage_key
except ImportError:  # pragma: no cover - script-style fallback
    try:
        from herdr.direct_dispatch import lineage_key
    except ImportError:  # pragma: no cover - selective mode degrades off
        lineage_key = None

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
    suggested_branch: Any,
    blockers: Optional[List[Dict[str, Any]]],
    affected_task_ids: Optional[List[str]] = None,
) -> str:
    """Stable identity of one blocked verdict (order-independent).

    ``affected_task_ids`` (PR #110) joins the fingerprint when present: the
    same blocker note targeting task B and later targeting task C are two
    different verdicts, not a repeat. ``None``/empty keeps the legacy digest
    byte-identical, so workflows without selective replan are unaffected.
    """
    parts = [str(suggested_branch or "")]
    for blocker in sorted(
        (b or {} for b in (blockers or [])),
        key=lambda b: str(b.get("task_id") or ""),
    ):
        parts.append(str(blocker.get("task_id") or ""))
        parts.append(str(blocker.get("note") or ""))
    ids = sorted({str(t).strip() for t in (affected_task_ids or []) if str(t).strip()})
    if ids:
        parts.append("affected:" + ",".join(ids))
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


def _lineage_root_of(task: Dict[str, Any]) -> str:
    if lineage_key is None:
        return str(task.get("task_id") or "")
    return lineage_key(task.get("task_id"))[0]


def _selective_latch_satisfied(
    tasks: Optional[List[Dict[str, Any]]],
    node_id: str,
    since: float,
    target_lineage_roots: List[str],
) -> bool:
    """Selective 放行条件:每条目标谱系都有 latch 之后完成的权威成员。

    preserved(未点名)任务一律不参与:它们的时间戳更新永远不能解除
    selective latch。多 target 时全部满足才放行(AND,不是 ANY)。
    """
    for root in target_lineage_roots:
        root_done = False
        for task in _node_tasks(tasks, node_id):
            if _lineage_root_of(task) != root:
                continue
            if task.get("status") == "superseded" or task.get("superseded_by"):
                continue
            if task.get("status") not in COMPLETED_LIKE:
                continue
            try:
                updated = float(task.get("updated_at") or 0)
            except (TypeError, ValueError):
                continue
            if updated > since:
                root_done = True
                break
        if not root_done:
            return False
    return True


def latch_blocks_advance(
    tasks: Optional[List[Dict[str, Any]]],
    node_id: str,
    latch_ts: Any,
    target_lineage_roots: Optional[List[str]] = None,
) -> bool:
    """True while an invalidated node awaits a genuine redo.

    Only a non-superseded task that reached a completed-like status AFTER
    the invalidation counts as redo; stale completions and superseded rows
    never clear the latch.

    Selective mode (PR #110, ``target_lineage_roots`` non-empty): only the
    named lineages can satisfy the latch, and *all* of them must. Preserved
    tasks and unrelated updates never release it early.
    """
    try:
        since = float(latch_ts or 0)
    except (TypeError, ValueError):
        return False
    if since <= 0:
        return False
    roots = [str(r).strip() for r in (target_lineage_roots or []) if str(r).strip()]
    if roots and lineage_key is not None:
        return not _selective_latch_satisfied(tasks, node_id, since, roots)
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
    target_lineage_roots: Optional[List[str]] = None,
    rework_requests: Optional[Dict[str, str]] = None,
) -> bool:
    """True when follow-up work for the retry node appeared after the note.

    Selective mode (PR #110, ``target_lineage_roots`` non-empty): a preserved
    task changing proves nothing about the targeted lineages, so handled
    requires this event's delivered rework request or a genuine replacement
    (``-rN`` member created after the note) for every targeted lineage root.
    """
    try:
        seen = float(first_seen_at or 0)
    except (TypeError, ValueError):
        return False
    if seen <= 0:
        return False
    roots = [str(r).strip() for r in (target_lineage_roots or []) if str(r).strip()]
    requests = rework_requests or {}
    delivered = {str(t.get("task_id")) for t in _node_tasks(tasks, retry_node)
                 if requests.get(str(t.get("task_id"))) == t.get("rework_request_id")
                 and requests.get(str(t.get("task_id")))
                 and t.get("rework_delivery") == "delivered"
                 and t.get("status") != "superseded"}
    if requests and not roots and delivered == set(requests):
        return True
    if roots and lineage_key is not None:
        for root in roots:
            replaced = False
            for task in _node_tasks(tasks, retry_node):
                if _lineage_root_of(task) != root:
                    continue
                if str(task.get("task_id")) in delivered:
                    replaced = True
                    break
                if lineage_key(task.get("task_id"))[1] < 2:
                    continue
                try:
                    created = float(
                        task.get("created_at") or task.get("updated_at") or 0
                    )
                except (TypeError, ValueError):
                    continue
                if created > seen:
                    replaced = True
                    break
            if not replaced:
                return False
        return True
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
    requests = (item or {}).get("rework_requests")
    if isinstance(requests, dict) and requests:
        summary["rework_requests"] = dict(requests)
    # PR #110: selective replan 的 target 谱系随通知一起持久化,
    # redelivery 判断据此保持 target-aware;legacy 事件不带这两个键。
    roots = [
        str(r).strip()
        for r in ((item or {}).get("target_lineage_roots") or [])
        if str(r).strip()
    ][:20]
    if roots:
        summary["mode"] = str((item or {}).get("mode") or "selective")
        summary["target_lineage_roots"] = roots
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
