"""Inbox task storage, persistence and state management."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr.inbox.schema import ensure_inbox_schema
from herdr.inbox.transitions import (
    ALL_INBOX_STATUSES,
    PRIORITY_DEFAULT,
    PRIORITY_WEIGHTS,
    TERMINAL_INBOX_STATUSES,
    VALID_PRIORITIES,
    WORKSPACE_HOLDING_STATUSES,
    InvalidInboxTransitionError,
    validate_inbox_transition,
)
from herdr.state.connection import get_db_connection


def _decode_row(row: sqlite3.Row) -> Dict[str, Any]:
    task = dict(row)
    context_raw = task.get("context_json")
    if context_raw:
        try:
            task["context"] = json.loads(context_raw)
        except Exception:
            task["context"] = {}
    else:
        task["context"] = {}

    hist_raw = task.get("execution_history_json")
    if hist_raw:
        try:
            task["execution_history"] = json.loads(hist_raw)
        except Exception:
            task["execution_history"] = []
    else:
        if isinstance(task.get("context"), dict) and "execution_history" in task["context"]:
            task["execution_history"] = task["context"].get("execution_history") or []
        else:
            task["execution_history"] = []
    return task


class InboxStore:
    """Thread-safe store for User Task Inbox records."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = get_db_connection(self.db_path)
        try:
            ensure_inbox_schema(conn)
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def create_task(
        self,
        title: str,
        instruction: str,
        project_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        user_id: str = "default",
        priority: str = PRIORITY_DEFAULT,
        source_type: str = "manual",
        source_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        template_name: str = "software-development-v1",
        agent_override: str = "auto",
        context: Optional[Dict[str, Any]] = None,
        auto_confirm: bool = False,
    ) -> Dict[str, Any]:
        """Create a new task in Inbox."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                return self.insert_task(
                    conn, title, instruction, project_id, workspace_id, user_id=user_id,
                    priority=priority, source_type=source_type, source_id=source_id,
                    idempotency_key=idempotency_key, template_name=template_name,
                    agent_override=agent_override, context=context, auto_confirm=auto_confirm,
                )
        finally:
            conn.close()

    @staticmethod
    def insert_task(
        conn: sqlite3.Connection,
        title: str,
        instruction: str,
        project_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        user_id: str = "default",
        priority: str = PRIORITY_DEFAULT,
        source_type: str = "manual",
        source_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        template_name: str = "software-development-v1",
        agent_override: str = "auto",
        context: Optional[Dict[str, Any]] = None,
        auto_confirm: bool = False,
    ) -> Dict[str, Any]:
        """Validate and write using the caller's open transaction."""
        if not conn.in_transaction:
            raise RuntimeError("Inbox insert requires an open transaction")
        title = (title or "").strip()
        instruction = (instruction or "").strip()
        if not title:
            raise ValueError("Task title cannot be empty")
        if not instruction:
            raise ValueError("Task instruction cannot be empty")

        p = str(priority).upper().strip()
        if p not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority '{priority}'. Allowed: {sorted(VALID_PRIORITIES)}")
        priority_weight = PRIORITY_WEIGHTS[p]
        eff_user = user_id or "default"

        # Check idempotency scoped per user
        if idempotency_key:
            cur = conn.execute(
                "SELECT * FROM user_inbox_tasks WHERE user_id = ? AND idempotency_key = ?",
                (eff_user, idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                return _decode_row(existing)

        # Determine initial status
        # Only confirmed project + workspace can be PENDING.
        has_scope = bool(project_id and workspace_id)
        initial_status = "PENDING" if (has_scope and auto_confirm) else "DRAFT"

        # Allocate sort_order at the tail of this priority queue in the workspace
        next_sort_order = 0
        if workspace_id:
            cur_ord = conn.execute(
                """
                SELECT COALESCE(MAX(sort_order), -10) as max_ord
                FROM user_inbox_tasks
                WHERE workspace_id = ? AND priority = ?
                """,
                (workspace_id, p),
            ).fetchone()
            if cur_ord and cur_ord["max_ord"] is not None:
                next_sort_order = max(0, int(cur_ord["max_ord"]) + 10)

        now = time.time()
        task_id = f"inbox_{uuid.uuid4().hex[:12]}"
        context_json = json.dumps(context or {}, ensure_ascii=False)

        conn.execute(
            """
            INSERT INTO user_inbox_tasks (
                id, user_id, project_id, workspace_id, title, instruction,
                source_type, source_id, idempotency_key, priority, priority_weight,
                sort_order, status, template_name, agent_override, context_json,
                execution_history_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                eff_user,
                project_id,
                workspace_id,
                title,
                instruction,
                source_type or "manual",
                source_id,
                idempotency_key,
                p,
                priority_weight,
                next_sort_order,
                initial_status,
                template_name or "software-development-v1",
                agent_override or "auto",
                context_json,
                "[]",
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM user_inbox_tasks WHERE id = ?", (task_id,)).fetchone()
        return _decode_row(row)

    @staticmethod
    def _manual_filter(conn):
        # Schema discovery and task reads must share a snapshot during migration.
        conn.execute("BEGIN")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'intake_task_links'").fetchone():
            return "NOT EXISTS (SELECT 1 FROM intake_task_links WHERE task_id = user_inbox_tasks.id AND user_inbox_tasks.status = 'DRAFT')"
        return "1=1"

    def get_task(self, task_id: str, *, manual_only=False) -> Optional[Dict[str, Any]]:
        """Fetch an inbox task by ID."""
        conn = self._conn()
        try:
            condition = "id = ?"
            if manual_only:
                condition += " AND " + self._manual_filter(conn)
            row = conn.execute(f"SELECT * FROM user_inbox_tasks WHERE {condition}", (task_id,)).fetchone()
            return _decode_row(row) if row else None
        finally:
            conn.close()

    def list_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        project_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        priority: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        *,
        manual_only: bool = False,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List tasks with filtering and queue position calculation."""
        conn = self._conn()
        try:
            where_clauses = [self._manual_filter(conn) if manual_only else "1=1"]
            params: List[Any] = []

            if user_id:
                where_clauses.append("user_id = ?")
                params.append(user_id)
            if status:
                where_clauses.append("status = ?")
                params.append(status.upper())
            if project_id:
                where_clauses.append("project_id = ?")
                params.append(project_id)
            if workspace_id:
                where_clauses.append("workspace_id = ?")
                params.append(workspace_id)
            if priority:
                where_clauses.append("priority = ?")
                params.append(priority.upper())

            where_str = " AND ".join(where_clauses)

            # Get total count
            total_cur = conn.execute(f"SELECT COUNT(1) FROM user_inbox_tasks WHERE {where_str}", tuple(params))
            total = total_cur.fetchone()[0]

            # Ordering strategy:
            # If filtering specifically by PENDING, order by priority and queue order.
            # Otherwise order by created_at DESC.
            if status == "PENDING":
                order_by = "priority_weight DESC, sort_order ASC, created_at ASC"
            else:
                order_by = "created_at DESC"

            query = f"""
                SELECT * FROM user_inbox_tasks
                WHERE {where_str}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
            """
            cur = conn.execute(query, tuple(params + [limit, offset]))
            tasks = [_decode_row(row) for row in cur.fetchall()]

            # Compute queue position for pending tasks per workspace
            # A pending task's queue position is 1 + number of pending tasks ahead of it in the same workspace.
            for t in tasks:
                if t.get("status") == "PENDING" and t.get("workspace_id"):
                    pos_cur = conn.execute(
                        """
                        SELECT COUNT(1) FROM user_inbox_tasks
                        WHERE workspace_id = ? AND status = 'PENDING'
                          AND (
                              priority_weight > ?
                              OR (priority_weight = ? AND sort_order < ?)
                              OR (priority_weight = ? AND sort_order = ? AND created_at < ?)
                          )
                        """,
                        (
                            t["workspace_id"],
                            t["priority_weight"],
                            t["priority_weight"],
                            t["sort_order"],
                            t["priority_weight"],
                            t["sort_order"],
                            t["created_at"],
                        ),
                    )
                    ahead = pos_cur.fetchone()[0]
                    t["queue_position"] = ahead + 1
                else:
                    t["queue_position"] = None

            return tasks, total
        finally:
            conn.close()

    def update_task_status(
        self,
        task_id: str,
        new_status: str,
        expected_status: Optional[str] = None,
        error: Optional[str] = None,
        workflow_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        lease_expires_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Atomically transition task status with strict validation."""
        target = str(new_status).upper()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT * FROM user_inbox_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Inbox task not found: {task_id}")

            current = row["status"]
            if expected_status and current != expected_status:
                raise InvalidInboxTransitionError(
                    f"Task {task_id} status mismatch: expected '{expected_status}', found '{current}'"
                )

            validate_inbox_transition(current, target)

            now = time.time()
            updates = ["status = ?", "updated_at = ?"]
            params: List[Any] = [target, now]

            if error is not None:
                updates.append("error = ?")
                params.append(error)
            if workflow_id is not None:
                updates.append("workflow_id = ?")
                params.append(workflow_id)
            if worker_id is not None:
                updates.append("worker_id = ?")
                params.append(worker_id)
            if lease_expires_at is not None:
                updates.append("lease_expires_at = ?")
                params.append(lease_expires_at)

            if target == "RUNNING" and not row["started_at"]:
                updates.append("started_at = ?")
                params.append(now)
            elif target in ("COMPLETED", "FAILED", "CANCELLED") and not row["completed_at"]:
                updates.append("completed_at = ?")
                params.append(now)

            params.append(task_id)
            update_sql = f"UPDATE user_inbox_tasks SET {', '.join(updates)} WHERE id = ?"
            conn.execute(update_sql, tuple(params))
            conn.execute("COMMIT")

            res_task = self.get_task(task_id)  # type: ignore
            if target in ("COMPLETED", "FAILED") and row["source_type"] == "active_intake":
                try:
                    from herdr.intake.connectors.linear.sync import dispatch_linear_sync_on_task_status
                    dispatch_linear_sync_on_task_status(Path(self.db_path), task_id, target, error=error)
                except Exception:
                    pass
            return res_task
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def confirm_task_scope(
        self,
        task_id: str,
        project_id: str,
        workspace_id: str,
    ) -> Dict[str, Any]:
        """Confirm project and workspace for a DRAFT task and move it to PENDING.

        Reallocates sort_order to the tail of the target workspace priority queue
        inside the atomic transaction to prevent accidental queue jumping.
        """
        if not project_id or not workspace_id:
            raise ValueError("project_id and workspace_id are required to confirm task")

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT status, priority FROM user_inbox_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Inbox task not found: {task_id}")
            if row["status"] != "DRAFT":
                raise InvalidInboxTransitionError(f"Only DRAFT tasks can be confirmed, current is '{row['status']}'")

            # Allocate sort_order at the tail of this priority queue in the workspace
            cur_ord = conn.execute(
                """
                SELECT COALESCE(MAX(sort_order), -10) as max_ord
                FROM user_inbox_tasks
                WHERE workspace_id = ? AND priority = ?
                """,
                (workspace_id, row["priority"]),
            ).fetchone()
            next_sort_order = max(0, int(cur_ord["max_ord"]) + 10) if (cur_ord and cur_ord["max_ord"] is not None) else 0

            now = time.time()
            conn.execute(
                """
                UPDATE user_inbox_tasks
                   SET project_id = ?, workspace_id = ?, sort_order = ?, status = 'PENDING', updated_at = ?
                 WHERE id = ?
                """,
                (project_id, workspace_id, next_sort_order, now, task_id),
            )
            conn.execute("COMMIT")
            return self.get_task(task_id)  # type: ignore
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def set_task_priority(self, task_id: str, priority: str) -> Dict[str, Any]:
        """Update priority for a task in DRAFT or PENDING."""
        p = str(priority).upper().strip()
        if p not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority '{priority}'. Allowed: {sorted(VALID_PRIORITIES)}")
        weight = PRIORITY_WEIGHTS[p]

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT status FROM user_inbox_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Inbox task not found: {task_id}")
            if row["status"] not in ("DRAFT", "PENDING"):
                raise InvalidInboxTransitionError(
                    f"Cannot change priority of task in '{row['status']}' state; only DRAFT or PENDING allowed."
                )

            now = time.time()
            conn.execute(
                """
                UPDATE user_inbox_tasks
                   SET priority = ?, priority_weight = ?, updated_at = ?
                 WHERE id = ?
                """,
                (p, weight, now, task_id),
            )
            conn.execute("COMMIT")
            return self.get_task(task_id)  # type: ignore
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def reorder_pending_task(self, task_id: str, direction: str) -> Dict[str, Any]:
        """Adjust sort_order of a pending task relative to adjacent pending tasks in same workspace."""
        if direction not in ("up", "down"):
            raise ValueError("Direction must be 'up' or 'down'")

        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT * FROM user_inbox_tasks WHERE id = ?", (task_id,))
            task = cur.fetchone()
            if not task:
                raise KeyError(f"Inbox task not found: {task_id}")
            if task["status"] != "PENDING":
                raise InvalidInboxTransitionError("Only PENDING tasks can be reordered")

            ws = task["workspace_id"]
            # Fetch all pending tasks in this workspace ordered
            cur = conn.execute(
                """
                SELECT id, sort_order FROM user_inbox_tasks
                WHERE workspace_id = ? AND status = 'PENDING'
                ORDER BY priority_weight DESC, sort_order ASC, created_at ASC
                """,
                (ws,),
            )
            rows = cur.fetchall()
            ids = [r["id"] for r in rows]
            if task_id not in ids:
                conn.execute("COMMIT")
                return self.get_task(task_id)  # type: ignore

            idx = ids.index(task_id)
            swap_idx = idx - 1 if direction == "up" else idx + 1
            if 0 <= swap_idx < len(ids):
                # Swap target with neighbor in the python list
                ids[idx], ids[swap_idx] = ids[swap_idx], ids[idx]
                # Re-assign dense sequence 10, 20, 30, 40... to ALL pending tasks in this workspace
                now = time.time()
                for rank, tid in enumerate(ids):
                    conn.execute(
                        "UPDATE user_inbox_tasks SET sort_order = ?, updated_at = ? WHERE id = ?",
                        (rank * 10, now, tid),
                    )

            conn.execute("COMMIT")
            return self.get_task(task_id)  # type: ignore
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Cancel a task if not already completed/failed.

        Protects against concurrency race conditions with Dispatcher launch:
        - If task is PENDING/DRAFT and has no workflow, transitions to CANCELLED atomically under BEGIN IMMEDIATE.
        - If task is CLAIMED, RUNNING, or WAITING (or has a workflow_id), safely stops
          the underlying workflow first to release workspace occupancy, then transitions to CANCELLED.
        - If stopping fails, preserves task status and workspace occupancy.
        """
        while True:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("SELECT * FROM user_inbox_tasks WHERE id = ?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Inbox task not found: {task_id}")
                task = _decode_row(row)
                cur_status = str(task.get("status") or "").upper()
                if cur_status in TERMINAL_INBOX_STATUSES:
                    conn.execute("COMMIT")
                    return task

                wid = task.get("workflow_id")
                # If PENDING or DRAFT without an underlying workflow, transition to CANCELLED immediately
                # under exclusive transaction to prevent Dispatcher from claiming it in the race window.
                if cur_status in ("PENDING", "DRAFT") and not wid:
                    now = time.time()
                    cur_up = conn.execute(
                        """
                        UPDATE user_inbox_tasks
                           SET status = 'CANCELLED',
                               error = ?,
                               completed_at = COALESCE(completed_at, ?),
                               updated_at = ?
                         WHERE id = ? AND status = ?
                        """,
                        (reason or "Cancelled by user", now, now, task_id, cur_status),
                    )
                    if cur_up.rowcount == 1:
                        conn.execute("COMMIT")
                        return self.get_task(task_id)  # type: ignore
                    else:
                        conn.execute("ROLLBACK")
                        continue
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

            # Task is CLAIMED, RUNNING, or WAITING (or has a workflow_id).
            # Safely stop underlying workflow outside of inbox DB lock to avoid deadlocks.
            if wid and cur_status in ("WAITING", "RUNNING", "CLAIMED"):
                from herdr.inbox.dispatcher import InboxDispatcher
                stop_res = InboxDispatcher(db_path=self.db_path)._safely_stop_fenced_workflow(
                    workflow_id=wid,
                    reason=reason or "Inbox task cancelled by user",
                )
                if stop_res not in ("STOPPED", "ALREADY_TERMINAL"):
                    # Record error so state is traceable, but preserve status and workspace occupancy!
                    conn = self._conn()
                    try:
                        conn.execute(
                            "UPDATE user_inbox_tasks SET error = ?, updated_at = ? WHERE id = ?",
                            (
                                f"Cancel failed: could not confirm agent stop for workflow {wid} ({stop_res})",
                                time.time(),
                                task_id,
                            ),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    raise RuntimeError(
                        f"Cannot cancel task {task_id}: failed to stop underlying workflow {wid} (stop_result={stop_res})"
                    )

            # Atomically verify and transition to CANCELLED
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute("SELECT * FROM user_inbox_tasks WHERE id = ?", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise KeyError(f"Inbox task not found: {task_id}")
                latest_task = _decode_row(row)
                latest_st = str(latest_task.get("status") or "").upper()
                latest_wid = latest_task.get("workflow_id")

                if latest_st in TERMINAL_INBOX_STATUSES:
                    conn.execute("COMMIT")
                    return latest_task

                # If workflow was attached or changed during stop window, loop and stop the new workflow
                if latest_wid and latest_wid != wid and latest_st in ("WAITING", "RUNNING", "CLAIMED"):
                    conn.execute("ROLLBACK")
                    continue

                validate_inbox_transition(latest_st, "CANCELLED")
                now = time.time()
                cur_cancel = conn.execute(
                    """
                    UPDATE user_inbox_tasks
                       SET status = 'CANCELLED',
                           error = ?,
                           completed_at = COALESCE(completed_at, ?),
                           updated_at = ?
                     WHERE id = ?
                       AND status = ?
                       AND (workflow_id = ? OR (workflow_id IS NULL AND ? IS NULL))
                    """,
                    (reason or "Cancelled by user", now, now, task_id, latest_st, wid, wid),
                )
                if cur_cancel.rowcount == 0:
                    conn.execute("ROLLBACK")
                    continue
                conn.execute("COMMIT")
                return self.get_task(task_id)  # type: ignore
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def retry_task(self, task_id: str) -> Dict[str, Any]:
        """Retry a failed task by returning it to PENDING without polluting workflow context."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("SELECT * FROM user_inbox_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Inbox task not found: {task_id}")
            if row["status"] != "FAILED":
                raise InvalidInboxTransitionError(f"Only FAILED tasks can be retried, current is '{row['status']}'")

            now = time.time()
            # Execution history is stored independently in execution_history_json
            raw_hist = row["execution_history_json"] if "execution_history_json" in row.keys() else None
            history = []
            if raw_hist:
                try:
                    history = json.loads(raw_hist)
                except Exception:
                    history = []
            elif row["context_json"]:
                try:
                    old_ctx = json.loads(row["context_json"])
                    if isinstance(old_ctx, dict) and "execution_history" in old_ctx:
                        history = list(old_ctx["execution_history"])
                except Exception:
                    pass

            history.append({
                "attempt": row["attempt_count"],
                "workflow_id": row["workflow_id"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "error": row["error"],
                "retried_at": now,
            })
            new_hist_json = json.dumps(history, ensure_ascii=False)

            # Strip execution_history from context_json if legacy contamination occurred
            ctx = json.loads(row["context_json"] or "{}") if row["context_json"] else {}
            if isinstance(ctx, dict) and "execution_history" in ctx:
                ctx.pop("execution_history", None)
            clean_ctx_json = json.dumps(ctx, ensure_ascii=False)

            conn.execute(
                """
                UPDATE user_inbox_tasks
                   SET status = 'PENDING', error = NULL, attempt_count = 0,
                       workflow_id = NULL, worker_id = NULL, started_at = NULL,
                       completed_at = NULL, context_json = ?, execution_history_json = ?, updated_at = ?
                 WHERE id = ?
                """,
                (clean_ctx_json, new_hist_json, now, task_id),
            )
            conn.execute("COMMIT")
            return self.get_task(task_id)  # type: ignore
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_inbox_stats(self, user_id: Optional[str] = None, *, manual_only=False) -> Dict[str, Any]:
        """Return counts by status and busy workspaces summary."""
        conn = self._conn()
        try:
            conditions = ["user_id = ?"] if user_id else ["1=1"]
            if manual_only:
                conditions.append(self._manual_filter(conn))
            where = "WHERE " + " AND ".join(conditions)
            params = [user_id] if user_id else []

            cur = conn.execute(
                f"SELECT status, COUNT(1) as cnt FROM user_inbox_tasks {where} GROUP BY status",
                tuple(params),
            )
            counts = {s: 0 for s in ALL_INBOX_STATUSES}
            for row in cur.fetchall():
                counts[row["status"]] = row["cnt"]

            # Busy workspaces (sanitized across user boundaries)
            busy_cur = conn.execute(
                """
                SELECT workspace_id, id, title, status, user_id FROM user_inbox_tasks
                WHERE status IN ('CLAIMED', 'RUNNING') AND workspace_id IS NOT NULL
                """
            )
            busy_workspaces = {}
            for r in busy_cur.fetchall():
                is_owner = (user_id is None) or (r["user_id"] == user_id)
                busy_workspaces[r["workspace_id"]] = {
                    "task_id": r["id"] if is_owner else None,
                    "title": r["title"] if is_owner else "任务执行中 (其他用户)",
                    "status": r["status"],
                }

            return {
                "total": sum(counts.values()),
                "counts": counts,
                "busy_workspaces": busy_workspaces,
            }
        finally:
            conn.close()

    def get_workspace_active_task(self, workspace_id: str) -> Optional[Dict[str, Any]]:
        """Return the task currently occupying workspace_id, if any."""
        if not workspace_id:
            return None
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                SELECT * FROM user_inbox_tasks
                WHERE workspace_id = ? AND status IN ('CLAIMED', 'RUNNING', 'WAITING')
                LIMIT 1
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
            return _decode_row(row) if row else None
        finally:
            conn.close()
