#!/usr/bin/env python3
"""Herdr Workflow Graph Projection (Functional Core).

Pure logic: Workflow Definition + Tasks + Controller facts -> Graph.
No I/O, no DOM, no scheduler mutation. Reused by console / API / future UIs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from .node_capacity import node_usage

COMPLETED_LIKE = {"completed", "committed", "integrated", "cleanup_ready", "cleaned"}
WORKING_LIKE = {"working", "dispatched", "pending", "agent_done"}


def _live_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        if t.get("status") == "superseded" or t.get("superseded_by"):
            continue
        out.append(t)
    return out


def _node_key(task: Dict[str, Any]) -> str:
    return str(task.get("node") or task.get("stage") or "")


def aggregate_node_status(node_tasks: List[Dict[str, Any]]) -> str:
    """Deterministic priority: blocked > failed > rework > working > completed > waiting."""
    live = _live_tasks(node_tasks)
    if not live:
        return "waiting"
    statuses = [str(t.get("status") or "unknown") for t in live]
    verdicts = [str(t.get("stage_verdict") or "") for t in live]

    # Blocked check: blocked status or verdict, unless passed
    if any((s == "blocked" or v == "blocked") and v != "pass" for s, v in zip(statuses, verdicts)):
        return "blocked"
    if any(s == "rework" for s in statuses):
        return "rework"
    if any(s in WORKING_LIKE for s in statuses):
        return "working"
    if all(s in COMPLETED_LIKE or v == "pass" for s, v in zip(statuses, verdicts)):
        return "completed"

    # Terminal tasks without active work: inspect latest task
    def _task_sort_key(t: Dict[str, Any]) -> float:
        for k in ("created_at", "updated_at", "last_activity_at"):
            val = t.get(k)
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str) and val.replace(".", "", 1).isdigit():
                return float(val)
        return 0.0

    sorted_tasks = sorted(live, key=_task_sort_key)
    latest = sorted_tasks[-1]
    latest_st = str(latest.get("status") or "")
    latest_v = str(latest.get("stage_verdict") or "")

    unexempted_failed = any(s == "failed" and v != "pass" for s, v in zip(statuses, verdicts))
    if unexempted_failed:
        return "failed"

    if latest_st in COMPLETED_LIKE:
        return "completed"
    if latest_st == "failed" or any(s == "failed" for s in statuses):
        return "failed"
    return "working"


def is_gate_override_valid(
    gate_override: Optional[Dict[str, Any]],
    live_tasks: List[Dict[str, Any]],
) -> bool:
    """Determine whether a stored gate override is currently applicable to the live tasks.

    Separates historical audit records from active applicability.
    A gate override is INVALID if:
    1. Not a dict or verdict != "pass".
    2. Single-task override (task_id specified):
       - Target task is not in live_tasks (superseded or removed).
       - Target task has status in {'blocked', 'rework'}.
       - Target task has stage_verdict in {'blocked', 'failed'} or stage_verdict != 'pass'.
       - Target task's version != snapshot version (re-run/updated after pass).
       - Any other live task on the node is blocked or failed (unless that other task has verdict == 'pass').
    3. Node-level override (no task_id):
       - Live task set != snapshot task_ids set (tasks added or removed since pass).
       - Any live task has stage_verdict in {'blocked', 'failed'} and stage_verdict != 'pass'.
       - Any live task has status in {'blocked', 'rework'}.
       - Any live task version != snapshot version (re-run/updated after pass).
    """
    if not isinstance(gate_override, dict) or gate_override.get("verdict") != "pass":
        return False

    override_tid = gate_override.get("task_id")
    task_versions = gate_override.get("task_versions")
    if not isinstance(task_versions, dict):
        task_versions = {}

    snapshot_task_ids = gate_override.get("task_ids")
    if snapshot_task_ids is None and task_versions:
        snapshot_task_ids = sorted(list(task_versions.keys()))

    live_by_id = {str(t.get("task_id")): t for t in live_tasks if t.get("task_id")}
    live_ids = set(live_by_id.keys())

    if override_tid:
        target_tid = str(override_tid)
        if target_tid not in live_by_id:
            return False

        target_task = live_by_id[target_tid]
        target_st = str(target_task.get("status") or "")
        target_v = str(target_task.get("stage_verdict") or "")

        if target_st in {"blocked", "rework"}:
            return False
        if target_v in {"blocked", "failed"} or target_v != "pass":
            return False

        expected_v = task_versions.get(target_tid)
        if expected_v is None:
            expected_v = gate_override.get("task_version")
        if expected_v is not None and target_task.get("version") != expected_v:
            return False

        for tid, t in live_by_id.items():
            if tid == target_tid:
                continue
            other_st = str(t.get("status") or "")
            other_v = str(t.get("stage_verdict") or "")
            if other_st in {"blocked", "failed", "rework"}:
                return False
            if other_v in {"blocked", "failed"} and other_v != "pass":
                return False

        return True
    else:
        if snapshot_task_ids is not None:
            if live_ids != set(str(tid) for tid in snapshot_task_ids):
                return False

        for tid, t in live_by_id.items():
            st = str(t.get("status") or "")
            v = str(t.get("stage_verdict") or "")
            if st in {"blocked", "rework"}:
                return False
            if v in {"blocked", "failed"} and v != "pass":
                return False
            if tid in task_versions:
                if t.get("version") != task_versions[tid]:
                    return False
                if v != "pass":
                    return False

        return True


def _normalize_definition(workflow: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(workflow, dict) or not workflow:
        return {"nodes": [], "stages": []}
    try:
        from herdr.workflow import normalize_workflow as _norm

        return _norm(dict(workflow))
    except Exception:
        # Fail-soft: keep only what we can prove, never invent edges.
        nodes = workflow.get("nodes") if isinstance(workflow.get("nodes"), list) else []
        clean = []
        for n in nodes:
            if isinstance(n, dict) and (n.get("id") or n.get("key")):
                nid = str(n.get("id") or n.get("key"))
                clean.append(
                    {
                        "id": nid,
                        "label": str(n.get("label") or nid),
                        "node_type": str(n.get("node_type") or "agent"),
                        "depends_on": list(n.get("depends_on") or []),
                        "purpose": str(n.get("purpose") or ""),
                    }
                )
        if clean:
            return {"nodes": clean, "stages": [], "configuration_error":"workflow_config_invalid", "context": workflow.get("context") if isinstance(workflow.get("context"), dict) else None}
        stages = workflow.get("stages") if isinstance(workflow.get("stages"), list) else []
        if stages:
            clean_nodes = []
            prev = None
            for idx, s in enumerate(stages):
                if isinstance(s, str):
                    nid, label = s, s
                elif isinstance(s, dict):
                    nid = str(s.get("key") or s.get("id") or f"node_{idx+1}")
                    label = str(s.get("label") or nid)
                else:
                    continue
                deps = [prev] if prev else []
                clean_nodes.append({"id": nid, "label": label, "node_type": "agent", "depends_on": deps, "purpose": ""})
                prev = nid
            return {"nodes": clean_nodes, "stages": []}
        return {"nodes": [], "stages": []}


def _context_ids(workflow_norm: Dict[str, Any]) -> Dict[str, List[str]]:
    ctx = workflow_norm.get("context") or {}
    if not isinstance(ctx, dict):
        return {"required": [], "optional": []}

    def _ids(key: str) -> List[str]:
        out = []
        for e in ctx.get(key) or []:
            if isinstance(e, dict) and e.get("id"):
                out.append(str(e["id"]))
            elif isinstance(e, str) and e.strip():
                out.append(e.strip())
        return out

    return {"required": _ids("required"), "optional": _ids("optional")}


def workflow_graph_projection(
    workflow: Optional[Dict[str, Any]],
    tasks: Optional[List[Dict[str, Any]]],
    blockers: Optional[List[Dict[str, Any]]] = None,
    *, all_tasks=None,
) -> Dict[str, Any]:
    """Project workflow definition + tasks into nodes/edges for canvas rendering."""
    from .scheduler import required_task_issues, node_is_complete
    norm = _normalize_definition(workflow)
    workflow_id = (workflow or {}).get("workflow_id")
    owned = [t for t in (tasks or []) if workflow_id is None or t.get("workflow_id") == workflow_id]
    nodes_def = norm.get("nodes") or []
    node_ids = {str(n.get("id")) for n in nodes_def if n.get("id")}

    wf_status = str((workflow or {}).get("status") or "") if isinstance(workflow, dict) else ""
    is_wf_completed = wf_status in {"completed", "cleaned", "archived"}
    gate_overrides = (workflow or {}).get("gate_overrides") if isinstance(workflow, dict) and isinstance((workflow or {}).get("gate_overrides"), dict) else {}

    # Group live tasks by node; unknown-node tasks are ignored (never invent nodes).
    by_node: Dict[str, List[Dict[str, Any]]] = {nid: [] for nid in node_ids}
    for t in _live_tasks(owned):
        key = _node_key(t)
        if key in by_node:
            # keep original record for counts but ensure live filtering already done
            by_node[key].append(t)

    blocker_task_ids = set()
    for b in blockers or []:
        if isinstance(b, dict) and b.get("task_id"):
            blocker_task_ids.add(str(b["task_id"]))

    nodes: List[Dict[str, Any]] = []
    for n in nodes_def:
        nid = str(n.get("id"))
        label = str(n.get("label") or nid)
        node_type = str(n.get("node_type") or "agent")
        depends_on = [str(d) for d in (n.get("depends_on") or []) if str(d) in node_ids]
        purpose = str(n.get("purpose") or "")
        nts = by_node.get(nid, [])
        live = _live_tasks(nts)
        agents = sorted({str(t.get("agent")) for t in live if t.get("agent")})
        task_ids = [str(t.get("task_id")) for t in live if t.get("task_id")]
        completed = sum(1 for t in live if str(t.get("status")) in COMPLETED_LIKE)
        failed = sum(1 for t in live if str(t.get("status")) == "failed")
        blocked = sum(1 for t in live if str(t.get("status")) == "blocked" or str(t.get("stage_verdict") or "") == "blocked")
        active = sum(1 for t in live if str(t.get("status")) in WORKING_LIKE or str(t.get("status")) == "rework")

        gate_override = gate_overrides.get(nid) or {}
        gate_passed = is_gate_override_valid(gate_override, live)

        if is_wf_completed:
            status = "completed"
            active_count = 0
            has_attention = False
        elif gate_passed:
            if active > 0:
                status = "working"
                active_count = max(0, active)
                has_attention = any(tid in blocker_task_ids for tid in task_ids)
            else:
                status = "completed"
                active_count = 0
                has_attention = False
        else:
            status = aggregate_node_status(live)
            active_count = max(0, active)
            has_attention = status in {"blocked", "failed", "rework"} or any(tid in blocker_task_ids for tid in task_ids)

        lineage = [t for t in owned if _node_key(t) == nid]
        issues = required_task_issues(lineage, n.get("required_task_ids"),
            all_tasks=all_tasks if all_tasks is not None else tasks or [],
            workflow_id=workflow_id, node_id=nid)
        if norm.get("configuration_error") or (workflow or {}).get("configuration_error"):
            issues = [{"task_id":None,"reason":"workflow_config_invalid"}] + issues
        if issues or ("required_task_ids" in n and status == "completed" and not node_is_complete(lineage,n["required_task_ids"])):
            if not issues:
                issues = [{"task_id":None,"reason":"required_task_incomplete"}]
            status = "blocked"
            has_attention = True

        nodes.append(
            {
                "id": nid,
                "label": label,
                "node_type": node_type,
                "depends_on": depends_on,
                "purpose": purpose,
                "status": status,
                "resource_usage": node_usage(n, tasks or [], (workflow or {}).get("workflow_id")),
                "task_count": len(live),
                "active_task_count": active_count,
                "completed_task_count": completed,
                "failed_task_count": failed,
                "blocked_task_count": blocked,
                "agents": agents,
                "task_ids": task_ids,
                "has_attention": bool(has_attention),
                "completion_issues": issues,
            }
        )

    # Downstream + edges purely from depends_on
    downstream: Dict[str, List[str]] = {n["id"]: [] for n in nodes}
    edges: List[Dict[str, str]] = []
    for n in nodes:
        for dep in n["depends_on"]:
            if dep in downstream:
                downstream[dep].append(n["id"])
                edges.append({"from": dep, "to": n["id"]})
    for n in nodes:
        n["downstream"] = downstream.get(n["id"], [])

    current = [n["id"] for n in nodes if n["status"] in {"working","rework"}]
    completed_nodes = {n["id"] for n in nodes if n["status"] == "completed"}
    ready = [n["id"] for n in nodes if n["status"] == "waiting" and all(d in completed_nodes for d in n["depends_on"])]
    frontier = current or ready
    return {"nodes": nodes, "edges": edges, "context": _context_ids(norm),
            "current_nodes":current,"ready_nodes":ready,
            "current_stage":frontier[0] if len(frontier)==1 else "",
            "current_stage_source":"derived_nodes"}


def pick_default_node(projection: Dict[str, Any]) -> Optional[str]:
    """Deterministic inspector default: blocked > failed > working > rework > waiting-next > first."""
    nodes = projection.get("nodes") or []
    if not nodes:
        return None
    by_id = {n["id"]: n for n in nodes}
    completed = {n["id"] for n in nodes if n.get("status") == "completed"}

    def _first_with(status: str) -> Optional[str]:
        for n in nodes:
            if n.get("status") == status:
                return str(n["id"])
        return None

    for s in ("blocked", "failed", "working", "rework"):
        hit = _first_with(s)
        if hit:
            return hit
    # waiting-next: earliest waiting whose deps are all completed
    for n in nodes:
        if n.get("status") == "waiting":
            deps = n.get("depends_on") or []
            if all(d in completed for d in deps):
                return str(n["id"])
    for n in nodes:
        if n.get("status") == "waiting":
            return str(n["id"])
    return str(nodes[0]["id"]) if nodes else None
