"""Inbox task dispatcher: atomic claiming, single-task workspace mutex, and auto-continuation."""

import concurrent.futures
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from herdr.inbox.store import InboxStore, _decode_row
from herdr.inbox.transitions import (
    ACTIVE_INBOX_STATUSES,
    WORKSPACE_HOLDING_STATUSES,
)
from herdr.projects import active_workflows_for_project, load_projects
from herdr.state.connection import get_db_connection

logger = logging.getLogger(__name__)

# Bounded launcher executor to avoid blocking the Controller main loop (Invariant 10)
_LAUNCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="inbox_launcher"
)
_LAUNCH_SEMAPHORE = threading.Semaphore(2)

ACTIVE_WORKFLOW_STATUSES = frozenset({
    "running",
    "pending",
    "in_progress",
    "paused",
    "closing",
})

TERMINAL_WORKFLOW_STATUSES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "abandoned",
    "halted",
})


def close_pane(pane_id: str) -> bool:
    """Safely close a tmux/herdr pane and return True if successful or already closed."""
    if not pane_id:
        return True
    try:
        import subprocess
        # Try herdr pane close first if available
        res = subprocess.run(["herdr", "pane", "close", str(pane_id)], capture_output=True, timeout=5)
        if res.returncode == 0:
            return True
    except Exception:
        pass
    try:
        import subprocess
        res = subprocess.run(["tmux", "kill-pane", "-t", str(pane_id)], capture_output=True, timeout=5)
        err = (res.stderr or b"").decode("utf-8", errors="replace").lower()
        if res.returncode == 0 or "can't find pane" in err or "no pane" in err:
            return True
        logger.warning("Failed to close pane %s: returncode=%s, stderr=%s", pane_id, res.returncode, err.strip())
        return False
    except Exception as exc:
        logger.warning("Exception closing pane %s: %s", pane_id, exc)
        return False


