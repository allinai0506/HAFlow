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
    ) -> None:
        """Safely stop execution of an orphaned/fenced workflow without erasing its identity (P1).

        Invariants:
        - All running/active tasks associated with the workflow are halted/interrupted before teardown.
        - Preserves project_id, workspace_id, title, config, and metadata via canonical transition_workflow.
        - Releases occupancy only after execution is stopped.
        """
        if not workflow_id:
            return
        try:
            from herdr.state_db import get_workflow, list_tasks, transition_task, transition_workflow
            wf = get_workflow(workflow_id, db_path=self.db_path)
            if not wf:
                return

            # 1. Safely stop execution of any active tasks first
            try:
                active_tasks = list_tasks(workflow_id=workflow_id, db_path=self.db_path)
                for t in active_tasks:
                    st = str(t.get("status") or "").lower()
                    if st in ("working", "agent_done", "rework", "paused", "blocked", "dispatched", "pending"):
                        tid = t.get("task_id")
                        if not tid:
                            continue
                        halted = False
                        try:
                            from herdr.steering import halt_task
                            res = halt_task(tid, reason=reason, operator="system", execute_kill=True)
                            if isinstance(res, dict) and res.get("ok"):
                                halted = True
                        except Exception:
                            pass
                        if not halted:
                            try:
                                transition_task(
                                    task_id=tid,
                                    to_status="failed",
                                    reason=reason,
                                    source="inbox_dispatcher",
                                    db_path=self.db_path,
                                )
                            except Exception:
                                pass
                        pane_id = t.get("pane_id")
                        if pane_id:
                            try:
                                from herdr.pane_pool import close_pane
                                close_pane(pane_id)
                            except Exception:
                                pass
            except Exception as task_err:
                logger.warning(f"Error halting tasks for fenced workflow {workflow_id}: {task_err}")

            # 2. Transition workflow to completed (outcome=abandoned) via canonical transition gate
            cur_status = str(wf.get("status") or "").lower()
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
        except Exception as exc:
            logger.error(f"Unexpected error in _safely_stop_fenced_workflow for {workflow_id}: {exc}")

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
        conn_pre = self._conn()
        try:
            conn_pre.execute(
                """
                UPDATE user_inbox_tasks
                   SET workflow_id = ?, updated_at = ?
                 WHERE id = ? AND (workflow_id IS NULL OR workflow_id = ?)
                """,
                (target_workflow_id, time.time(), task_id, target_workflow_id),
            )
            conn_pre.commit()
        finally:
            conn_pre.close()

        def _execute():
            stop_hb = threading.Event()

            def _heartbeat_loop():
                while not stop_hb.wait(25.0):
                    try:
                        conn_hb = self._conn()
                        hb_now = time.time()
                        conn_hb.execute(
                            """
                            UPDATE user_inbox_tasks
                               SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                             WHERE id = ? AND status = 'CLAIMED' AND worker_id = ?
                            """,
                            (hb_now, hb_now + 180.0, hb_now, task_id, fencing_token),
                        )
                        conn_hb.commit()
                        conn_hb.close()
                    except Exception:
                        pass

            hb_thread = threading.Thread(
                target=_heartbeat_loop,
                daemon=True,
                name=f"inbox_hb_{task_id[:8]}",
            )
            hb_thread.start()

            def _default_runner(t: Dict[str, Any]) -> str:
                projects = load_projects().get("projects", {})
                p_record = next(
                    (p for p in projects.values() if p.get("project_id") == t.get("project_id")),
                    None,
                )
                if not p_record or not p_record.get("project_root"):
                    raise RuntimeError(f"Cannot resolve project root for project_id '{t.get('project_id')}'")

                from console.workflow_api import run_workflow
                from herdr.workflow import load_template, execution_mode, context_contract_ids

                wid_arg = t.get("workflow_id")
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

                output = run_workflow(
                    root=p_record["project_root"],
                    req=t["instruction"],
                    agent=t.get("agent_override") or "auto",
                    template=tmpl_name,
                    title=t["title"],
                    context=wf_context,
                    workflow_id=wid_arg,
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

            try:
                workflow_id = exec_runner(task) or target_workflow_id
                stop_hb.set()

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
                        logger.error(
                            f"Fencing token mismatch for task {task_id}; lost ownership. Halting workflow {workflow_id}."
                        )
                        self._safely_stop_fenced_workflow(
                            workflow_id,
                            reason=f"Fencing token mismatch for task {task_id}; launcher lost ownership",
                        )
                        return None
                finally:
                    conn.close()

                return self.store.get_task(task_id)
            except Exception as exc:
                stop_hb.set()
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
                                self._safely_stop_fenced_workflow(
                                    target_workflow_id,
                                    reason=f"Fencing mismatch on recovery for task {task_id}",
                                )
                                return None
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
                                self._safely_stop_fenced_workflow(
                                    target_workflow_id,
                                    reason=f"Fencing mismatch on recovery for task {task_id}",
                                )
                                return None
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
                if release_sem_on_finish:
                    _LAUNCH_SEMAPHORE.release()

        if async_launch:
            _LAUNCH_EXECUTOR.submit(_execute)
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
            t_list, _ = self.store.list_tasks(status=st, limit=500)
            tasks_to_reconcile.extend(t_list)

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

            claimed = self.claim_next_task(ws)
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
