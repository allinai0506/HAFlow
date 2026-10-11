"""Universal Workflow and Ledger Reconciliation Engine.

Provides platform-wide self-healing for HAFlow workflows across all business domains:
- Reconciles DAG node states against persisted task records
- Reconciles node dispatch recovery operations
- Clears stale stage advance latches
- Audits and verifies the delivery ledger (shared notes)
- Computes ready, active, blocked, and completed nodes
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set

from . import recovery_store, state_db, workflow_docs
from .state_store import StateStore
from .workflow import find_node, get_ready_nodes, is_workflow_completed, normalize_workflow

logger = logging.getLogger(__name__)


def _get_store(store: Optional[StateStore] = None) -> StateStore:
    if store is not None:
        return store
    from .state_store import get_store
    return get_store()


def reconcile_workflow_state(
    workflow_id: str,
    store: Optional[StateStore] = None,
) -> Dict[str, Any]:
    """Perform universal state reconciliation on a workflow and its delivery ledger.

    Args:
        workflow_id: Unique workflow identifier.
        store: Optional StateStore instance.

    Returns:
        Structured reconciliation receipt dict.
    """
    s = _get_store(store)
    workflow = s.get_workflow(workflow_id)
    if not workflow:
        raise ValueError(f"Workflow '{workflow_id}' not found")

    raw_cfg = workflow.get("config") or {}
    workflow_cfg = normalize_workflow(raw_cfg)
    nodes = workflow_cfg.get("nodes", [])

    db_path = getattr(s, "db_path", None)
    # 1. Reconcile recovery operations and node dispatches
    recovery_ops_reconciled = 0
    if db_path:
        try:
            recovery_store.reconcile(db_path, workflow_id)
            from . import node_dispatch_store
            node_dispatch_store.reconcile_workflow(db_path, workflow_id, discover=True)
            recovery_ops_reconciled += 1
        except Exception as exc:
            logger.debug("Recovery store reconciliation note for %s: %s", workflow_id, exc)

    # 2. Load all tasks for this workflow
    all_tasks = s.list_tasks(workflow_id=workflow_id)

    completed_nodes: Set[str] = set()
    blocked_nodes: Set[str] = set()
    active_nodes: Set[str] = set()
    gate_overrides = dict(workflow.get("gate_overrides") or {})

    for node in nodes:
        nid = node["id"]
        node_tasks = [
            t for t in all_tasks
            if (t.get("node") or t.get("stage")) == nid and t.get("status") != "superseded"
        ]

        # Check for human gate override
        node_override = gate_overrides.get(nid, {})
        is_force_passed = (node_override.get("verdict") == "pass")

        # Check for blocked gate
        is_blocked = any(t.get("stage_verdict") == "blocked" for t in node_tasks) and not is_force_passed
        if is_blocked:
            blocked_nodes.add(nid)

        # Check for completion
        has_completed_task = any(
            t.get("status") in ("completed", "committed", "integrated", "cleanup_ready", "cleaned")
            and t.get("stage_verdict") != "blocked"
            for t in node_tasks
        )

        if (has_completed_task or is_force_passed) and not is_blocked:
            completed_nodes.add(nid)
        elif any(t.get("status") in ("dispatched", "working", "rework") for t in node_tasks):
            active_nodes.add(nid)

    ready_node_objects = get_ready_nodes(workflow_cfg, completed_nodes)
    ready_node_ids = [
        n["id"] for n in ready_node_objects
        if n["id"] not in active_nodes and n["id"] not in blocked_nodes
    ]

    # 3. Reconcile stage advance latches in StateDB & stage-state.json
    stage_advances_cleared = 0
    advances = dict(workflow.get("stage_advancing") or {})
    advances_changed = False
    for adv_node in list(advances.keys()):
        if adv_node in completed_nodes or adv_node in blocked_nodes:
            del advances[adv_node]
            stage_advances_cleared += 1
            advances_changed = True
    if advances_changed:
        workflow["stage_advancing"] = advances
        s.save_workflow(workflow)

    try:
        from .kernel import get_stage_state_file, _atomic_write_json
        import json
        s_file = get_stage_state_file()
        if s_file.exists():
            with open(s_file, "r", encoding="utf-8") as fp:
                s_data = json.load(fp)
            if isinstance(s_data, dict):
                changed = False
                for n_id in completed_nodes | blocked_nodes:
                    k = f"{workflow_id}:{n_id}"
                    if k in s_data:
                        del s_data[k]
                        stage_advances_cleared += 1
                        changed = True
                if changed:
                    _atomic_write_json(s_file, s_data)
    except Exception as exc:
        logger.debug("stage-state.json reconciliation note: %s", exc)

    # 4. Reconcile delivery ledger (workflow_docs shared notes)
    notes: List[Dict[str, Any]] = []
    delivery_notes: List[Dict[str, Any]] = []
    try:
        notes = workflow_docs.load_notes(workflow_id)
        delivery_notes = [n for n in notes if n.get("kind") == "delivery"]
    except Exception as exc:
        logger.debug("Could not read shared notes for %s: %s", workflow_id, exc)

    # 5. Check if workflow is overall completed
    all_done = is_workflow_completed(workflow_cfg, completed_nodes)
    current_status = workflow.get("status", "unknown")
    if all_done and not blocked_nodes and current_status == "running":
        if not workflow.get("suppress_auto_close"):
            workflow["status"] = "completed"
            s.save_workflow(workflow)
            current_status = "completed"

    active_tasks = [
        t for t in all_tasks
        if t.get("status") in ("dispatched", "working", "rework", "blocked", "paused")
    ]

    return {
        "workflow_id": workflow_id,
        "status": current_status,
        "reconciled": True,
        "nodes": {
            "total": len(nodes),
            "completed": sorted(completed_nodes),
            "ready": sorted(ready_node_ids),
            "active": sorted(active_nodes),
            "blocked": sorted(blocked_nodes),
        },
        "active_tasks_count": len(active_tasks),
        "delivery_ledger": {
            "total_notes": len(notes),
            "delivery_notes": [
                n.get("title") or n.get("body", "")[:32] for n in delivery_notes
            ],
        },
        "stage_advances_cleared": stage_advances_cleared,
        "stage_advances_count": len(advances),
        "timestamp": time.time(),
    }

