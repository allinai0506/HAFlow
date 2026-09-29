#!/usr/bin/env python3
"""Herdr Workflow Graph Projection (Functional Core).

Pure logic: Workflow Definition + Tasks + Controller facts -> Graph.
No I/O, no DOM, no scheduler mutation. Reused by console / API / future UIs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
    if any(s == "blocked" for s in statuses) or any(v == "blocked" for v in verdicts):
        return "blocked"
    if any(s == "failed" for s in statuses):
        return "failed"
    if any(s == "rework" for s in statuses):
        return "rework"
    if any(s in WORKING_LIKE or s == "rework" for s in statuses):
        return "working"
    if all(s in COMPLETED_LIKE for s in statuses):
        return "completed"
    # Unknown/mixed with no active signal: surface as working to draw attention,
    # except pure unknown -> waiting is already handled; fallback working.
    return "working"


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
            return {"nodes": clean, "stages": [], "context": workflow.get("context") if isinstance(workflow.get("context"), dict) else None}
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
) -> Dict[str, Any]:
    """Project workflow definition + tasks into nodes/edges for canvas rendering."""
    norm = _normalize_definition(workflow)
    nodes_def = norm.get("nodes") or []
    node_ids = {str(n.get("id")) for n in nodes_def if n.get("id")}

    # Group live tasks by node; unknown-node tasks are ignored (never invent nodes).
    by_node: Dict[str, List[Dict[str, Any]]] = {nid: [] for nid in node_ids}
    for t in _live_tasks(tasks or []):
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
        status = aggregate_node_status(live)
        agents = sorted({str(t.get("agent")) for t in live if t.get("agent")})
        task_ids = [str(t.get("task_id")) for t in live if t.get("task_id")]
        completed = sum(1 for t in live if str(t.get("status")) in COMPLETED_LIKE)
        failed = sum(1 for t in live if str(t.get("status")) == "failed")
        blocked = sum(1 for t in live if str(t.get("status")) == "blocked" or str(t.get("stage_verdict") or "") == "blocked")
        active = len(live) - completed
        has_attention = status in {"blocked", "failed", "rework"} or any(tid in blocker_task_ids for tid in task_ids)
        nodes.append(
            {
                "id": nid,
                "label": label,
                "node_type": node_type,
                "depends_on": depends_on,
                "purpose": purpose,
                "status": status,
                "task_count": len(live),
                "active_task_count": max(0, active),
                "completed_task_count": completed,
                "failed_task_count": failed,
                "blocked_task_count": blocked,
                "agents": agents,
                "task_ids": task_ids,
                "has_attention": bool(has_attention),
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

    return {"nodes": nodes, "edges": edges, "context": _context_ids(norm)}


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
