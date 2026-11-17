"""Universal Workflow Shutdown Protocol (P1 - ADR-0007 / OEP-0002).

Invariants:
1. Atomically sets workflow status to CLOSING before attempting teardown side effects.
2. In CLOSING state, node_dispatch.active returns False, rejecting new task dispatches.
3. Coordinates with in-flight launches via workflow_launch_lock and launch intent draining.
4. Refreshes active tasks snapshot to catch any task registered right before or during closing.
5. Safely halts/kills only verified owned live panes/agents; refuses to signal unproven panes.
6. Re-verifies zero active execution instances before terminal transition.
7. Atomically transitions workflow to terminal state (completed / abandoned).
8. Guarantees workspace occupancy is released only after all agents are confirmed stopped.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from herdr.kernel import state_db_path
from herdr.state_db import get_workflow, list_tasks, transition_task, transition_workflow
from herdr.task_resources import owned_live_pane, workflow_launch_lock
from herdr.transitions import TERMINAL_WORKFLOW_STATUSES

logger = logging.getLogger(__name__)

ACTIVE_TASK_STATUSES: Set[str] = {
    "working", "agent_done", "rework", "paused", "blocked", "dispatched", "pending"
}


def close_pane(pane_id: str) -> bool:
    """Safely close a tmux/herdr pane and return True if successful or already closed."""
    if not pane_id:
        return True
    try:
        res = subprocess.run(["herdr", "pane", "close", str(pane_id)], capture_output=True, timeout=5)
        if res.returncode == 0:
            return True
    except Exception:
        pass
    try:
        res = subprocess.run(["tmux", "kill-pane", "-t", str(pane_id)], capture_output=True, timeout=5)
        err = (res.stderr or b"").decode("utf-8", errors="replace").lower()
        if res.returncode == 0 or "can't find pane" in err or "no pane" in err:
            return True
        logger.warning("Failed to close pane %s: returncode=%s, stderr=%s", pane_id, res.returncode, err.strip())
        return False
    except Exception as exc:
        logger.warning("Exception closing pane %s: %s", pane_id, exc)
        return False


def drain_unsettled_launch_intents(
    workflow_id: str,
    db_path: Optional[str | Path] = None,
    reason: str = "workflow shutdown",
) -> None:
    """Abort any allocating or unsettled launch intents for the workflow (P1)."""
    try:
        from herdr.task_resources import _launch_event
        from herdr.state_store import SQLiteStateStore
        resolved_db = Path(db_path).resolve() if db_path else Path(state_db_path()).resolve()
        if not resolved_db.exists():
            return
        conn = sqlite3.connect(str(resolved_db))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                """
                SELECT payload_json FROM events
                WHERE workflow_id = ? AND event_type = 'launch_intent'
                ORDER BY id DESC
                """,
                (workflow_id,),
            )
            rows = cur.fetchall()
            seen_keys = set()
            store = SQLiteStateStore(str(resolved_db))
            now = time.time()
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"] or "{}")
                    key = payload.get("key")
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    if payload.get("phase") == "allocating":
                        rolled_back = {**payload, "phase": "resources_absent", "abort_reason": reason}
                        _launch_event(store, rolled_back, now=now)
                except Exception as parse_err:
                    logger.warning("Error parsing launch_intent event: %s", parse_err)
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Error draining launch intents for %s: %s", workflow_id, exc)


def shutdown_workflow(
    workflow_id: Optional[str],
    *,
    reason: str = "workflow shutdown requested",
    outcome: str = "abandoned",
    source: str = "workflow_shutdown",
    db_path: Optional[str | Path] = None,
    timeout: float = 10.0,
) -> str:
    """Execute the canonical 8-step Workflow Shutdown Protocol (P1 - ADR-0007 / OEP-0002).

    Returns:
    - "STOPPED": All tasks confirmed stopped, workflow transitioned to terminal.
    - "ALREADY_TERMINAL": Workflow was already in terminal state with no active tasks.
    - "STOP_FAILED": One or more tasks could not be confirmed stopped, workflow left in closing state.
    """
    if not workflow_id:
        return "ALREADY_TERMINAL"

    try:
        resolved_db = Path(db_path).resolve() if db_path else Path(state_db_path()).resolve()
        wf = get_workflow(workflow_id, db_path=resolved_db)
        if not wf:
            return "ALREADY_TERMINAL"

        cur_status = str(wf.get("status") or "").lower()

        # Step 0: Check if already terminal with no active tasks
        if cur_status in TERMINAL_WORKFLOW_STATUSES:
            try:
                all_tasks = list_tasks(workflow_id=workflow_id, db_path=resolved_db)
                active_tasks = [t for t in all_tasks if str(t.get("status") or "").lower() in ACTIVE_TASK_STATUSES]
                if not active_tasks:
                    return "ALREADY_TERMINAL"
            except Exception as task_err:
                logger.warning("Error listing tasks for terminal workflow %s: %s", workflow_id, task_err)
                return "STOP_FAILED"

        # Step 1: Atomically transition workflow to CLOSING before any teardown
        if cur_status != "closing":
            try:
                transition_workflow(
                    workflow_id=workflow_id,
                    to_status="closing",
                    reason=reason,
                    source=source,
                    force=True,
                    db_path=resolved_db,
                )
            except Exception as tr_err:
                logger.error("Failed to transition workflow %s to closing: %s", workflow_id, tr_err)
                return "STOP_FAILED"

        # Step 2 & 3: Coordinate with in-flight launch operations under workflow_launch_lock
        with workflow_launch_lock(resolved_db, workflow_id, timeout=timeout):
            drain_unsettled_launch_intents(workflow_id, db_path=resolved_db, reason=reason)

            # Step 4: Fresh snapshot of all active tasks after holding launch lock
            try:
                all_tasks = list_tasks(workflow_id=workflow_id, db_path=resolved_db)
                active_tasks = [t for t in all_tasks if str(t.get("status") or "").lower() in ACTIVE_TASK_STATUSES]
            except Exception as task_err:
                logger.warning("Error listing tasks for closing workflow %s: %s", workflow_id, task_err)
                return "STOP_FAILED"

            # Step 5: Safely stop execution of all active tasks
            import sys
            from herdr import task_resources, steering
            disp_mod = sys.modules.get("herdr.inbox.dispatcher")
            close_fn = getattr(disp_mod, "close_pane", close_pane)

            for t in active_tasks:
                tid = t.get("task_id")
                if not tid:
                    continue
                pane_id = t.get("pane_id")
                task_stopped = False

                if pane_id:
                    try:
                        owned, probe_reason = task_resources.owned_live_pane(t)
                        if owned:
                            # 1. Graceful halt via steering
                            try:
                                res = steering.halt_task(tid, reason=reason, operator="system", execute_kill=True)
                                if isinstance(res, dict) and res.get("ok"):
                                    task_stopped = True
                                    try:
                                        from herdr.state_db import get_task
                                        cur_t = get_task(tid, db_path=resolved_db)
                                        if cur_t and str(cur_t.get("status") or "").lower() in ACTIVE_TASK_STATUSES:
                                            transition_task(
                                                task_id=tid,
                                                to_status="failed",
                                                reason=reason,
                                                source=source,
                                                db_path=resolved_db,
                                                force=True,
                                            )
                                    except Exception as tr_err:
                                        logger.warning("Error transitioning halted task %s: %s", tid, tr_err)
                            except Exception as halt_err:
                                logger.warning("Error halting task %s: %s", tid, halt_err)

                            # 2. If halt failed, close the verified owned pane
                            if not task_stopped:
                                if close_fn(pane_id):
                                    task_stopped = True
                                    try:
                                        transition_task(
                                            task_id=tid,
                                            to_status="failed",
                                            reason=reason,
                                            source=source,
                                            db_path=resolved_db,
                                            force=True,
                                        )
                                    except Exception as tr_err:
                                        logger.warning("Error transitioning closed task %s: %s", tid, tr_err)
                                else:
                                    logger.warning("Failed to close owned pane %s for task %s", pane_id, tid)
                        elif probe_reason == "pane_missing":
                            task_stopped = True
                            try:
                                transition_task(
                                    task_id=tid,
                                    to_status="failed",
                                    reason=reason,
                                    source=source,
                                    db_path=resolved_db,
                                    force=True,
                                )
                            except Exception as tr_err:
                                logger.warning("Error transitioning missing pane task %s: %s", tid, tr_err)
                        else:
                            # Runtime ownership not proven (e.g. session_mismatch) -> Invariant 11: REFUSE to signal
                            logger.warning(
                                "Runtime ownership for task %s pane %s not proven (%s); "
                                "refusing to send halt signal or close pane to protect other agents",
                                tid,
                                pane_id,
                                probe_reason,
                            )
                    except Exception as pane_err:
                        logger.warning("Error verifying/stopping pane %s for task %s: %s", pane_id, tid, pane_err)
                else:
                    # No physical pane was attached
                    try:
                        transition_task(
                            task_id=tid,
                            to_status="failed",
                            reason=reason,
                            source=source,
                            db_path=resolved_db,
                            force=True,
                        )
                        task_stopped = True
                    except Exception as tr_err:
                        logger.warning("Error transitioning unassigned task %s: %s", tid, tr_err)

                if not task_stopped:
                    logger.error(
                        "Cannot confirm task %s (pane=%s) was stopped; refusing to mark workflow stopped",
                        tid,
                        pane_id,
                    )
                    return "STOP_FAILED"

            # Step 6: Re-verify zero active execution instances remain
            try:
                verify_tasks = list_tasks(workflow_id=workflow_id, db_path=resolved_db)
                still_active = [t for t in verify_tasks if str(t.get("status") or "").lower() in ACTIVE_TASK_STATUSES]
                if still_active:
                    logger.error(
                        "Active tasks still remain for workflow %s: %s",
                        workflow_id,
                        [t.get("task_id") for t in still_active],
                    )
                    return "STOP_FAILED"
            except Exception as task_err:
                logger.warning("Error re-verifying tasks for workflow %s: %s", workflow_id, task_err)
                return "STOP_FAILED"

        # Step 7: Atomically transition workflow to terminal state
        try:
            transition_workflow(
                workflow_id=workflow_id,
                to_status="completed",
                reason=reason,
                source=source,
                metadata={"outcome": outcome},
                force=True,
                db_path=resolved_db,
            )
        except Exception as tr_err:
            logger.error("Failed to transition closing workflow %s to completed: %s", workflow_id, tr_err)
            return "STOP_FAILED"

        return "STOPPED"

    except Exception as exc:
        logger.error("Unexpected error in shutdown_workflow for %s: %s", workflow_id, exc)
        return "STOP_FAILED"
