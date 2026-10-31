"""Read-only Ops Center card and inventory projections."""
from herdr.ops_center_helpers import (
    ACTIVE_TASK_STATUSES, COMPLETED_TASK_STATUSES, TERMINAL_STATUSES,
    KNOWN_AGENTS, ANOMALY_ACTIONS, _agent_runtime_status, _format_age, _now,
    _task_summaries_for_ops, _collect_workflow_node_metadata, _node_task_status_counts,
    _select_drilldown_task, _trajectory_bucket, _coerce_epoch, _elapsed_seconds,
    _task_healthy_status, _classify_task_anomalies, _suggested_actions_for_anomaly,
    _node_delivery_complete, _safe_workflow,
)
import json

def _build_workflow_cards(workflows_tasks, now, all_tasks=None, workflow_lookup=None):
    from herdr.scheduler import required_task_issues
    inventory = all_tasks if all_tasks is not None else [
        t for group in workflows_tasks.values() for t in group]
    cards = []
    for workflow_id in sorted(workflows_tasks):
        wf_tasks = workflows_tasks.get(workflow_id, [])
        workflow, workflow_label = (workflow_lookup or _safe_workflow)(workflow_id)
        node_meta = _collect_workflow_node_metadata(workflow or {})

        all_nodes = {}
        for task in wf_tasks:
            node_id = task.get("node") or task.get("stage") or "unknown"
            all_nodes.setdefault(node_id, []).append(task)

        # 显示到 dashboard 时只做“卡片”层面的聚合，不直接展开所有 Task 明细。
        nodes = []
        completed = 0
        working = 0
        blocked = 0
        failed = 0
        superseded = 0
        total = 0

        for node_id, node_tasks in sorted(all_nodes.items()):
            counts = _node_task_status_counts(node_tasks)
            drilldown_task = _select_drilldown_task(node_tasks)
            total += counts["total"]
            working += counts["active"]
            completed += counts["completed"]
            blocked += counts["blocked"]
            failed += counts["failed"]
            superseded += counts["superseded"]
            status = "empty"
            if counts["total"] == 0 and counts["superseded"] > 0:
                # Every task on this node was replaced or abandoned — surface
                # it distinctly instead of masquerading as an empty node.
                status = "superseded"
            elif counts["failed"] > 0:
                status = "failed"
            elif counts["blocked"] > 0:
                status = "blocked"
            elif not (workflow or {}).get("configuration_error") and _node_delivery_complete(
                    node_tasks, (node_meta.get(node_id) or {}).get("required_task_ids")):
                status = "completed"
            elif counts["active"] > 0:
                status = "in_progress"
            elif counts["total"] > 0:
                status = "pending"

            node_summary = {
                "node_id": node_id,
                "node_label": (node_meta.get(node_id) or {}).get(
                    "label",
                    node_id,
                ),
                "total": counts["total"],
                "active": counts["active"],
                "completed": counts["completed"],
                "blocked": counts["blocked"],
                "failed": counts["failed"],
                "superseded": counts["superseded"],
                "status": status,
                "completion_issues": ([{"task_id": None, "reason": "workflow_config_invalid"}]
                    if (workflow or {}).get("configuration_error") else required_task_issues(
                    node_tasks, (node_meta.get(node_id) or {}).get("required_task_ids"),
                    all_tasks=inventory, workflow_id=workflow_id, node_id=node_id,
                )),
                "drilldown_task": drilldown_task.get("task_id") if drilldown_task else None,
            }
            nodes.append(node_summary)

        non_terminal = [
            task
            for task in wf_tasks
            if task.get("status") not in TERMINAL_STATUSES
        ]
        started = [
            _coerce_epoch(task.get("started_at"))
            or _coerce_epoch(task.get("created_at"))
            for task in non_terminal
            if _coerce_epoch(task.get("started_at"))
            or _coerce_epoch(task.get("created_at"))
        ]
        runtime = now - min(started) if started else None

        card = {
            "workflow_id": workflow_id,
            "workflow_label": workflow_label,
            "tasks": {
                "total": total,
                "active": working,
                "completed": completed,
                "blocked": blocked,
                "failed": failed,
                "superseded": superseded,
            },
            "runtime_seconds": round(runtime, 2) if runtime is not None else None,
            "runtime_bucket": _trajectory_bucket(runtime),
            "nodes": nodes,
        }
        # Startup surfaces (PR #359 ops labels) read the workflow record's
        # lifecycle fields off the card; task-count aggregation alone cannot
        # express startup_pending/startup_failed, and zero-task rows would
        # render as generic idle cards.
        if isinstance(workflow, dict):
            for key in ("status", "startup_ready", "outcome", "startup_failure_reason"):
                if workflow.get(key) is not None:
                    card[key] = workflow.get(key)
        cards.append(card)

    return cards

