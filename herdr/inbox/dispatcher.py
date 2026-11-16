"""Workspace task dispatcher, mutex enforcement and auto-continuation."""

import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from herdr.inbox.store import InboxStore
from herdr.inbox.transitions import (
    WORKSPACE_HOLDING_STATUSES,
    InvalidInboxTransitionError,
)
from herdr.projects import active_workflows_for_project, load_projects
from herdr.state.connection import get_db_connection


class InboxDispatcher:
    """Manages workspace single-task execution, priority scheduling, and auto-continuation."""

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

        Returns:
            (is_busy: bool, reason: Optional[str])
        """
        if not workspace_id:
            return True, "Invalid empty workspace_id"

        # 1. Check Inbox tasks holding workspace (CLAIMED or RUNNING)
        active_inbox_task = self.store.get_workspace_active_task(workspace_id)
        if active_inbox_task:
            return (
                True,
                f"Inbox task '{active_inbox_task['id']}' ({active_inbox_task['title']}) is {active_inbox_task['status']}",
            )

        # 2. Check existing workflows table in SQLite
        conn = self._conn()
        try:
            # Query workflows that match workspace_id and are not in terminal status
            rows = conn.execute(
                """
                SELECT workflow_id, title, status, metadata_json
                FROM workflows
                WHERE status NOT IN ('completed')
                """
            ).fetchall()
            for r in rows:
                meta = {}
                if r["metadata_json"]:
                    try:
                        meta = json.loads(r["metadata_json"])
                    except Exception:
                        pass
                ws = meta.get("workspace_id")
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
                active_wfs = active_workflows_for_project(p.get("project_id"))
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
        """Atomically claim the next highest priority PENDING task for workspace_id."""
        if not workspace_id:
            return None

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Check if workspace is already busy in this transaction
            holding_cur = conn.execute(
                """
                SELECT id FROM user_inbox_tasks
                WHERE workspace_id = ? AND status IN ('CLAIMED', 'RUNNING')
                LIMIT 1
                """,
                (workspace_id,),
            )
            if holding_cur.fetchone():
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

            # CAS update
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
                (self.worker_id, lease_exp, now, now, task_id),
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

    def launch_task(
        self,
        task_id: str,
        runner_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> Dict[str, Any]:
        """Launch execution for a CLAIMED task and transition it to RUNNING."""
        task = self.store.get_task(task_id)
        if not task:
            raise KeyError(f"Task not found: {task_id}")
        if task["status"] != "CLAIMED":
            raise InvalidInboxTransitionError(f"Task {task_id} must be CLAIMED to launch, got '{task['status']}'")

        def _default_runner(t: Dict[str, Any]) -> str:
            # Look up project root from load_projects
            projects = load_projects().get("projects", {})
            p_record = next(
                (p for p in projects.values() if p.get("project_id") == t.get("project_id")),
                None,
            )
            if not p_record or not p_record.get("project_root"):
                raise RuntimeError(f"Cannot resolve project root for project_id '{t.get('project_id')}'")

            from console.workflow_api import run_workflow
            output = run_workflow(
                root=p_record["project_root"],
                req=t["instruction"],
                agent=t.get("agent_override") or "auto",
                template=t.get("template_name") or "software-development-v1",
                title=t["title"],
                context=t.get("context"),
            )
            # Parse WORKFLOW_ID from output
            import re
            m = re.search(r"WORKFLOW_ID=([^\s]+)", output or "")
            if m:
                return m.group(1).strip()
            # If not formatted with WORKFLOW_ID=..., find newest workflow
            from herdr.state_db import list_workflows
            wfs = list_workflows()
            if wfs:
                return wfs[0]["workflow_id"]
            raise RuntimeError("Workflow launched but workflow_id could not be resolved from output")

        exec_runner = runner_fn or _default_runner

        try:
            workflow_id = exec_runner(task)
            # Successfully launched -> move to RUNNING
            return self.store.update_task_status(
                task_id=task_id,
                new_status="RUNNING",
                expected_status="CLAIMED",
                workflow_id=workflow_id,
                worker_id=self.worker_id,
            )
        except Exception as exc:
            err_msg = str(exc)
            # Check attempts
            current_attempts = task.get("attempt_count", 1)
            max_attempts = task.get("max_attempts", 3)
            if current_attempts >= max_attempts:
                # Exceeded max retry -> FAILED
                return self.store.update_task_status(
                    task_id=task_id,
                    new_status="FAILED",
                    expected_status="CLAIMED",
                    error=f"Launch failed permanently ({current_attempts}/{max_attempts}): {err_msg}",
                )
            else:
                # Reset to PENDING for retry
                return self.store.update_task_status(
                    task_id=task_id,
                    new_status="PENDING",
                    expected_status="CLAIMED",
                    error=f"Launch failed ({current_attempts}/{max_attempts}): {err_msg}",
                )

    def reconcile_running_tasks(self) -> List[Dict[str, Any]]:
        """Check all RUNNING tasks against underlying Workflow engine state.

        Reconciles completed or failed workflows, freeing the workspace for auto-continuation.
        """
        reconciled = []
        running_tasks, _ = self.store.list_tasks(status="RUNNING", limit=500)
        from herdr.state_db import get_workflow

        now = time.time()
        for t in running_tasks:
            wid = t.get("workflow_id")
            if not wid:
                continue

            wf = get_workflow(wid, db_path=self.db_path)
            if not wf:
                continue

            wf_status = wf.get("status")
            if wf_status == "completed":
                # Workflow verified complete -> update inbox task to COMPLETED
                updated = self.store.update_task_status(
                    task_id=t["id"],
                    new_status="COMPLETED",
                    expected_status="RUNNING",
                )
                reconciled.append(updated)
            elif wf_status in ("failed", "cancelled", "halted"):
                updated = self.store.update_task_status(
                    task_id=t["id"],
                    new_status="FAILED",
                    expected_status="RUNNING",
                    error=f"Underlying workflow '{wid}' ended with status '{wf_status}'",
                )
                reconciled.append(updated)
            elif wf_status == "paused" or wf.get("current_stage") == "awaiting_verdict":
                # Needs intervention
                updated = self.store.update_task_status(
                    task_id=t["id"],
                    new_status="WAITING",
                    expected_status="RUNNING",
                )
                reconciled.append(updated)
            else:
                # Still running: extend heartbeat and lease
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
        """Recover tasks that were CLAIMED but where worker died or lease expired without RUNNING."""
        recovered = []
        now = time.time()
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
                if attempts >= max_att:
                    conn.execute(
                        """
                        UPDATE user_inbox_tasks
                           SET status = 'FAILED', error = 'Claim lease expired after maximum retries',
                               updated_at = ?, completed_at = ?
                         WHERE id = ?
                        """,
                        (now, now, tid),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE user_inbox_tasks
                           SET status = 'PENDING', error = 'Claim lease expired; returned to queue',
                               worker_id = NULL, lease_expires_at = NULL, updated_at = ?
                         WHERE id = ?
                        """,
                        (now, tid),
                    )
                recovered.append(r)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return recovered

    def dispatch_sweep(
        self,
        runner_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    ) -> Dict[str, Any]:
        """Perform one complete dispatch sweep:

        1. Reconcile running tasks (frees completed workspaces).
        2. Recover stale claims.
        3. Identify idle workspaces with pending tasks.
        4. Claim and launch the next task for each idle workspace.
        """
        reconciled = self.reconcile_running_tasks()
        recovered = self.recover_stale_tasks()

        # Find all workspaces that have PENDING tasks
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                SELECT DISTINCT workspace_id FROM user_inbox_tasks
                WHERE status = 'PENDING' AND workspace_id IS NOT NULL AND workspace_id != ''
                """
            )
            workspaces = [row["workspace_id"] for row in cur.fetchall()]
        finally:
            conn.close()

        launched = []
        busy_workspaces = []

        for ws in workspaces:
            is_busy, reason = self.is_workspace_busy(ws)
            if is_busy:
                busy_workspaces.append({"workspace_id": ws, "reason": reason})
                continue

            claimed_task = self.claim_next_task(ws)
            if claimed_task:
                launched_task = self.launch_task(claimed_task["id"], runner_fn=runner_fn)
                launched.append(launched_task)

        return {
            "reconciled": reconciled,
            "recovered": recovered,
            "launched": launched,
            "busy_workspaces": busy_workspaces,
        }
