"""Human dashboard V3 pure builder (herdr/dashboard.py).

Functional Core: no I/O, no subprocess, no wall-clock reads.
Caller (console assembly shell) collects tasks/blockers/deliveries/
stalls/anomalies with timeouts and failure isolation, then calls
build_dashboard() for presentation grouping. All timestamps are
rendered as real local clocks (YYYY-MM-DD HH:MM:SS).
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

ACTIVE_STATUSES = {"pending", "dispatched", "working", "blocked",
                   "agent_done", "rework", "failed"}
HISTORY_STATUSES = {"superseded", "cleaned"}

STATUS_TEXT = {
    "pending": "等待派发",
    "dispatched": "已派发",
    "working": "执行中",
    "blocked": "需你决策",
    "failed": "失败卡住",
    "agent_done": "待验收",
    "rework": "返工中",
    "completed": "已完成",
    "committed": "已完成",
    "integrated": "已完成",
    "cleanup_ready": "待清理",
    "cleaned": "已归档",
    "superseded": "已取代",
}


def format_clock(epoch: Any) -> str:
    """Format epoch seconds as local real clock, or '—' when unknown."""
    try:
        if epoch is None or epoch == "":
            return "—"
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def status_text(status: Any) -> str:
    return STATUS_TEXT.get(str(status or ""), str(status or "unknown"))


def _epoch(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_history(task: Mapping[str, Any]) -> bool:
    if task.get("status") in HISTORY_STATUSES:
        return True
    return bool(task.get("superseded_by"))


def _attention_reason(task: Mapping[str, Any]) -> str:
    if task.get("finalize_escalated"):
        return str(task.get("finalize_escalate_reason") or "终化冲突，需你确认后关闭")
    if task.get("stage_verdict") == "blocked":
        note = str(task.get("stage_verdict_note") or task.get("blocker") or "").strip()
        return note or "门禁阻断，等你放行或打回"
    if task.get("status") == "blocked":
        return str(task.get("blocker") or task.get("sentinel_reason") or "工位阻断，等你决策")
    if task.get("status") == "failed":
        return str(task.get("failure_reason") or task.get("blocker") or "任务失败，需你决策")
    return ""


def build_dashboard(
    tasks: List[Mapping[str, Any]],
    *,
    blockers: Optional[List[Mapping[str, Any]]] = None,
    actions_by_task: Optional[Dict[str, Mapping[str, Any]]] = None,
    deliveries: Optional[List[Mapping[str, Any]]] = None,
    stalls: Optional[Dict[str, Mapping[str, Any]]] = None,
    anomalies: Optional[List[Mapping[str, Any]]] = None,
    runtimes: Optional[Dict[str, Mapping[str, Any]]] = None,
    scope: Optional[str] = None,
    workflows: Optional[List[Mapping[str, Any]]] = None,
    now: Optional[float] = None,
    limits: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    import time as _time

    now_f = float(now) if now is not None else _time.time()
    lim = {"tasks": 50, "attention": 30, "deliveries": 10, "stuck": 30}
    if limits:
        lim.update({k: int(v) for k, v in limits.items() if v is not None})

    task_list = [copy.deepcopy(dict(t)) for t in (tasks or [])]
    blocker_list = [dict(b) for b in (blockers or [])]
    delivery_list = [dict(x) for x in (deliveries or [])]
    anomaly_list = [dict(a) for a in (anomalies or [])]
    actions = dict(actions_by_task or {})
    stall_map = {k: dict(v) for k, v in (stalls or {}).items()}
    runtime_map = dict(runtimes or {})

    # --- tasks section: active first, then most recently updated ---
    def _task_rank(t: Mapping[str, Any]) -> tuple:
        active = 0 if (t.get("status") in ACTIVE_STATUSES and not _is_history(t)) else 1
        upd = _epoch(t.get("last_activity_at")) or _epoch(t.get("updated_at")) or 0.0
        return (active, -upd)

    ordered_tasks = sorted(task_list, key=_task_rank)
    task_items = []
    for t in ordered_tasks[: lim["tasks"]]:
        upd = _epoch(t.get("last_activity_at")) or _epoch(t.get("updated_at"))
        pane_id = t.get("pane_id") or ""
        rt = runtime_map.get(pane_id) or {} if pane_id else {}
        task_items.append({
            "task_id": t.get("task_id") or "—",
            "workflow_id": t.get("workflow_id") or "unknown",
            "node": t.get("node") or t.get("stage") or "—",
            "agent": t.get("agent") or "—",
            "pane_id": pane_id,
            "runtime_status": rt.get("agent_status") if isinstance(rt, Mapping) else None,
            "status": t.get("status") or "unknown",
            "status_text": status_text(t.get("status")),
            "goal": str(t.get("goal") or "")[:200],
            "updated_at": upd,
            "updated_at_text": format_clock(upd),
        })

    # --- attention: union of explicit blockers + escalated + blocked/failed ---
    seen: Dict[str, Dict[str, Any]] = {}
    candidates: List[Mapping[str, Any]] = []
    candidates.extend(blocker_list)
    for t in task_list:
        if _is_history(t):
            continue
        if t.get("finalize_escalated") or t.get("stage_verdict") == "blocked" \
                or t.get("status") in {"blocked", "failed"}:
            candidates.append(t)
    for t in candidates:
        tid = t.get("task_id") or ""
        if not tid or tid in seen or _is_history(t):
            continue
        act = actions.get(tid) or {}
        reason = _attention_reason(t)
        if not reason:
            reason = str(t.get("blocker") or t.get("stage_verdict_note") or "等你看一眼")
        upd = _epoch(t.get("last_activity_at")) or _epoch(t.get("updated_at"))
        seen[tid] = {
            "task_id": tid,
            "workflow_id": t.get("workflow_id") or "unknown",
            "node": t.get("node") or t.get("stage") or "—",
            "pane_id": t.get("pane_id") or "",
            "reason": reason[:300],
            "default_action": str(act.get("title") or _default_action_title(t))[:100],
            "default_action_text": str(act.get("effect") or _default_action_text(t))[:300],
            "command": str(act.get("command_line") or "")[:300],
            "endpoint": str(act.get("api_endpoint") or "")[:200],
            "payload": dict(act.get("api_payload") or {}) if isinstance(act.get("api_payload"), Mapping) else {},
            "updated_at": upd,
            "updated_at_text": format_clock(upd),
        }
    attention = sorted(seen.values(),
                       key=lambda a: -(a.get("updated_at") or 0.0))[: lim["attention"]]

    # --- deliveries: newest first ---
    def _dl_ts(x: Mapping[str, Any]) -> float:
        return _epoch(x.get("ts")) or _epoch(x.get("updated_at")) or 0.0

    ordered_dl = sorted(delivery_list, key=_dl_ts, reverse=True)[: lim["deliveries"]]
    delivery_items = []
    for x in ordered_dl:
        ts = _dl_ts(x)
        delivery_items.append({
            "workflow_id": x.get("workflow_id") or "unknown",
            "title": str(x.get("title") or f"{x.get('delivery_branch', '')}@{str(x.get('candidate_sha', ''))[:12]}")[:200],
            "delivery_branch": x.get("delivery_branch") or "",
            "candidate_sha": x.get("candidate_sha") or "",
            "review_task": x.get("review_task") or "",
            "test_gate": x.get("test_gate") or "",
            "updated_at": ts,
            "updated_at_text": format_clock(ts),
        })

    # --- stuck: stalls + anomalies ---
    stuck: List[Dict[str, Any]] = []
    for wid, s in stall_map.items():
        if not s.get("message"):
            continue
        stuck.append({
            "task_id": s.get("target_task_id") or "",
            "workflow_id": wid,
            "reason": str(s.get("message"))[:300],
            "suggestion": str(s.get("suggested_action") or "")[:100],
            "last_event_text": format_clock(now_f),
        })
    for a in anomaly_list:
        la = _epoch(a.get("last_activity_at"))
        stuck.append({
            "task_id": a.get("task_id") or "",
            "workflow_id": a.get("workflow_id") or "unknown",
            "reason": f"{a.get('kind', 'STUCK')}: {a.get('task_id', '')}",
            "suggestion": "打开任务看详情，必要时换人重跑",
            "last_event_text": format_clock(la),
        })
    stuck = stuck[: lim["stuck"]]

    return {
        "generated_at": now_f,
        "generated_at_text": format_clock(now_f),
        "scope": scope or "all",
        "workflows": [dict(w) for w in (workflows or [])],
        "tasks": task_items,
        "attention": attention,
        "deliveries": delivery_items,
        "stuck": stuck,
        "counts": {
            "tasks": len(task_list),
            "attention": len(seen),
            "deliveries": len(delivery_list),
            "stuck": len(stuck),
        },
    }


def _default_action_title(task: Mapping[str, Any]) -> str:
    if task.get("finalize_escalated"):
        return "确认关闭"
    if task.get("stage_verdict") == "blocked" or task.get("status") == "blocked":
        return "去会签放行/打回"
    if task.get("status") == "failed":
        return "换人重跑"
    return "看一眼"


def _default_action_text(task: Mapping[str, Any]) -> str:
    if task.get("finalize_escalated"):
        return "终化存在冲突，确认无误后关闭工作流；不确定就先看交付物。"
    if task.get("stage_verdict") == "blocked" or task.get("status") == "blocked":
        return "你确认产物后点放行或打回，无需敲命令。"
    if task.get("status") == "failed":
        return "建议换执行者重跑一次，旧任务保留备查。"
    return "暂无自动动作，先看任务详情。"