def _build_agent_fleet(tasks, now, runtime_probe=None):
    seen = set()
    for task in tasks:
        agent = task.get("agent")
        if isinstance(agent, str) and agent.strip():
            seen.add(agent.strip())

    all_agents = sorted(set(KNOWN_AGENTS + [agent for agent in seen if agent]))
    task_by_agent = {}
    for task in tasks:
        agent = task.get("agent")
        if not agent:
            continue
        task_by_agent.setdefault(agent, []).append(task)

    fleet = []
    runtime_cache = {}
    for agent in all_agents:
        agent_tasks = task_by_agent.get(agent, [])
        active_tasks = [
            task
            for task in agent_tasks
            if task.get("status") not in TERMINAL_STATUSES
            and task.get("status") != "superseded"
        ]

        load = len(active_tasks)
        current_task = None
        for task in active_tasks:
            last = (
                _coerce_epoch(task.get("last_activity_at"))
                or _coerce_epoch(task.get("updated_at"))
                or _coerce_epoch(task.get("created_at"))
                or 0
            )
            candidate = task
            if current_task is None:
                current_task = candidate
                continue
            candidate_last = (
                _coerce_epoch(candidate.get("last_activity_at"))
                or _coerce_epoch(candidate.get("updated_at"))
                or _coerce_epoch(candidate.get("created_at"))
                or 0
            )
            current_last = (
                _coerce_epoch(current_task.get("last_activity_at"))
                or _coerce_epoch(current_task.get("updated_at"))
                or _coerce_epoch(current_task.get("created_at"))
                or 0
            )
            if candidate_last > current_last:
                current_task = candidate

        pane_id = current_task.get("pane_id") if current_task else None
        if pane_id and pane_id in runtime_cache:
            runtime_status = runtime_cache[pane_id]
        elif pane_id:
            runtime_status = (runtime_probe or _agent_runtime_status)(pane_id)
            runtime_cache[pane_id] = runtime_status
        else:
            runtime_status = None

        current_task_status = current_task.get("status") if current_task else None
        current_task_id = current_task.get("task_id") if current_task else None
        last_activity = (
            _coerce_epoch(current_task.get("last_activity_at"))
            if current_task
            else None
        )
        if last_activity is None and current_task is not None:
            last_activity = (
                _coerce_epoch(current_task.get("updated_at"))
                or _coerce_epoch(current_task.get("created_at"))
            )

        started = None
        if current_task:
            started = _coerce_epoch(current_task.get("started_at")) or _coerce_epoch(
                current_task.get("created_at")
            )

        health = _task_healthy_status(current_task_status, runtime_status, load)
        is_stale = False
        if current_task_status in ACTIVE_TASK_STATUSES and runtime_status in {"idle", "unknown", None}:
            is_stale = True

        latest_result = None
        finished_candidates = [
            task for task in agent_tasks
            if task.get("status") in COMPLETED_TASK_STATUSES or task.get("status") == "failed"
        ]
        if finished_candidates:
            finished_candidates.sort(
                key=lambda item: (
                    _coerce_epoch(item.get("updated_at"))
                    or _coerce_epoch(item.get("last_activity_at"))
                    or 0
                )
            )
            latest_result = finished_candidates[-1].get("status")

        fleet.append(
            {
                "agent": agent,
                "health": health,
                "task_status": current_task_status,
                "current_task": current_task_id,
                "load": load,
                "runtime_seconds": _elapsed_seconds(started, now) if started else None,
                "runtime_bucket": _trajectory_bucket(
                    _elapsed_seconds(started, now) if started else None
                ),
                "last_result": latest_result or "-",
                "runtime_status": runtime_status,
                "last_event_seconds": (now - last_activity) if last_activity else None,
                "last_event": _format_age((now - last_activity) if last_activity else None),
                "stale": is_stale,
                "drilldown": {
                    "open_pane": f"herdr pane focus {pane_id}" if pane_id else None,
                },
            }
        )

    return sorted(
        fleet,
        key=lambda item: (-item["load"], item["agent"]),
    )

