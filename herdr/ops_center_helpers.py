"""Small shared helpers for the read-only Ops Center projection."""
import json
import subprocess
import time
from pathlib import Path

from herdr.agent_router import DEFAULT_ALLOWED
from herdr.transitions import (
    ACTIVE_TASK_STATUSES, COMPLETED_TASK_STATUSES,
    TERMINAL_TASK_STATUSES as TERMINAL_STATUSES,
)

KNOWN_AGENTS = [agent for agent in DEFAULT_ALLOWED if agent]

ANOMALY_ACTIONS = {
    "BLOCKED": ["打开 Pane", "重试", "换 Agent", "禁用 Agent", "让总指挥处理", "自动修复"],
    "FAILED": ["打开 Pane", "重试", "换 Agent", "禁用 Agent", "让总指挥处理", "自动修复"],
    "AUTH_REQUIRED": ["打开 Pane", "换 Agent", "禁用 Agent", "让总指挥处理"],
    "TOKEN_EXHAUSTED": ["打开 Pane", "换 Agent", "让总指挥处理", "自动修复"],
    "TRUST_REQUIRED": ["打开 Pane", "换 Agent", "让总指挥处理"],
    "UPDATE_BLOCKED": ["打开 Pane", "重试", "让总指挥处理", "自动修复"],
    "ANCHOR_MISSING": ["打开 Pane", "让总指挥处理", "自动修复"],
    "PANE_NOT_FOUND": ["打开 Pane", "让总指挥处理", "自动修复"],
    "CONTROLLER_RECOVERY": ["让总指挥处理", "自动修复"],
}


def _now():
    return time.time()

