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

from .reason_labels import human_reason

ACTIVE_STATUSES = {"pending", "dispatched", "working", "blocked",
                   "agent_done", "rework", "failed"}
EXECUTING_STATUSES = {"pending", "dispatched", "working", "agent_done", "rework"}
REPLY_STEER_STATUSES = {"dispatched", "working", "rework", "blocked", "paused", "interrupted"}
CLOSED_STATUSES = {"completed", "committed", "integrated", "cleaned", "superseded"}
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


def reply_mode_for(task: Mapping[str, Any]) -> str:
    """How a person can answer this task from the dashboard. No I/O."""
    status = str((task or {}).get("status") or "")
    if status in REPLY_STEER_STATUSES:
        return "steer"
    if status == "failed":
        return "resume"
    return "none"


def reply_plan(task: Mapping[str, Any], instruction: Any) -> Dict[str, Any]:
    """Decide how one human sentence continues a task. No I/O, no force-pass."""
    text = str(instruction or "").strip()
    if not text:
        return {"ok": False, "error": "请先写一句话，说明接下来怎么做"}
    if len(text) > 1000:
        return {"ok": False, "error": "一句话太长了，请收短到 1000 字以内"}
    if not task or not task.get("task_id"):
        return {"ok": False, "error": "找不到这件任务"}
    mode = reply_mode_for(task)
    tid = str(task.get("task_id"))
    if mode == "steer":
        return {
            "ok": True,
            "mode": "steer",
            "task_id": tid,
            "instruction": text,
            "urgent": True,
            "message": "已经送到这个工位，它会按你的话继续。",
        }
    if mode != "resume":
        return {"ok": False, "error": "这个任务已经结束，回复送不进去。"}
    wid = str(task.get("workflow_id") or "").strip()
    if not wid:
        return {"ok": False, "error": "这件任务没有工作流，不能重新开始"}
    from .controller_actions import inherit_experience_policy, next_replacement_id

    stage = str(task.get("stage") or task.get("node") or "implementation").strip() or "implementation"
    agent = str(task.get("agent") or "auto").strip() or "auto"
    goal = str(task.get("goal") or f"继续完成{stage}")[:500]
    new_id = next_replacement_id(tid, f"{wid}-{stage}-r2")
    prompt = (
        f"人的回复：{text}\n\n"
        "请按这句话继续完成原来的任务，不要另起一套方案。"
        f"原目标：{goal}"
    )
    launch: Dict[str, Any] = {
        "type": "launch",
        "task_id": new_id,
        "workflow_id": wid,
        "stage": stage,
        "agent": agent,
        "supersedes": tid,
        "supersede_reason": "human replied and asked to continue",
        "goal": goal,
        "prompt": prompt[:4000],
        **inherit_experience_policy(dict(task)),
    }
    source = str(task.get("project_root") or task.get("source") or "").strip()
    if source:
        launch["source"] = source
    return {
        "ok": True,
        "mode": "resume",
        "task_id": tid,
        "new_task_id": new_id,
        "message": "已按你的话重新开始，旧任务保留备查。",
        "launch": launch,
    }


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
    decisions: Optional[List[Mapping[str, Any]]] = None,
    scope: Optional[str] = None,
    workflows: Optional[List[Mapping[str, Any]]] = None,
    now: Optional[float] = None,
    limits: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    import time as _time

    now_f = float(now) if now is not None else _time.time()
    lim = {"tasks": 50, "attention": 30, "deliveries": 10, "stuck": 30,
           "decisions": 30}
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
        if _is_history(t) or t.get("status") in CLOSED_STATUSES:
            bucket = 2
        elif t.get("status") in EXECUTING_STATUSES:
            bucket = 0
        else:
            bucket = 1
        upd = _epoch(t.get("last_activity_at")) or _epoch(t.get("updated_at")) or 0.0
        return (bucket, -upd)

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

    # --- decisions: open asks awaiting a human ruling ---
    # An open decision is not a task, so it gets its own identity key and
    # also joins `attention` (the console's single "等你的问题" surface).
    decision_list: List[Dict[str, Any]] = []
    for x in decisions or []:
        if not isinstance(x, Mapping):
            continue
        did = str(x.get("decision_id") or "").strip()
        if not did:
            continue
        raised = _epoch(x.get("raised_at"))
        question = str(x.get("question") or x.get("title") or "").strip()
        options = [str(o) for o in (x.get("options") or []) if str(o).strip()]
        recommended = str(x.get("recommended") or "").strip()
        decision_list.append({
            "decision_id": did,
            "workflow_id": x.get("workflow_id") or "unknown",
            "node": x.get("node") or "—",
            "title": str(x.get("title") or did)[:200],
            "question": question[:300],
            "options": options,
            "recommended": recommended,
            "reason": question[:300] or f"待你裁决：{did}",
            "default_action": f"裁决 {did}",
            "default_action_text": (
                (question[:200] + "。") if question else ""
            ) + "确认后点“已拍板”把你的结论落库，总指挥下一轮即可读到。",
            "raised_at": raised,
            "raised_at_text": format_clock(raised),
        })
    decision_list.sort(key=lambda x: -(x.get("raised_at") or 0.0))
    decision_items = decision_list[: lim["decisions"]]

    titles = {
        w.get("workflow_id"): (w.get("title") or w.get("workflow_id"))
        for w in (workflows or [])
        if isinstance(w, Mapping) and w.get("workflow_id")
    }
    by_id = {}
    for src in task_list:
        src_id = src.get("task_id")
        if src_id and src_id not in by_id:
            by_id[src_id] = src

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
        src = by_id.get(tid) or t
        reason = _attention_reason(src)
        if not reason:
            reason = str(src.get("blocker") or src.get("stage_verdict_note") or "等你看一眼")
        reason = human_reason(reason)
        upd = _epoch(src.get("last_activity_at")) or _epoch(src.get("updated_at")) or _epoch(t.get("last_activity_at")) or _epoch(t.get("updated_at"))
        wid = src.get("workflow_id") or t.get("workflow_id") or "unknown"
        seen[tid] = {
            "task_id": tid,
            "workflow_id": wid,
            "workflow_title": titles.get(wid) or wid,
            "node": src.get("node") or src.get("stage") or t.get("node") or t.get("stage") or "—",
            "pane_id": src.get("pane_id") or t.get("pane_id") or "",
            "status": src.get("status") or t.get("status") or "",
            "goal": str(src.get("goal") or "")[:160],
            "reply_mode": reply_mode_for(src),
            "reason": reason[:300],
            "default_action": str(act.get("title") or _default_action_title(src))[:100],
            "default_action_text": str(act.get("effect") or _default_action_text(src))[:300],
            "command": str(act.get("command_line") or "")[:300],
            "endpoint": str(act.get("api_endpoint") or "")[:200],
            "payload": dict(act.get("api_payload") or {}) if isinstance(act.get("api_payload"), Mapping) else {},
            "updated_at": upd,
            "updated_at_text": format_clock(upd),
        }
    # A decision has no one-click executor: the ruling text is the human's.
    # Keyed by workflow too, so two workflows reusing an id both stay visible.
    for dec in decision_items:
        key = f"decision:{dec['workflow_id']}:{dec['decision_id']}"
        seen.setdefault(key, {
            "task_id": "",
            "decision_id": dec["decision_id"],
            "workflow_id": dec["workflow_id"],
            "workflow_title": titles.get(dec["workflow_id"]) or dec["workflow_id"],
            "node": dec["node"],
            "pane_id": "",
            "status": "",
            "goal": "",
            "reply_mode": "decide",
            "reason": dec["reason"],
            "default_action": dec["default_action"],
            "default_action_text": dec["default_action_text"],
            "command": "",
            "options": dec["options"],
            "recommended": dec["recommended"],
            "updated_at": dec["raised_at"],
            "updated_at_text": dec["raised_at_text"],
        })
    ordered_attention = sorted(seen.values(),
                               key=lambda a: -(a.get("updated_at") or 0.0))
    attention = ordered_attention[: lim["attention"]]

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
            "workflow_title": titles.get(wid) or wid,
            "reason": human_reason(str(s.get("message")))[:300],
            "suggestion": str(s.get("suggested_action") or "")[:100],
            "reply_mode": "none",
            "last_event_text": format_clock(now_f),
        })
    for a in anomaly_list:
        la = _epoch(a.get("last_activity_at"))
        awid = a.get("workflow_id") or "unknown"
        atid = a.get("task_id") or ""
        stuck.append({
            "task_id": atid,
            "workflow_id": awid,
            "workflow_title": titles.get(awid) or awid,
            "reason": "任务失败了，写一句话就能让它按你的话继续",
            "suggestion": "写一句话，按原执行者继续",
            "reply_mode": "resume" if atid else "none",
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
        "decisions": decision_items,
        "deliveries": delivery_items,
        "stuck": stuck,
        "counts": {
            "tasks": len(task_list),
            "live": sum(1 for t in task_list if t.get("status") in EXECUTING_STATUSES and not _is_history(t)),
            "history": sum(1 for t in task_list if _is_history(t) or t.get("status") in CLOSED_STATUSES),
            # Count what is actually rendered, not the pre-slice total, so the
            # KPI can never disagree with the list underneath it.
            "attention": len(attention),
            "decisions": len(decision_items),
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