def _build_anomalies(tasks, now, runtime_probe=None):
    anomalies = []
    runtime_cache = {}
    for task in tasks:
        if task.get("status") == "superseded":
            continue

        pane_id = task.get("pane_id")
        status = task.get("status")
        if (
            pane_id
            and (status in ACTIVE_TASK_STATUSES or status in ("dispatched", "working", "agent_done", "rework", "blocked"))
        ):
            if pane_id in runtime_cache:
                runtime_status = runtime_cache[pane_id]
            else:
                runtime_status = (runtime_probe or _agent_runtime_status)(pane_id)
                runtime_cache[pane_id] = runtime_status
        else:
            runtime_status = None

        kinds = _classify_task_anomalies(task, runtime_status, now)
        if not kinds:
            continue

        for kind in kinds:
            suggestion = _suggested_actions_for_anomaly(kind, task)
            last_activity = (
                _coerce_epoch(task.get("last_activity_at"))
                or _coerce_epoch(task.get("updated_at"))
                or _coerce_epoch(task.get("created_at"))
            )
            anomalies.append(
                {
                    "kind": kind,
                    "workflow_id": task.get("workflow_id"),
                    "task_id": task.get("task_id"),
                    "agent": task.get("agent"),
                    "node": task.get("node") or task.get("stage"),
                    "pane_id": task.get("pane_id"),
                    "version": task.get("version"),
                    "last_event_seconds": (now - last_activity) if last_activity else None,
                    "last_event": _format_age((now - last_activity) if last_activity else None),
                    "actions": suggestion["actions"],
                    "links": suggestion["links"],
                }
            )

    return anomalies

def _build_boss_summary(tasks, workflows, anomalies):
    running_workflows = 0
    for workflow_id in workflows:
        wf_tasks = workflows.get(workflow_id, [])
        if any(
            task.get("status") in ACTIVE_TASK_STATUSES or task.get("status") in ("pending", "dispatched")
            for task in wf_tasks
            if task.get("status") != "superseded"
        ):
            running_workflows += 1

    working_agents = set()
    for task in tasks:
        if (
            task.get("status") in ACTIVE_TASK_STATUSES
            and task.get("status") != "superseded"
        ):
            working_agents.add(task.get("agent"))

    return {
        "working_agents": len([a for a in working_agents if a]),
        "running_workflows": running_workflows,
        "blocked_workflows": [
            workflow_id
            for workflow_id in sorted(workflows)
            if any(
                (task.get("status") == "blocked" or task.get("finalize_escalated"))
                for task in workflows.get(workflow_id, [])
                if task.get("status") != "superseded" and not task.get("superseded_by")
            )
        ],
        "attention_required": len(anomalies),
    }

def _ops_center_payload(workflow_id=None, now=None, include_tasks=False, *,
                        snapshot_tasks=None, workflow_lookup=None, runtime_probe=None,
                        registered_workflow_ids=None):
    now = _now() if now is None else float(now)
    all_tasks = snapshot_tasks or []
    if workflow_id:
        workflow_ids = {workflow_id}
        tasks = [task for task in all_tasks if task.get("workflow_id") == workflow_id]
    else:
        tasks = all_tasks
        # Cards must also cover registered zero-task workflows: pending startups
        # never spawn tasks, and startup-failure convergence explicitly requires
        # zero tasks, so deriving IDs from tasks alone hides exactly the rows the
        # startup labels exist for.
        workflow_ids = {task.get("workflow_id") for task in tasks if task.get("workflow_id")}
        workflow_ids.update(str(wid) for wid in (registered_workflow_ids or ()))

    # 确保当前查询范围中至少有工作流分组，避免前端无法渲染卡片。
    if workflow_id and workflow_id not in workflow_ids:
        workflow_ids = {workflow_id}

    workflows = {wf_id: [] for wf_id in workflow_ids}
    for task in tasks:
        wf = task.get("workflow_id")
        if wf in workflows:
            workflows[wf].append(task)

    workflow_cards = _build_workflow_cards(
        workflows, now, all_tasks=all_tasks, workflow_lookup=workflow_lookup)
    # One request-local memo serves fleet and anomalies alike.
    runtime_cache = {}
    probe = runtime_probe or _agent_runtime_status
    def runtime_once(pane_id):
        if pane_id not in runtime_cache:
            runtime_cache[pane_id] = probe(pane_id)
        return runtime_cache[pane_id]
    agent_fleet = _build_agent_fleet(tasks, now, runtime_probe=runtime_once)
    anomalies = _build_anomalies(tasks, now, runtime_probe=runtime_once)
    boss = _build_boss_summary(tasks, workflows, anomalies)
    task_summaries = _task_summaries_for_ops(tasks, include_detail=include_tasks, now=now)

    payload = {
        "generated_at": int(now),
        "scope": workflow_id or "all",
        "boss": boss,
        "workflow_cards": workflow_cards,
        "agent_fleet": agent_fleet,
        "anomalies": anomalies,
    }

    if include_tasks:
        payload["task_summaries"] = task_summaries
        payload["count"] = len(task_summaries)
    else:
        payload["task_summaries"] = [
            {
                "task_id": item["task_id"],
                "agent": item["agent"],
                "status": item["status"],
                "node": item["node"],
                "elapsed": item["elapsed"],
                "last_event": _format_age(
                    now
                    - (
                        item["last_activity_at"]
                        if item["last_activity_at"] is not None
                        else now
                    )
                ),
            }
            for item in task_summaries
        ]
        payload["count"] = len(task_summaries)

    return payload