class InboxDispatcher:
    """Manages workspace single-task execution mutex and auto-dispatching."""

    def __init__(
        self,
        store: Optional[InboxStore] = None,
        db_path: Optional[Path] = None,
        worker_id: Optional[str] = None,
    ):
        self.db_path = db_path
        self.store = store or InboxStore(db_path=db_path)
        self.worker_id = worker_id or f"dispatcher_{os.getpid()}_{uuid.uuid4().hex[:6]}"

    def _conn(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def is_workspace_busy(self, workspace_id: str) -> Tuple[bool, Optional[str]]:
        """Check if workspace_id currently has an active task or running workflow.

        Workflows in terminal states (completed, failed, cancelled, abandoned, halted)
        do NOT occupy the workspace.

        Returns:
            (is_busy: bool, reason: Optional[str])
        """
        if not workspace_id:
            return True, "Invalid empty workspace_id"

        # 1. Check Inbox tasks holding workspace (CLAIMED, RUNNING, or WAITING)
        active_inbox_task = self.store.get_workspace_active_task(workspace_id)
        if active_inbox_task:
            return (
                True,
                f"Inbox task '{active_inbox_task['id']}' is {active_inbox_task['status']}",
            )

        # 2. Check existing workflows table in SQLite (strictly ACTIVE statuses only)
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT workflow_id, title, status, metadata_json, config_json
                FROM workflows
                WHERE status IN ('running', 'pending', 'in_progress', 'paused', 'closing')
                """
            ).fetchall()
            for r in rows:
                meta = {}
                if r["metadata_json"]:
                    try:
                        meta = json.loads(r["metadata_json"])
                    except Exception:
                        pass
                config = {}
                if r["config_json"]:
                    try:
                        config = json.loads(r["config_json"])
                    except Exception:
                        pass
                ws = meta.get("workspace_id") or config.get("workspace_id")
                if ws == workspace_id:
                    return (
                        True,
                        f"Active workflow '{r['workflow_id']}' ({r['title']}) is {r['status']}",
                    )
        finally:
            conn.close()

        # 3. Check projects.json active workflows by project_id
        projects_data = load_projects().get("projects", {})
        for p in projects_data.values():
            if p.get("workspace_id") == workspace_id:
                active_wfs = [
                    w for w in active_workflows_for_project(p.get("project_id"))
                    if w.get("status") in ACTIVE_WORKFLOW_STATUSES
                ]
                if active_wfs:
                    wf = active_wfs[0]
                    return (
                        True,
                        f"Active project workflow '{wf.get('workflow_id')}' ({wf.get('title')}) is {wf.get('status')}",
                    )

        return False, None

    def claim_next_task(
        self,
        workspace_id: str,
        lease_duration_seconds: float = 180.0,
    ) -> Optional[Dict[str, Any]]:
        """Atomically verify workspace availability and claim the next highest priority PENDING task.

        Executes active task verification, active workflow verification, and CAS claim
        within the same BEGIN IMMEDIATE transaction to eliminate race conditions.
        """
        if not workspace_id:
            return None

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Check 1: Inbox tasks holding workspace (CLAIMED, RUNNING, or WAITING)
            holding_cur = conn.execute(
                """
                SELECT id FROM user_inbox_tasks
                WHERE workspace_id = ? AND status IN ('CLAIMED', 'RUNNING', 'WAITING')
                LIMIT 1
                """,
                (workspace_id,),
            )
            if holding_cur.fetchone():
                conn.execute("COMMIT")
                return None

            # Check 2: Active workflows in database holding workspace
            active_wf_cur = conn.execute(
                """
                SELECT workflow_id, metadata_json, config_json FROM workflows
                WHERE status IN ('running', 'pending', 'in_progress', 'paused', 'closing')
                """,
            )
            for wf_row in active_wf_cur.fetchall():
                meta = {}
                if wf_row["metadata_json"]:
                    try:
                        meta = json.loads(wf_row["metadata_json"])
                    except Exception:
                        pass
                config = {}
                if wf_row["config_json"]:
                    try:
                        config = json.loads(wf_row["config_json"])
                    except Exception:
                        pass
                ws = meta.get("workspace_id") or config.get("workspace_id")
                if ws == workspace_id:
                    conn.execute("COMMIT")
                    return None

            # Check 3: Active workflows for project
            projects_data = load_projects().get("projects", {})
            for p in projects_data.values():
                if p.get("workspace_id") == workspace_id:
                    active_wfs = [
                        w for w in active_workflows_for_project(p.get("project_id"))
                        if w.get("status") in ACTIVE_WORKFLOW_STATUSES
                    ]
                    if active_wfs:
                        conn.execute("COMMIT")
                        return None

            # Find candidate task: highest priority weight, then sort order, then FIFO
            cur = conn.execute(
                """
                SELECT id FROM user_inbox_tasks
                WHERE workspace_id = ? AND status = 'PENDING'
                ORDER BY priority_weight DESC, sort_order ASC, created_at ASC
                LIMIT 1
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.execute("COMMIT")
                return None

            task_id = row["id"]
            now = time.time()
            lease_exp = now + lease_duration_seconds
            fencing_token = f"{self.worker_id}:{uuid.uuid4().hex[:8]}"

            # CAS update to CLAIMED with unique launcher fencing token
            update_cur = conn.execute(
                """
                UPDATE user_inbox_tasks
                   SET status = 'CLAIMED',
                       worker_id = ?,
                       lease_expires_at = ?,
                       heartbeat_at = ?,
                       attempt_count = attempt_count + 1,
                       updated_at = ?
                 WHERE id = ? AND status = 'PENDING'
                """,
                (fencing_token, lease_exp, now, now, task_id),
            )

            if update_cur.rowcount == 1:
                conn.execute("COMMIT")
                return self.store.get_task(task_id)
            else:
                conn.execute("COMMIT")
                return None
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _safely_stop_fenced_workflow(
        self,
        workflow_id: Optional[str],
        reason: str = "launcher lost ownership (fencing mismatch)",
    ) -> str:
        """Safely stop execution of an orphaned/fenced workflow without erasing its identity (P1).

        Invariants:
        - All running/active tasks associated with the workflow are halted/interrupted before teardown.
        - Preserves project_id, workspace_id, title, config, and metadata via canonical transition_workflow.
        - Releases occupancy only after execution is stopped.

        Returns:
        - "STOPPED": Execution confirmed stopped, workflow transitioned to terminal.
        - "ALREADY_TERMINAL": Workflow was already in a terminal state with no active tasks.
        - "STOP_FAILED": Execution could not be confirmed stopped; workflow status unchanged.
        """
        if not workflow_id:
            return "ALREADY_TERMINAL"
        try:
            from herdr.state_db import get_workflow, list_tasks, transition_task, transition_workflow
            wf = get_workflow(workflow_id, db_path=self.db_path)
            if not wf:
                return "ALREADY_TERMINAL"

            cur_status = str(wf.get("status") or "").lower()
            active_statuses = {"working", "agent_done", "rework", "paused", "blocked", "dispatched", "pending"}

            try:
                all_tasks = list_tasks(workflow_id=workflow_id, db_path=self.db_path)
                active_tasks = [t for t in all_tasks if str(t.get("status") or "").lower() in active_statuses]
            except Exception as task_err:
                logger.warning(f"Error listing tasks for fenced workflow {workflow_id}: {task_err}")
                return "STOP_FAILED"

            if cur_status in TERMINAL_WORKFLOW_STATUSES and not active_tasks:
                return "ALREADY_TERMINAL"

            # 1. Safely stop execution of any active tasks first
            for t in active_tasks:
                tid = t.get("task_id")
                if not tid:
                    continue
                pane_id = t.get("pane_id")
                task_stopped = False

                # Runtime identity verification MUST precede any interrupt or kill signal (P1-02 / Invariant 11)
                if pane_id:
                    try:
                        from herdr.task_resources import owned_live_pane
                        owned, probe_reason = owned_live_pane(t)
                        if owned:
                            # 1. Ownership proven: attempt graceful halt via steering
                            try:
                                from herdr.steering import halt_task
                                res = halt_task(tid, reason=reason, operator="system", execute_kill=True)
                                if isinstance(res, dict) and res.get("ok"):
                                    task_stopped = True
                            except Exception as halt_err:
                                logger.warning(f"Error halting task {tid}: {halt_err}")

                            # 2. If halt failed, close the verified owned pane
                            if not task_stopped:
                                if close_pane(pane_id):
                                    task_stopped = True
                                    try:
                                        transition_task(
                                            task_id=tid,
                                            to_status="failed",
                                            reason=reason,
                                            source="inbox_dispatcher",
                                            db_path=self.db_path,
                                            force=True,
                                        )
                                    except Exception as tr_err:
                                        logger.warning(f"Error transitioning closed task {tid}: {tr_err}")
                                else:
                                    logger.warning(f"Failed to close owned pane {pane_id} for task {tid}")
                        elif probe_reason == "pane_missing":
                            # Positive proof that pane no longer exists; task is physically stopped
                            task_stopped = True
                            try:
                                transition_task(
                                    task_id=tid,
                                    to_status="failed",
                                    reason=reason,
                                    source="inbox_dispatcher",
                                    db_path=self.db_path,
                                    force=True,
                                )
                            except Exception as tr_err:
                                logger.warning(f"Error transitioning missing pane task {tid}: {tr_err}")
                        else:
                            # Ownership NOT proven (e.g. session_mismatch, pane reused by another agent).
                            # Crucial safety gate: refuse to send halt/interrupt signal or close pane!
                            logger.warning(
                                "Runtime ownership for task %s pane %s not proven (%s); "
                                "refusing to send halt signal or close pane to prevent interrupting other agents",
                                tid,
                                pane_id,
                                probe_reason,
                            )
                    except Exception as pane_err:
                        logger.warning(f"Error verifying/stopping pane {pane_id} for task {tid}: {pane_err}")
                else:
                    # If no physical pane was ever attached, status-only transition to failed is safe
                    try:
                        transition_task(
                            task_id=tid,
                            to_status="failed",
                            reason=reason,
                            source="inbox_dispatcher",
                            db_path=self.db_path,
                            force=True,
                        )
                        task_stopped = True
                    except Exception as tr_err:
                        logger.warning(f"Error transitioning unassigned task {tid}: {tr_err}")

                # If neither halt nor close_pane succeeded, agent process may still be executing
                if not task_stopped:
                    logger.error(
                        f"Cannot confirm task {tid} (pane={pane_id}) was stopped; refusing to mark workflow stopped"
                    )
                    return "STOP_FAILED"

            # 2. Transition workflow to completed (outcome=abandoned) via canonical transition gate
            if cur_status not in TERMINAL_WORKFLOW_STATUSES:
                try:
                    transition_workflow(
                        workflow_id=workflow_id,
                        to_status="completed",
                        reason=reason,
                        source="inbox_dispatcher",
                        metadata={"outcome": "abandoned"},
                        force=True,
                        db_path=self.db_path,
                    )
                except Exception as tr_err:
                    logger.error(f"Failed to transition fenced workflow {workflow_id}: {tr_err}")
                    return "STOP_FAILED"

            return "STOPPED"
        except Exception as exc:
            logger.error(f"Unexpected error in _safely_stop_fenced_workflow for {workflow_id}: {exc}")
            return "STOP_FAILED"

    def _handle_post_runner_fencing_mismatch(
        self,
        task_id: str,
        fencing_token: str,
        workflow_id: Optional[str],
        reason_prefix: str = "Fencing token mismatch",
    ) -> Optional[Dict[str, Any]]:
        """Differentiate true orphan workflows from concurrently recovered or terminal workflows (P1-03).

        If another Dispatcher already recovered the task with the same workflow_id as RUNNING or WAITING,
        or if the workflow already reached terminal state, DO NOT kill the workflow.
        Only halt the workflow if it is confirmed to be an untracked orphan.
        """
        if not workflow_id:
            return None

        conn = self._conn()
        try:
            cur = conn.execute(
                "SELECT status, worker_id, workflow_id FROM user_inbox_tasks WHERE id = ?",
                (task_id,),
            )
            chk_row = cur.fetchone()
        finally:
            conn.close()

        if chk_row and chk_row["workflow_id"] == workflow_id:
            chk_st = str(chk_row["status"] or "").upper()
            if chk_st == "CANCELLED":
                logger.info(
                    "Task %s was cancelled by user; stopping workflow %s",
                    task_id, workflow_id,
                )
                self._safely_stop_fenced_workflow(
                    workflow_id,
                    reason=f"{reason_prefix}: task {task_id} was cancelled by user",
                )
                return self.store.get_task(task_id)
            else:
                # RUNNING, WAITING, CLAIMED, COMPLETED, FAILED: task is actively or historically associated with workflow_id
                logger.info(
                    "Task %s remains associated with workflow %s (status=%s, worker=%s); preserving workflow execution",
                    task_id, workflow_id, chk_st, chk_row["worker_id"],
                )
                return self.store.get_task(task_id)

        # Confirmed orphan workflow: either assigned to another worker, reset, or workflow_id mismatched
        logger.error(
            "%s for task %s (expected worker %s, found %s, wf_db=%s, wf_actual=%s); halting orphan workflow",
            reason_prefix,
            task_id,
            fencing_token,
            chk_row["worker_id"] if chk_row else None,
            chk_row["workflow_id"] if chk_row else None,
            workflow_id,
        )
        self._safely_stop_fenced_workflow(
            workflow_id,
            reason=f"{reason_prefix} for task {task_id}; launcher lost ownership",
        )
        return None

    def launch_task(
        self,
        task: Dict[str, Any],
        runner_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
        async_launch: bool = False,
        release_sem_on_finish: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Launch the underlying Workflow for a CLAIMED task.

        Guarantees:
        - Heartbeat loop maintains lease_expires_at while runner is active.
        - Fencing token verification on transition to RUNNING.
        - If fencing token check fails, launcher aborts and halts orphan workflow.
        - Bounded concurrency with optional semaphore release upon completion.
        """
        task_id = task["id"]
        fencing_token = task.get("worker_id") or self.worker_id

        # Pre-assign and persist workflow_id before launch to eliminate crash-window duplicate runs (P1-02)
        target_workflow_id = task.get("workflow_id") or f"wf_ibx_{uuid.uuid4().hex[:8]}"
        task["workflow_id"] = target_workflow_id
        attempt_count = task.get("attempt_count", 1)
        launch_attempt_id = f"{fencing_token}:{attempt_count}"
        task["launch_attempt_id"] = launch_attempt_id
        task["fencing_token"] = fencing_token
        conn_pre = self._conn()
        try:
            now_pre = time.time()
            cur_pre = conn_pre.execute(
                """
                UPDATE user_inbox_tasks
                   SET workflow_id = ?,
                       lease_expires_at = ?,
                       heartbeat_at = ?,
                       updated_at = ?
                 WHERE id = ?
                   AND status = 'CLAIMED'
                   AND worker_id = ?
                   AND (workflow_id IS NULL OR workflow_id = ?)
                   AND (lease_expires_at IS NULL OR lease_expires_at >= ?)
                """,
                (target_workflow_id, now_pre + 180.0, now_pre, now_pre, task_id, fencing_token, target_workflow_id, now_pre),
            )
            if cur_pre.rowcount == 0:
                logger.warning(
                    "Launcher ownership check failed before starting workflow for task %s (worker=%s)",
                    task_id,
                    fencing_token,
                )
                if release_sem_on_finish:
                    _LAUNCH_SEMAPHORE.release()
                return None
            conn_pre.commit()
        except Exception:
            if release_sem_on_finish:
                _LAUNCH_SEMAPHORE.release()
            raise
        finally:
            conn_pre.close()

        def _execute():
            sem_released = False

            def _release_sem():
                nonlocal sem_released
                if release_sem_on_finish and not sem_released:
                    sem_released = True
                    _LAUNCH_SEMAPHORE.release()

            stop_hb = threading.Event()
            lost_ownership = threading.Event()

            def _verify_execution_fencing() -> None:
                """Atomically verify launcher still has valid execution ownership in the database (P1-01)."""
                if lost_ownership.is_set():
                    raise RuntimeError(f"Launcher lost ownership for task {task_id}")
                conn_chk = self._conn()
                try:
                    now_c = time.time()
                    cur_chk = conn_chk.execute(
                        """
                        SELECT worker_id, status, lease_expires_at
                        FROM user_inbox_tasks
                        WHERE id = ?
                        """,
                        (task_id,),
                    )
                    row_c = cur_chk.fetchone()
                    if not row_c:
                        lost_ownership.set()
                        raise RuntimeError(f"Task {task_id} not found at execution fencing boundary")
                    if row_c["status"] != "CLAIMED" or row_c["worker_id"] != fencing_token:
                        lost_ownership.set()
                        raise RuntimeError(
                            f"Launcher lost execution ownership for task {task_id}: "
                            f"expected worker {fencing_token}, found {row_c['worker_id']} (status={row_c['status']})"
                        )
                    if row_c["lease_expires_at"] is not None and row_c["lease_expires_at"] < now_c:
                        lost_ownership.set()
                        raise RuntimeError(
                            f"Launcher lease expired for task {task_id} ({row_c['lease_expires_at']} < {now_c})"
                        )
                finally:
                    conn_chk.close()

            try:
                def _heartbeat_loop():
                    while not stop_hb.wait(25.0):
                        try:
                            conn_hb = self._conn()
                            hb_now = time.time()
                            cur_hb = conn_hb.execute(
                                """
                                UPDATE user_inbox_tasks
                                   SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                                 WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                                """,
                                (hb_now, hb_now + 180.0, hb_now, task_id, fencing_token),
                            )
                            conn_hb.commit()
                            if cur_hb.rowcount == 0:
                                logger.warning(
                                    "Heartbeat update failed (rowcount=0) for task %s (worker=%s); lost ownership",
                                    task_id,
                                    fencing_token,
                                )
                                lost_ownership.set()
                                conn_hb.close()
                                break
                            conn_hb.close()
                        except Exception as hb_exc:
                            logger.warning("Heartbeat exception for task %s: %s", task_id, hb_exc)

                hb_thread = threading.Thread(
                    target=_heartbeat_loop,
                    daemon=True,
                    name=f"inbox_hb_{task_id[:8]}",
                )
                hb_thread.start()

                def _default_runner(t: Dict[str, Any]) -> str:
                    if lost_ownership.is_set():
                        raise RuntimeError(f"Cannot run workflow: launcher lost ownership for task {task_id}")

                    # Idempotency check: if workflow was already registered in state_db, do not launch duplicate agent
                    from herdr.state_db import get_workflow
                    wid_arg = t.get("workflow_id")
                    if wid_arg:
                        existing_wf = get_workflow(wid_arg, db_path=self.db_path)
                        if existing_wf:
                            logger.info("Workflow %s already registered; reusing without re-launching", wid_arg)
                            return wid_arg

                    projects = load_projects().get("projects", {})
                    p_record = next(
                        (p for p in projects.values() if p.get("project_id") == t.get("project_id")),
                        None,
                    )
                    if not p_record or not p_record.get("project_root"):
                        raise RuntimeError(f"Cannot resolve project root for project_id '{t.get('project_id')}'")

                    from console.workflow_api import run_workflow
                    from herdr.workflow import load_template, execution_mode, context_contract_ids

                    tmpl_name = t.get("template_name") or "software-development-v1"

                    # Filter context bindings so internal audit state or undeclared keys are never passed to Workflow CLI
                    wf_context = None
                    try:
                        tmpl = load_template(tmpl_name)
                        mode = execution_mode(tmpl)
                        if mode == "context":
                            req_ids, opt_ids = context_contract_ids(tmpl)
                            declared = set(req_ids) | set(opt_ids)
                            raw_ctx = t.get("context") or {}
                            wf_context = {k: v for k, v in raw_ctx.items() if k in declared}
                    except Exception:
                        wf_context = None

                    _verify_execution_fencing()

                    output = run_workflow(
                        root=p_record["project_root"],
                        req=t["instruction"],
                        agent=t.get("agent_override") or "auto",
                        template=tmpl_name,
                        title=t["title"],
                        context=wf_context,
                        workflow_id=wid_arg,
                        task_id=t["id"],
                        fencing_token=fencing_token,
                        launch_attempt_id=t.get("attempt_count", 1),
                        db_path=self.db_path,
                    )
                    import re
                    m = re.search(r"WORKFLOW_ID=([^\s]+)", output or "")
                    if m:
                        return m.group(1).strip()
                    if wid_arg:
                        return wid_arg
                    from herdr.state_db import list_workflows
                    wfs = list_workflows()
                    if wfs:
                        return wfs[0]["workflow_id"]
                    raise RuntimeError("Workflow launched but workflow_id could not be resolved from output")

                exec_runner = runner_fn or _default_runner

                # Pre-runner atomic CAS renewal: verify launcher still owns task and atomically renew lease (P1)
                if lost_ownership.is_set():
                    logger.warning("Launcher lost ownership prior to CAS renewal for task %s", task_id)
                    return None

                conn_cas = self._conn()
                try:
                    now_check = time.time()
                    cur_cas = conn_cas.execute(
                        """
                        UPDATE user_inbox_tasks
                           SET lease_expires_at = ?, heartbeat_at = ?, updated_at = ?
                         WHERE id = ?
                           AND status = 'CLAIMED'
                           AND worker_id = ?
                           AND (lease_expires_at IS NULL OR lease_expires_at >= ?)
                        """,
                        (now_check + 180.0, now_check, now_check, task_id, fencing_token, now_check),
                    )
                    conn_cas.commit()
                    if cur_cas.rowcount == 0:
                        logger.warning(
                            "Launcher fencing token atomic CAS renewal failed before runner execution for task %s (expected worker %s, now=%s)",
                            task_id, fencing_token, now_check
                        )
                        lost_ownership.set()
                        return None
                finally:
                    conn_cas.close()

                if lost_ownership.is_set():
                    logger.warning("Launcher lost ownership immediately before runner execution for task %s", task_id)
                    return None

                def _fenced_runner(t: Dict[str, Any]) -> str:
                    _verify_execution_fencing()
                    res_wf = exec_runner(t)
                    try:
                        _verify_execution_fencing()
                    except Exception as post_err:
                        logger.warning("Fencing verification failed post-runner for task %s: %s", task_id, post_err)
                        lost_ownership.set()
                    return res_wf

                try:
                    workflow_id = _fenced_runner(task) or target_workflow_id
                    stop_hb.set()

                    if lost_ownership.is_set():
                        return self._handle_post_runner_fencing_mismatch(
                            task_id, fencing_token, workflow_id,
                            reason_prefix="Fencing mismatch detected during runner execution",
                        )

                    # CAS update with fencing token verification
                    conn = self._conn()
                    now = time.time()
                    try:
                        cur = conn.execute(
                            """
                            UPDATE user_inbox_tasks
                               SET status = 'RUNNING',
                                   workflow_id = ?,
                                   updated_at = ?,
                                   started_at = COALESCE(started_at, ?),
                                   lease_expires_at = ?,
                                   heartbeat_at = ?
                             WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                            """,
                            (workflow_id, now, now, now + 180.0, now, task_id, fencing_token),
                        )
                        conn.commit()
                        if cur.rowcount == 0:
                            return self._handle_post_runner_fencing_mismatch(
                                task_id, fencing_token, workflow_id,
                                reason_prefix="Fencing token mismatch on RUNNING transition",
                            )
                    finally:
                        conn.close()

                    return self.store.get_task(task_id)
                except Exception as exc:
                    stop_hb.set()
                    if lost_ownership.is_set():
                        logger.warning(
                            "Launcher lost ownership for task %s during execution; aborting without updating task status",
                            task_id
                        )
                        return None

                    err_msg = str(exc)
                    current_attempts = task.get("attempt_count", 1)
                    max_attempts = task.get("max_attempts", 3)
                    conn = self._conn()
                    now = time.time()
                    try:
                        # Check if workflow was registered in DB before/during launch (P2)
                        from herdr.state_db import get_workflow
                        wf = get_workflow(target_workflow_id, db_path=self.db_path) if target_workflow_id else None
                        if wf:
                            wf_st = str(wf.get("status") or "").lower()
                            wf_out = str(wf.get("outcome") or "").lower()
                            if wf_st in ("running", "in_progress", "pending"):
                                cur = conn.execute(
                                    """
                                    UPDATE user_inbox_tasks
                                       SET status = 'RUNNING',
                                           workflow_id = ?,
                                           error = NULL,
                                           updated_at = ?,
                                           started_at = COALESCE(started_at, ?),
                                           lease_expires_at = ?,
                                           heartbeat_at = ?
                                     WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                                    """,
                                    (target_workflow_id, now, now, now + 180.0, now, task_id, fencing_token),
                                )
                                conn.commit()
                                if cur.rowcount == 0:
                                    return self._handle_post_runner_fencing_mismatch(
                                        task_id, fencing_token, target_workflow_id,
                                        reason_prefix="Fencing mismatch on recovery for task",
                                    )
                                return self.store.get_task(task_id)
                            elif wf_st == "paused":
                                cur = conn.execute(
                                    """
                                    UPDATE user_inbox_tasks
                                       SET status = 'WAITING',
                                           workflow_id = ?,
                                           updated_at = ?
                                     WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                                    """,
                                    (target_workflow_id, now, task_id, fencing_token),
                                )
                                conn.commit()
                                if cur.rowcount == 0:
                                    return self._handle_post_runner_fencing_mismatch(
                                        task_id, fencing_token, target_workflow_id,
                                        reason_prefix="Fencing mismatch on recovery for paused task",
                                    )
                                return self.store.get_task(task_id)
                            elif wf_st in TERMINAL_WORKFLOW_STATUSES:
                                is_failed = (
                                    wf_st in ("failed", "cancelled", "halted", "abandoned")
                                    or wf_out in ("startup_failed", "failed", "failure", "abandoned", "error")
                                )
                                new_st = "FAILED" if is_failed else "COMPLETED"
                                err = f"Workflow '{target_workflow_id}' ended with status '{wf_st}'" if is_failed else None
                                cur = conn.execute(
                                    """
                                    UPDATE user_inbox_tasks
                                       SET status = ?,
                                           workflow_id = ?,
                                           error = ?,
                                           completed_at = ?,
                                           updated_at = ?
                                     WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                                    """,
                                    (new_st, target_workflow_id, err, now, now, task_id, fencing_token),
                                )
                                conn.commit()
                                return self.store.get_task(task_id)

                        # Workflow was never registered in DB: true startup failure, requeue or fail
                        if current_attempts >= max_attempts:
                            conn.execute(
                                """
                                UPDATE user_inbox_tasks
                                   SET status = 'FAILED',
                                       error = ?,
                                       updated_at = ?,
                                       completed_at = ?
                                 WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                                """,
                                (f"Launch failed permanently ({current_attempts}/{max_attempts}): {err_msg}", now, now, task_id, fencing_token),
                            )
                        else:
                            conn.execute(
                                """
                                UPDATE user_inbox_tasks
                                   SET status = 'PENDING',
                                       error = ?,
                                       worker_id = NULL,
                                       lease_expires_at = NULL,
                                       updated_at = ?
                                 WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                                """,
                                (f"Launch failed ({current_attempts}/{max_attempts}): {err_msg}", now, task_id, fencing_token),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                    return self.store.get_task(task_id)
            finally:
                stop_hb.set()
                _release_sem()

        if async_launch:
            try:
                _LAUNCH_EXECUTOR.submit(_execute)
            except Exception:
                if release_sem_on_finish:
                    _LAUNCH_SEMAPHORE.release()
                raise
            # Return task record immediately; status remains CLAIMED until launcher marks RUNNING
            return self.store.get_task(task_id) or task
        else:
            return _execute()

    def reconcile_running_tasks(self) -> List[Dict[str, Any]]:
        """Check all RUNNING and WAITING tasks against underlying Workflow engine state.

        Reconciles completed or failed workflows, freeing the workspace for auto-continuation.
        Also tracks human resumption (WAITING -> RUNNING) and human signoff pause (RUNNING -> WAITING).
        Handles lost workflows after a 60s grace period by marking them FAILED.
        """
        reconciled = []
        tasks_to_reconcile = []
        for st in ("RUNNING", "WAITING"):
            offset = 0
            batch_size = 500
            while True:
                t_list, total = self.store.list_tasks(status=st, limit=batch_size, offset=offset)
                if not t_list:
                    break
                tasks_to_reconcile.extend(t_list)
                offset += len(t_list)
                if offset >= total:
                    break

        from herdr.state_db import get_workflow

        now = time.time()
        for t in tasks_to_reconcile:
            wid = t.get("workflow_id")
            cur_status = t["status"]
            if not wid:
                last_seen = t.get("updated_at") or t.get("started_at") or t.get("created_at") or now
                if (now - last_seen) > 60.0:
                    updated = self.store.update_task_status(
                        task_id=t["id"],
                        new_status="FAILED",
                        expected_status=cur_status,
                        error="Task in active state without assigned workflow_id after grace period",
                    )
                    reconciled.append(updated)
                continue

            wf = get_workflow(wid, db_path=self.db_path)
            if not wf:
                # P2-05: Check grace period (60s) before marking lost workflow as FAILED
                last_seen = t.get("updated_at") or t.get("started_at") or t.get("created_at") or now
                if (now - last_seen) > 60.0:
                    updated = self.store.update_task_status(
                        task_id=t["id"],
                        new_status="FAILED",
                        expected_status=cur_status,
                        error=f"Underlying workflow '{wid}' could not be found after grace period (lost workflow)",
                    )
                    reconciled.append(updated)
                continue

            wf_status = wf.get("status")
            wf_outcome = str(wf.get("outcome") or "").lower()

            if wf_status == "completed":
                if wf_outcome in ("startup_failed", "failed", "failure", "abandoned", "error", "interrupted"):
                    # Workflow lifecycle completed, but outcome was a failure!
                    updated = self.store.update_task_status(
                        task_id=t["id"],
                        new_status="FAILED",
                        expected_status=cur_status,
                        error=f"Underlying workflow '{wid}' finished with failed outcome '{wf_outcome}'",
                    )
                    reconciled.append(updated)
                else:
                    # Workflow verified complete and successful -> update inbox task to COMPLETED
                    updated = self.store.update_task_status(
                        task_id=t["id"],
                        new_status="COMPLETED",
                        expected_status=cur_status,
                    )
                    reconciled.append(updated)
            elif wf_status in ("failed", "cancelled", "halted", "abandoned"):
                updated = self.store.update_task_status(
                    task_id=t["id"],
                    new_status="FAILED",
                    expected_status=cur_status,
                    error=f"Underlying workflow '{wid}' ended with status '{wf_status}'" + (f" (outcome: {wf_outcome})" if wf_outcome else ""),
                )
                reconciled.append(updated)
            elif (wf_status in ("running", "in_progress")) and cur_status == "WAITING":
                # Workflow resumed by human -> transition back to RUNNING
                updated = self.store.update_task_status(
                    task_id=t["id"],
                    new_status="RUNNING",
                    expected_status="WAITING",
                )
                reconciled.append(updated)
            elif (wf_status == "paused" or wf.get("current_stage") == "awaiting_verdict") and cur_status == "RUNNING":
                # Needs human intervention -> transition to WAITING (holds workspace mutex)
                updated = self.store.update_task_status(
                    task_id=t["id"],
                    new_status="WAITING",
                    expected_status="RUNNING",
                )
                reconciled.append(updated)
            else:
                # Still running: extend heartbeat and lease
                if cur_status == "RUNNING":
                    conn = self._conn()
                    try:
                        conn.execute(
                            """
                            UPDATE user_inbox_tasks
                               SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                             WHERE id = ? AND status = 'RUNNING'
                            """,
                            (now, now + 180.0, now, t["id"]),
                        )
                    finally:
                        conn.close()

        return reconciled

    def recover_stale_tasks(self) -> List[Dict[str, Any]]:
        """Recover tasks that were CLAIMED but where worker died or lease expired without RUNNING.

        If a workflow was already created for the task, recovers tracking of the existing
        workflow rather than resetting to PENDING and causing duplicate runs (P1-02).
        """
        recovered = []
        now = time.time()
        from herdr.state_db import get_workflow
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """
                SELECT * FROM user_inbox_tasks
                WHERE status = 'CLAIMED' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (now,),
            )
            rows = [_decode_row(r) for r in cur.fetchall()]
            for r in rows:
                tid = r["id"]
                attempts = r.get("attempt_count", 1)
                max_att = r.get("max_attempts", 3)
                wid = r.get("workflow_id")

                wf = get_workflow(wid, db_path=self.db_path) if wid else None
                if wf:
                    wf_st = str(wf.get("status") or "").lower()
                    wf_out = str(wf.get("outcome") or "").lower()
                    if wf_st in ("running", "in_progress", "pending"):
                        conn.execute(
                            """
                            UPDATE user_inbox_tasks
                               SET status = 'RUNNING',
                                   lease_expires_at = ?,
                                   heartbeat_at = ?,
                                   updated_at = ?
                             WHERE id = ? AND status = 'CLAIMED'
                            """,
                            (now + 180.0, now, now, tid),
                        )
                        recovered.append(self.store.get_task(tid))
                        continue
                    elif wf_st == "paused":
                        conn.execute(
                            """
                            UPDATE user_inbox_tasks
                               SET status = 'WAITING',
                                   updated_at = ?
                             WHERE id = ? AND status = 'CLAIMED'
                            """,
                            (now, tid),
                        )
                        recovered.append(self.store.get_task(tid))
                        continue
                    elif wf_st in TERMINAL_WORKFLOW_STATUSES:
                        new_st = "FAILED" if (wf_st in ("failed", "cancelled", "halted", "abandoned") or wf_out in ("startup_failed", "failed", "failure", "abandoned", "error")) else "COMPLETED"
                        err = f"Workflow '{wid}' ended with status '{wf_st}'" if new_st == "FAILED" else None
                        conn.execute(
                            """
                            UPDATE user_inbox_tasks
                               SET status = ?,
                                   error = ?,
                                   completed_at = ?,
                                   updated_at = ?
                             WHERE id = ? AND status = 'CLAIMED'
                            """,
                            (new_st, err, now, now, tid),
                        )
                        recovered.append(self.store.get_task(tid))
                        continue

                # Workflow not created yet in DB: worker crashed before launching
                if attempts >= max_att:
                    conn.execute(
                        """
                        UPDATE user_inbox_tasks
                           SET status = 'FAILED', error = 'Claim lease expired after maximum retries',
                               updated_at = ?, completed_at = ?
                         WHERE id = ? AND status = 'CLAIMED'
                        """,
                        (now, now, tid),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE user_inbox_tasks
                           SET status = 'PENDING', error = 'Claim lease expired; returned to queue',
                               worker_id = NULL, lease_expires_at = NULL, updated_at = ?
                         WHERE id = ? AND status = 'CLAIMED'
                        """,
                        (now, tid),
                    )
                recovered.append(self.store.get_task(tid))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return [t for t in recovered if t is not None]

    def dispatch_sweep(
        self,
        runner_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
        async_launch: bool = False,
    ) -> Dict[str, Any]:
        """Perform one complete dispatch sweep:

        1. Recover stale tasks with expired claim leases.
        2. Reconcile running and waiting tasks against underlying workflow engine.
        3. Discover all registered projects and their workspaces.
        4. For each available workspace, claim and launch the next pending task.
        """
        report: Dict[str, Any] = {
            "recovered": [],
            "reconciled": [],
            "launched": [],
            "busy_workspaces": [],
        }

        # 1. Recover stale claimed tasks
        report["recovered"] = self.recover_stale_tasks()

        # 2. Reconcile running and waiting tasks
        report["reconciled"] = self.reconcile_running_tasks()

        # 3. Discover workspaces
        projects_data = load_projects().get("projects", {})
        workspaces: Set[str] = set()
        for p in projects_data.values():
            ws = p.get("workspace_id")
            if ws:
                workspaces.add(ws)

        # Also inspect workspace_id in pending tasks
        pending_tasks, _ = self.store.list_tasks(status="PENDING", limit=200)
        for t in pending_tasks:
            ws = t.get("workspace_id")
            if ws:
                workspaces.add(ws)

        # 4. Dispatch for each workspace
        for ws in sorted(workspaces):
            is_busy, reason = self.is_workspace_busy(ws)
            if is_busy:
                report["busy_workspaces"].append({"workspace_id": ws, "reason": reason})
                continue

            if async_launch:
                if not _LAUNCH_SEMAPHORE.acquire(blocking=False):
                    report["busy_workspaces"].append({
                        "workspace_id": ws,
                        "reason": "Launcher worker pool is at capacity (max 2 concurrent launches)",
                    })
                    break

            try:
                claimed = self.claim_next_task(ws)
            except Exception:
                if async_launch:
                    _LAUNCH_SEMAPHORE.release()
                raise

            if not claimed:
                if async_launch:
                    _LAUNCH_SEMAPHORE.release()
                continue

            launched_task = self.launch_task(
                claimed,
                runner_fn=runner_fn,
                async_launch=async_launch,
                release_sem_on_finish=async_launch,
            )
            report["launched"].append(launched_task)

        return report