def _coerce_epoch(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None

def _init_status_history(task, initial_status, now):
    task["created_at"] = now
    task["updated_at"] = now
    task["last_activity_at"] = now
    task["status_history"] = [
        {
            "status": initial_status,
            "at": now,
            "from": None,
            "to": initial_status,
        }
    ]

def _append_status_history(task, old_status, new_status, now):
    history = task.get("status_history")
    if not isinstance(history, list):
        history = []
        if old_status:
            history.append(
                {
                    "status": old_status,
                    "at": task.get("updated_at") or now,
                    "from": None,
                    "to": old_status,
                }
            )
    history.append(
        {
            "status": new_status,
            "at": now,
            "from": old_status,
            "to": new_status,
        }
    )
    task["status_history"] = history

def _safe_float(value):
    return _coerce_epoch(value)

def _elapsed_seconds(started_at, now):
    started = _safe_float(started_at)
    if started is None:
        return None
    elapsed = now - started
    if elapsed < 0:
        return 0
    return round(elapsed, 2)

def _task_status_trajectory(task):
    history = task.get("status_history")
    if isinstance(history, list) and history:
        result = []
        for entry in history:
            if isinstance(entry, dict):
                status = entry.get("to") or entry.get("status")
            else:
                status = None

            if status and (not result or result[-1] != status):
                result.append(status)

        if result:
            return result

    status = task.get("status")
    return [status] if status else []

def _agent_runtime_status(pane_id):
    if not pane_id:
        return None

    try:
        proc = subprocess.run(
            ["herdr", "agent", "get", pane_id],
            text=True,
            capture_output=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    try:
        return json.loads(proc.stdout).get("result", {}).get("agent", {}).get(
            "agent_status"
        )
    except Exception:
        return None

def _normalize_workflow_nodes(workflow):
    nodes = workflow.get("nodes")
    if nodes is None:
        nodes = workflow.get("stages", [])
    return [
        {
            "id": item.get("id") or item.get("key"),
            "label": item.get("label") or item.get("name") or item.get("id") or item.get("key"),
            "next": item.get("next"),
            "required_task_ids": item.get("required_task_ids"),
        }
        for item in nodes
        if (item.get("id") or item.get("key"))
    ]

def _node_task_status_counts(tasks_for_node):
    counts = {
        "total": 0,
        "active": 0,
        "blocked": 0,
        "failed": 0,
        "completed": 0,
        "superseded": 0,
    }

    for task in tasks_for_node:
        status = task.get("status")
        # Superseded tasks (replaced or abandoned) must not pollute the live
        # totals. Keep this predicate in sync with is_node_complete in
        # services/herdr-controller.py and node_status below.
        if status == "superseded" or task.get("superseded_by"):
            counts["superseded"] += 1
            continue
        counts["total"] += 1
        if status in ACTIVE_TASK_STATUSES or (status in ("pending",)):
            counts["active"] += 1
        if status == "blocked":
            counts["blocked"] += 1
        if status == "failed":
            counts["failed"] += 1
        if status in COMPLETED_TASK_STATUSES:
            counts["completed"] += 1

    return counts

def _task_healthy_status(task_status, runtime_status, load=0):
    if task_status in TERMINAL_STATUSES and task_status != "failed":
        return "READY" if load == 0 else "BUSY"

    if task_status in {"failed", "superseded"}:
        return "FAILED" if task_status == "failed" else "OFF"

    if task_status == "blocked":
        return "BLOCKED"

    if task_status in {"dispatched", "working", "rework", "agent_done"}:
        if runtime_status in {"working", "done", "active"}:
            return "BUSY"
        if runtime_status == "blocked":
            return "BLOCKED"
        if runtime_status in {"idle", None, "unknown"}:
            return "STALE"
        return "BUSY"

    if task_status == "idle":
        if load > 0:
            return "STALE"
        return "OFF"

    if task_status == "pending":
        return "READY" if runtime_status is None else "BUSY"

    return "READY"

def _trajectory_bucket(elapsed):
    if elapsed is None:
        return None
    if elapsed <= 600:
        return "normal"
    if elapsed <= 1800:
        return "slow"
    return "stuck"

def _task_summaries_for_ops(tasks, include_detail=False, now=None):
    now = _now() if now is None else float(now)
    summaries = []
    for task in tasks:
        started_at = _coerce_epoch(task.get("started_at") or task.get("created_at"))
        started_at = started_at if started_at is not None else None
        last_activity = (
            _coerce_epoch(task.get("last_activity_at"))
            or _coerce_epoch(task.get("updated_at"))
            or now
        )
        summary = {
            "task_id": task.get("task_id"),
            "agent": task.get("agent"),
            "status": task.get("status"),
            "node": task.get("node") or task.get("stage"),
            "started_at": started_at,
            "last_activity_at": last_activity,
            "elapsed": _elapsed_seconds(started_at, now),
            "elapsed_bucket": _trajectory_bucket(_elapsed_seconds(started_at, now)),
            "pane_id": task.get("pane_id"),
        }
        if include_detail:
            summary["status_trajectory"] = _task_status_trajectory(task)
        summaries.append(summary)
    return summaries

def _format_age(seconds):
    if seconds is None:
        return None
    if seconds < 60:
        return "刚刚"

    mins = int(seconds // 60)
    if mins < 60:
        return f"{mins}m ago"

    hours = mins // 60
    mins = mins % 60
    return f"{hours}h {mins}m ago"

def _collect_workflow_node_metadata(workflow):
    nodes = _normalize_workflow_nodes(workflow)
    node_meta = {}
    for node in nodes:
        node_id = node.get("id")
        if node_id:
            node_meta[node_id] = {
                "label": node.get("label", node_id),
                "next": node.get("next"),
                "required_task_ids": node.get("required_task_ids"),
            }
    return node_meta

def _safe_workflow(workflow_id):
    if not workflow_id:
        return None, "unknown-workflow"

    try:
        from herdr.projects import workflow_config_for
        from herdr.state_store import get_state_store
        from herdr.workflow import normalize_workflow

        record = get_state_store().get_workflow(workflow_id)
        workflow = normalize_workflow(record) if record else workflow_config_for(workflow_id)
        if not workflow:
            return {"configuration_error": "workflow_config_invalid"}, workflow_id
        workflow_label = workflow.get("title") or workflow.get("name") or workflow_id
        return workflow, workflow_label
    except Exception:
        return {"configuration_error": "workflow_config_invalid"}, workflow_id

def _safe_text(value):
    return str(value) if value is not None else ""

def _anomaly_reason_text(task):
    return " ".join(
        part.lower()
        for part in [
            _safe_text(task.get("failure")),
            _safe_text(task.get("failure_reason")),
            _safe_text(task.get("error")),
            _safe_text(task.get("message")),
            _safe_text(task.get("result")),
            _safe_text(task.get("last_result")),
            _safe_text(task.get("sentinel_reason")),
        ]
        if part
    ).strip()

def _contains_any(source, *candidates):
    if not source:
        return False

    return any(candidate in source for candidate in candidates)

def _classify_task_anomalies(task, runtime_status, now):
    status = task.get("status")
    reason_text = _anomaly_reason_text(task)
    anomalies = []

    if status == "blocked":
        anomalies.append("BLOCKED")
    if status == "failed":
        if "auth" in reason_text or "login" in reason_text or "登录" in reason_text:
            anomalies.append("AUTH_REQUIRED")
        elif (
            "token" in reason_text
            and (
                "exhaust" in reason_text
                or "limit" in reason_text
                or "insufficient" in reason_text
                or "额度" in reason_text
            )
        ):
            anomalies.append("TOKEN_EXHAUSTED")
        elif "trust" in reason_text:
            anomalies.append("TRUST_REQUIRED")
        elif (
            "update" in reason_text
            and ("blocked" in reason_text or "阻塞" in reason_text or "失败" in reason_text)
        ):
            anomalies.append("UPDATE_BLOCKED")
        elif (
            "anchor" in reason_text
            and ("missing" in reason_text or "不存在" in reason_text)
        ):
            anomalies.append("ANCHOR_MISSING")
        elif (
            "pane" in reason_text
            and ("missing" in reason_text or "不存在" in reason_text or "找不到" in reason_text)
        ):
            anomalies.append("PANE_NOT_FOUND")
        elif _contains_any(
            reason_text,
            "agent_process_crash",
            "controller_recovery",
            "controller restart",
            "recovery",
        ):
            anomalies.append("CONTROLLER_RECOVERY")
        elif _coerce_epoch(task.get("failed_at")) is not None and (
            now - _coerce_epoch(task.get("failed_at")) > 180
        ):
            # 长时间失败后仍停留，不易恢复时，提升为控制器级异常提示。
            anomalies.append("CONTROLLER_RECOVERY")
        else:
            anomalies.append("FAILED")

    if status in ACTIVE_TASK_STATUSES:
        if not task.get("pane_id"):
            anomalies.append("PANE_NOT_FOUND")
        elif runtime_status is None:
            anomalies.append("CONTROLLER_RECOVERY")

    if status == "failed" and not anomalies:
        anomalies.append("FAILED")

    last_activity = _coerce_epoch(task.get("last_activity_at")) or _coerce_epoch(
        task.get("updated_at")
    ) or _coerce_epoch(task.get("created_at"))
    if (
        status in ("dispatched", "working", "agent_done", "rework", "blocked")
        and runtime_status in {"idle", "unknown", None}
        and last_activity is not None
        and (now - last_activity) > 600
    ):
        anomalies.append("BLOCKED")

    if (
        status in ("pending", "dispatched")
        and not task.get("acceptance_criteria")
        and last_activity is not None
        and (now - last_activity) > 1200
    ):
        anomalies.append("AUTH_REQUIRED")

    seen = []
    ordered = [
        "BLOCKED",
        "FAILED",
        "AUTH_REQUIRED",
        "TOKEN_EXHAUSTED",
        "TRUST_REQUIRED",
        "UPDATE_BLOCKED",
        "ANCHOR_MISSING",
        "PANE_NOT_FOUND",
        "CONTROLLER_RECOVERY",
    ]
    for kind in ordered:
        if kind in anomalies and kind not in seen:
            seen.append(kind)

    return seen

def _suggested_actions_for_anomaly(kind, task):
    actions = ANOMALY_ACTIONS.get(kind, [])
    pane_id = task.get("pane_id")
    task_id = task.get("task_id")
    links = {
        "open_task": f"herdr-task get {task_id}" if task_id else None,
        "open_pane": f"herdr pane read {pane_id} --source visible" if pane_id else None,
        "open_logs": f"herdr pane read {pane_id} --source logfile" if pane_id else None,
        "retry_task": f"herdr-task dispatch {task_id} --prompt \"<重新下发 prompt>\"" if task_id else None,
    }

    return {
        "kind": kind,
        "actions": [action for action in actions],
        "links": {k: v for k, v in links.items() if v},
    }

def _select_drilldown_task(tasks_for_node):
    def _sort_key(task):
        started = _coerce_epoch(task.get("started_at"))
        if started is None:
            started = 0
        last = _coerce_epoch(task.get("last_activity_at")) or _coerce_epoch(task.get("updated_at")) or 0
        return (task.get("status") in ACTIVE_TASK_STATUSES, last, started)

    selected = None
    for task in tasks_for_node:
        if task.get("status") == "failed":
            selected = task
            break
    if selected is not None:
        return selected

    candidates = [
        task for task in tasks_for_node
        if task.get("status") not in TERMINAL_STATUSES
    ]
    if not candidates:
        # All tasks terminal — prefer the latest authoritative task (the
        # replacement) so drilldown never lands on a retired one.
        authoritative = [
            task for task in tasks_for_node
            if task.get("status") != "superseded" and not task.get("superseded_by")
        ]
        pool = authoritative or tasks_for_node
        return max(
            pool,
            key=lambda task: (
                _coerce_epoch(task.get("updated_at"))
                or _coerce_epoch(task.get("last_activity_at"))
                or _coerce_epoch(task.get("created_at"))
                or 0
            ),
        )

    candidates.sort(key=_sort_key, reverse=True)
    return candidates[0]

def _node_delivery_complete(tasks, required_task_ids=None):
    from herdr.scheduler import node_is_complete
    return node_is_complete(tasks, required_task_ids)
