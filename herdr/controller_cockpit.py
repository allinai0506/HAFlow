"""Pure Workflow Cockpit projection over caller-provided authoritative snapshots.

The shell owns coherent, bounded reads. This module performs no I/O and neither
dispatches work nor treats a recommendation as an execution receipt.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from heapq import nlargest

from .controller_actions import action_automation, controller_action_type, resolve_workflow_blockers
from .workflow import resolve_control_policy

MAX_TASKS = 200
MAX_ACTIONS = 100
MAX_TIMELINE = 100
MAX_NODES = 200
MAX_RECOVERY = 100
# Row counts do not bound bytes: agent-produced text needs explicit budgets so
# one oversized field cannot dominate a Cockpit response.
MAX_FIELD_BYTES = 4096
MAX_TASK_BYTES = 8192
MAX_WORKFLOW_BYTES = 32768
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Identity, policy and safety facts stay complete; only display text is bounded.
TASK_IDENTITY_FIELDS = frozenset({
    "task_id", "workflow_id", "parent_task_id", "superseded_by", "node", "stage",
    "status", "stage_verdict", "stage_verdict_note", "agent", "run_id", "execution_id",
    "candidate_sha", "pane_id", "version", "created_at", "updated_at",
})
WORKFLOW_IDENTITY_FIELDS = frozenset({
    "workflow_id", "title", "template_name", "current_stage", "status", "outcome",
    "created_at", "updated_at", "execution_id", "run_id", "candidate_sha",
    "candidate_episode_id", "candidate_source_node", "agent_override",
})


def _utf8_len(value):
    return len(value.encode("utf-8", "ignore"))


def _bounded_copy(record, protected, field_limit, row_limit):
    """Display copy with explicit truncation markers; identity fields stay complete.

    Truncation is recorded per field under ``_truncated_fields`` so a shortened
    value is distinguishable from an absent or unknown one. The marker list
    lives on the row while budgeting, so its own bytes count against the limit.
    """
    row = deepcopy(record)
    truncated = {}
    sizes = {}
    for key, value in row.items():
        if key in protected:
            continue
        if isinstance(value, str):
            sizes[key] = _utf8_len(value)
        elif isinstance(value, (dict, list)):
            sizes[key] = _utf8_len(json.dumps(value, ensure_ascii=False, default=str))

    def shrink(key, keep):
        # sizes[key] 始终是该字段“当前剩余字节”：字段级截断后行预算二段
        # 仍能继续收缩同一字段（PR #343 评审遗留：归零会让 3×100KB 字段
        # 组出 12.5KB 的行）。omitted 从截断前尺寸累计，保证标记如实。
        original = row[key]
        if isinstance(original, str):
            row[key] = original.encode("utf-8")[:keep].decode("utf-8", "ignore")
            omitted = max(0, sizes[key] - keep)
            sizes[key] = _utf8_len(row[key])
        else:
            row[key] = {"truncated": True, "omitted_bytes": sizes[key]}
            omitted = sizes[key]
            sizes[key] = 0
        entry = truncated.get(key)
        if entry is None:
            entry = {"field": key, "omitted_bytes": 0}
            truncated[key] = entry
            row["_truncated_fields"] = sorted(truncated.values(), key=lambda item: item["field"])
        entry["omitted_bytes"] += omitted

    for key in sorted(sizes, key=sizes.get, reverse=True):
        if sizes[key] <= field_limit:
            break
        shrink(key, field_limit)

    def row_bytes():
        return _utf8_len(json.dumps(row, ensure_ascii=False, default=str))

    while sizes:
        excess = row_bytes() - row_limit
        if excess <= 0:
            break
        key = max(sizes, key=sizes.get)
        if sizes[key] <= 128:
            break
        # Cut enough for this field to absorb the whole overflow, including
        # the marker entry it may add, so the loop converges immediately.
        marker_growth = 0 if key in truncated else 96
        shrink(key, max(0, sizes[key] - excess - marker_growth))
    if not truncated:
        row.pop("_truncated_fields", None)
    return row


def _bounded_response(result):
    """Shed optional display sections before the whole read fails closed."""
    sheds = []

    def size():
        return _utf8_len(json.dumps(result, ensure_ascii=False, default=str))

    if size() <= MAX_RESPONSE_BYTES:
        return sheds
    for event in result.get("timeline") or []:
        event.pop("payload", None)
    sheds.append("timeline_payloads")
    if size() > MAX_RESPONSE_BYTES:
        for node in result.get("nodes") or []:
            node["tasks"] = []
        sheds.append("node_task_rows")
    return sheds

def _bound_shell_sections(result):
    """Envelope 预算同样约束 read_cockpit 后置追加的 shell 段（PR #343 评审遗留）。

    blockers/receipts 在 build_cockpit_projection 的预算检查之后拼装：超大
    persisted receipts 可让 2MB+ 响应仍报 confirmed。先按最旧优先丢弃
    receipts（append 时 DESC 序，pop 即最旧），仍超限则与投影尾部同口径
    fail-closed。
    """
    sheds = []

    def envelope_size():
        return _utf8_len(json.dumps(result, ensure_ascii=False, default=str))

    while envelope_size() > MAX_RESPONSE_BYTES and result.get("receipts"):
        result["receipts"].pop()
        sheds.append("receipts")
    if sheds:
        result["response_shed"] = [*(result.get("response_shed") or []), *sheds]
    if envelope_size() > MAX_RESPONSE_BYTES:
        result["read_state"] = "unknown"
        result["errors"] = {**result["errors"], "response_budget": "exceeded"}
    return result


def _owned(record, workflow_id):
    return isinstance(record, dict) and record.get("workflow_id") == workflow_id


def _historical(task, workflow):
    if task.get("status") == "superseded" or task.get("superseded_by"):
        return True
    if task.get("status") == "cleaned" and not resolve_workflow_blockers([task], workflow):
        return True
    return any(task.get(key) and workflow.get(key) and task[key] != workflow[key]
               for key in ("execution_id", "candidate_sha"))


def _stale(action, task):
    target = action.get("target") or {}
    expected = action.get("expected_identity") or {}
    pane = task.get("pane_id") or (task.get("runtime") or {}).get("pane_id")
    actual = {**task, "node_id": task.get("node") or task.get("stage"), "pane_id": pane,
              "task_version": task.get("version")}
    # Missing facts remain unknown. Only a proven mismatch makes an action obsolete.
    return any(actual.get(key) is not None and actual.get(key) != "" and value != actual[key]
               for identity in (target, expected) for key, value in identity.items()
               if key in {"workflow_id", "node_id", "task_id", "run_id", "pane_id",
                          "task_version", "execution_id", "candidate_sha"})


def _event_order(event):
    value = event.get("timestamp")
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            timestamp = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, OverflowError):
            timestamp = float("-inf")
    return timestamp, event.get("id") if isinstance(event.get("id"), int) else 0


def _project_actions(actions, workflow, config, all_tasks, unresolved, read_unknown, graph_node_ids,
                     action_id=None, target_task_id=None):
    wid = workflow.get("workflow_id")
    by_id = {task.get("task_id"): task for task in all_tasks if isinstance(task, dict) and task.get("task_id")}
    projected = []
    seen = set()
    # Reserve the front of the same bounded inventory for Workflow controls.
    # Tasks can otherwise consume the entire window before resume is visited.
    ordered = sorted(actions, key=lambda a: bool((a.target if hasattr(a, 'target') else a.get('target', {})).get('task_id')))
    for raw in ordered:
        raw_id = raw.action_id if hasattr(raw, 'action_id') else raw.get('action_id')
        if action_id is not None and raw_id != action_id:
            continue
        if target_task_id is not None:
            # Deep links resolve the exact target independently of the display cap.
            raw_target = (raw.target if hasattr(raw, 'target') else raw.get('target', {})) or {}
            raw_blocker = raw.blocker_task_id if hasattr(raw, 'blocker_task_id') else (raw.get('blocker_task_id') or '')
            if raw_target.get('task_id') != target_task_id and raw_blocker != target_task_id:
                continue
        action = raw.to_dict() if hasattr(raw, "to_dict") else deepcopy(raw)
        target = action.get("target") or {}
        payload = action.get("api_payload") or {}
        if any(value and value != wid for value in (target.get("workflow_id"), payload.get("workflow_id"))):
            continue
        tid = target.get("task_id") or action.get("blocker_task_id")
        task = by_id.get(tid)
        if not tid and _stale(action, workflow):
            continue
        if task and ((task.get("workflow_id") and task["workflow_id"] != wid)
                     or _historical(task, workflow) or _stale(action, task)):
            continue
        # The existing execution boundary forbids these while *any* Workflow
        # recovery obligation is unresolved, even when it names a different Task.
        if unresolved and controller_action_type(action) in {"launch", "rework", "force_pass", "force_pass_advance"}:
            continue
        if action.get("action_id") in seen:
            continue
        seen.add(action.get("action_id"))
        action["automation"], action["risk"] = action_automation(
            action, config, task, workflow, read_unknown=read_unknown, graph_node_ids=graph_node_ids)
        projected.append(action)
        if len(projected) > MAX_ACTIONS:
            break
    return projected[:MAX_ACTIONS], len(projected) > MAX_ACTIONS


def _judgment(workflow, nodes, current, ready, unresolved, actions, read_unknown):
    waiting = [node for node in nodes if node.get("status") == "waiting"]
    next_nodes = ready or [node["id"] for node in waiting
                           if any(dep in current for dep in node.get("depends_on") or [])]
    next_step = "start_" + "_".join(next_nodes) if next_nodes else "unknown"
    requires_user = (any(op.get("status") == "waiting_human" for op in unresolved)
                     or any(a.get("recommended") and a["automation"]["mode"] == "confirmation_required"
                            for a in actions))
    if read_unknown:
        return {"now": "unknown", "blocked_reason": "read_unknown", "next": "refresh", "requires_user": requires_user}
    if unresolved:
        return {"now": "recovery_pending", "blocked_reason": "recovery_unresolved",
                "next": "resolve_recovery", "requires_user": requires_user}
    status = workflow.get("status")
    if status in {"completed", "cleaned", "archived", "paused", "failed"}:
        return {"now": status, "blocked_reason": "workflow_paused" if status == "paused" else "",
                "next": "resume" if status == "paused" else "none", "requires_user": status == "paused"}
    completed = {n["id"] for n in nodes if n.get("status") == "completed"}
    blocked = [n for n in nodes if n.get("status") in {"blocked", "failed"}]
    blocked_reason = ("node_blocked" if blocked else "dependencies_incomplete"
                      if any(dep not in completed for n in waiting for dep in n.get("depends_on") or []) else "")
    now = "_".join(current) + "_running" if current else "ready" if ready else "waiting" if waiting else "unknown"
    return {"now": now, "blocked_reason": blocked_reason, "next": next_step, "requires_user": requires_user}


def build_cockpit_projection(*, workflow, config, tasks, graph, actions, recovery, controller, events,
                             errors=None, action_id=None, target_task_id=None):
    """Assemble a bounded read model; absent snapshots always remain unknown.

    Recovery, events and Tasks must be Workflow-scoped by the shell. Explicit
    foreign records are still excluded here. Truncation is display-only: action
    attribution is checked against all supplied Tasks before applying that cap.
    """
    missing = [name for name, value in (("workflow", workflow), ("config", config), ("tasks", tasks),
                                      ("graph", graph), ("actions", actions), ("recovery", recovery),
                                      ("controller", controller), ("events", events)) if value is None]
    workflow = workflow or {}
    config = config or {}
    # Resolved before any display truncation: policy facts never come from a
    # bounded copy, and an unavailable config stays explicitly unknown.
    control_policy = resolve_control_policy(config, "", "advance") if config.get("nodes") else None
    graph = graph or {}
    runtime = deepcopy(controller or {})
    runtime["status"] = runtime.get("status") or "unknown"
    read_unknown = bool(errors or missing or not workflow.get("workflow_id")
                        or not config.get("nodes") or not graph.get("nodes")
                        or runtime["status"] == "unknown")
    wid = workflow.get("workflow_id")
    all_tasks = tasks or []
    owned_tasks = [task for task in all_tasks if _owned(task, wid)]
    visible_tasks = owned_tasks[:MAX_TASKS]
    by_node = {}
    for task in visible_tasks:
        by_node.setdefault(task.get("node") or task.get("stage"), []).append(
            _bounded_copy(task, TASK_IDENTITY_FIELDS, MAX_FIELD_BYTES, MAX_TASK_BYTES))
    nodes = []
    for node in (graph.get("nodes") or [])[:MAX_NODES]:
        row = deepcopy(node)
        row["tasks"] = by_node.get(row.get("id"), [])
        nodes.append(row)
    current = list(graph.get("current_nodes") or [n["id"] for n in nodes if n.get("status") in {"working", "rework"}])
    ready = list(graph.get("ready_nodes") or [])
    scoped_recovery = [op for op in recovery or [] if isinstance(op, dict)
                       and op.get("workflow_id") in (None, "", wid)]
    unresolved = [op for op in scoped_recovery if op.get("status") not in {"resolved", "superseded"}]
    projected_actions, actions_truncated = _project_actions(actions or [], workflow, config, all_tasks,
                                                           unresolved, read_unknown,
                                                           {node.get("id") for node in graph.get("nodes") or []},
                                                           action_id, target_task_id)
    timeline = nlargest(MAX_TIMELINE + 1, (event for event in events or [] if _owned(event, wid)), key=_event_order)
    todos = [{"kind": "recovery", **deepcopy(op)} for op in unresolved[:MAX_RECOVERY]]
    todos.extend({"kind": "action", "action_id": a.get("action_id"), "title": a.get("title"),
                  "target": deepcopy(a.get("target") or {}), "automation": deepcopy(a["automation"])}
                 for a in projected_actions if a.get("recommended") and a["automation"]["mode"] != "automatic")
    result = {
        "schema": "controller-cockpit/v1",
        "workflow": _bounded_copy(workflow, WORKFLOW_IDENTITY_FIELDS, MAX_FIELD_BYTES, MAX_WORKFLOW_BYTES),
        "controller": runtime,
        "control_policy": control_policy,
        "judgment": _judgment(workflow, nodes, current, ready, unresolved, projected_actions, read_unknown),
        "read_state": "unknown" if read_unknown else "confirmed", "errors": deepcopy(errors or {}),
        "missing_sources": missing, "nodes": nodes, "current_nodes": current, "ready_nodes": ready,
        "tasks": [_bounded_copy(task, TASK_IDENTITY_FIELDS, MAX_FIELD_BYTES, MAX_TASK_BYTES)
                  for task in visible_tasks],
        "todos": todos, "actions": projected_actions,
        "recovery": deepcopy(scoped_recovery[:MAX_RECOVERY]), "timeline": deepcopy(timeline[:MAX_TIMELINE]),
        "tasks_truncated": len(owned_tasks) > MAX_TASKS, "actions_truncated": actions_truncated,
        "timeline_truncated": len(timeline) > MAX_TIMELINE,
        "nodes_truncated": len(graph.get("nodes") or []) > MAX_NODES,
        "recovery_truncated": len(scoped_recovery) > MAX_RECOVERY,
    }
    sheds = _bounded_response(result)
    if sheds:
        result["response_shed"] = sheds
    if _utf8_len(json.dumps(result, ensure_ascii=False, default=str)) > MAX_RESPONSE_BYTES:
        # Budgets failed even after shedding optional sections: fail closed
        # instead of serving a read that cannot be trusted for execution.
        result["read_state"] = "unknown"
        result["errors"] = {**result["errors"], "response_budget": "exceeded"}
    return result
