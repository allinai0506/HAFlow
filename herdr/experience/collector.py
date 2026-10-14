"""herdr.experience.collector — Local Experience Collector & Sync Engine.

Responsible for:
- Detecting settled tasks on local machine.
- Extracting candidate business rules via extractor.
- Local idempotency & state tracking in local SQLite DB.
- Offline buffering and retry mechanism when Hub is temporarily unreachable.
- 100% fail-open: never blocks or crashes task settlement or runtime flow.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr import state_db
from .extractor import extract_candidate_from_facts
from .models import BusinessRuleExperience

LOGGER = logging.getLogger("herdr.experience.collector")

DEFAULT_COLLECTOR_DB_PATH = Path.home() / ".herdr-controller" / "experience_collector.db"


def _get_collector_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target = db_path or Path(os.environ.get("HERDR_COLLECTOR_DB_PATH") or DEFAULT_COLLECTOR_DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS local_collector_records (
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                outcome_id TEXT,
                experience_id TEXT,
                status TEXT NOT NULL,
                candidate_json TEXT,
                outcome_json TEXT,
                last_error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (task_id, run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_collector_status
            ON local_collector_records (status)
        """)
        # Schema migration for existing databases
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(local_collector_records)").fetchall()}
        if "outcome_json" not in existing_cols:
            conn.execute("ALTER TABLE local_collector_records ADD COLUMN outcome_json TEXT")
        if "retry_count" not in existing_cols:
            conn.execute("ALTER TABLE local_collector_records ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")
        if "next_retry_at" not in existing_cols:
            conn.execute("ALTER TABLE local_collector_records ADD COLUMN next_retry_at REAL NOT NULL DEFAULT 0")
    return conn


class ExperienceCollector:
    """Local machine experience collector and sync manager."""

    _lock = threading.Lock()

    def __init__(
        self,
        db_path: Optional[Path] = None,
        hub_client: Optional[Any] = None,
        contributor: Optional[str] = None,
    ):
        self.db_path = db_path
        if hub_client is not None:
            self.hub_client = hub_client
        elif os.environ.get("HERDR_HUB_URL") or os.environ.get("HERDR_HUB_TOKEN"):
            try:
                from .client import ExperienceHubClient
                self.hub_client = ExperienceHubClient()
            except Exception as exc:
                LOGGER.debug("Could not auto-initialize ExperienceHubClient: %s", exc)
                self.hub_client = None
        else:
            self.hub_client = None

        self.contributor = contributor or os.environ.get("HERDR_EMPLOYEE_ID") or os.environ.get("USER") or "local_employee"

    def enqueue_task_settlement(
        self,
        task_id: str,
        outcome: Optional[Dict[str, Any]] = None,
        state_db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Synchronously enqueue task settlement into durable storage before async work.

        Guarantees that pending collection survives process exit or daemon thread termination.
        """
        try:
            task = state_db.get_task(task_id, db_path=state_db_path)
            run_id = str((task.get("run_id") if task else None) or (outcome.get("run_id") if outcome else "") or f"run_{task_id}")
            outcome_id = outcome.get("outcome_id") if outcome else None

            conn = _get_collector_db(self.db_path)
            try:
                with conn:
                    row = conn.execute(
                        "SELECT status, experience_id FROM local_collector_records WHERE task_id = ? AND run_id = ?",
                        (task_id, run_id),
                    ).fetchone()
                    if row and row["status"] in {"uploaded", "no_candidate"}:
                        return {
                            "ok": True,
                            "status": row["status"],
                            "experience_id": row["experience_id"],
                            "reason": "already_processed",
                        }

                    now = time.time()
                    outcome_json = json.dumps(outcome, ensure_ascii=False) if outcome else None
                    conn.execute("""
                        INSERT INTO local_collector_records
                        (task_id, run_id, outcome_id, experience_id, status, outcome_json, retry_count, next_retry_at, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, 'pending_collection', ?, 0, 0, ?, ?)
                        ON CONFLICT(task_id, run_id) DO UPDATE SET
                            outcome_id = COALESCE(excluded.outcome_id, local_collector_records.outcome_id),
                            outcome_json = COALESCE(excluded.outcome_json, local_collector_records.outcome_json),
                            updated_at = excluded.updated_at
                        WHERE local_collector_records.status NOT IN ('uploaded', 'no_candidate')
                    """, (task_id, run_id, outcome_id, outcome_json, now, now))
                return {"ok": True, "status": "pending_collection", "task_id": task_id, "run_id": run_id}
            finally:
                conn.close()
        except Exception as exc:
            LOGGER.error("Failed to enqueue task settlement for %s: %s", task_id, exc)
            return {"ok": False, "status": "error", "reason": str(exc)}

    def process_task_settlement(
        self,
        task_id: str,
        outcome: Optional[Dict[str, Any]] = None,
        state_db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Process a settled task outcome. Idempotent and fail-safe.

        Returns status dict:
          {"ok": bool, "status": str, "experience_id": Optional[str], "reason": str}
        """
        try:
            task = state_db.get_task(task_id, db_path=state_db_path)
            if not task:
                return {"ok": False, "status": "skipped", "reason": "task_not_found"}

            run_id = str(task.get("run_id") or (outcome.get("run_id") if outcome else "") or f"run_{task_id}")
            outcome_id = outcome.get("outcome_id") if outcome else None

            # 1. Check local deduplication and recover outcome if omitted
            conn = _get_collector_db(self.db_path)
            try:
                row = conn.execute(
                    "SELECT * FROM local_collector_records WHERE task_id = ? AND run_id = ?",
                    (task_id, run_id),
                ).fetchone()
                if row:
                    existing_status = row["status"]
                    if existing_status in {"uploaded", "no_candidate"}:
                        return {
                            "ok": True,
                            "status": existing_status,
                            "experience_id": row["experience_id"],
                            "reason": "already_processed",
                        }
                    if outcome is None and row["outcome_json"]:
                        try:
                            outcome = json.loads(row["outcome_json"])
                        except Exception:
                            pass
            finally:
                conn.close()

            # 2. Gather steering, rework, and human decision facts
            steers = []
            try:
                steers = state_db.list_steers(task_id=task_id, db_path=state_db_path)
            except Exception as e:
                LOGGER.debug("list_steers failed: %s", e)

            decisions = []
            workflow_id = task.get("workflow_id")
            if workflow_id:
                try:
                    from herdr import workflow_docs
                    all_notes = workflow_docs.load_notes(workflow_id)
                    decisions = [
                        n for n in all_notes
                        if n.get("kind") == "decision" and (not n.get("task_id") or str(n.get("task_id")) == task_id)
                    ]
                except Exception as e:
                    LOGGER.debug("load_notes failed: %s", e)

            # 3. Extract candidate
            exp, reason = extract_candidate_from_facts(
                task=task,
                outcome=outcome,
                steers=steers,
                decisions=decisions,
                contributor=self.contributor,
            )

            now = time.time()
            conn = _get_collector_db(self.db_path)
            try:
                with conn:
                    if not exp:
                        conn.execute("""
                            INSERT INTO local_collector_records
                            (task_id, run_id, outcome_id, experience_id, status, candidate_json, last_error, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(task_id, run_id) DO UPDATE SET
                                status = excluded.status,
                                last_error = excluded.last_error,
                                updated_at = excluded.updated_at
                        """, (task_id, run_id, outcome_id, None, "no_candidate", None, reason, now, now))
                        return {"ok": True, "status": "no_candidate", "reason": reason}

                    candidate_json = json.dumps(exp.to_dict(), ensure_ascii=False)
                    conn.execute("""
                        INSERT INTO local_collector_records
                        (task_id, run_id, outcome_id, experience_id, status, candidate_json, last_error, retry_count, next_retry_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                        ON CONFLICT(task_id, run_id) DO UPDATE SET
                            experience_id = excluded.experience_id,
                            status = excluded.status,
                            candidate_json = excluded.candidate_json,
                            updated_at = excluded.updated_at
                    """, (task_id, run_id, outcome_id, exp.experience_id, "pending_upload", candidate_json, None, now, now))
            finally:
                conn.close()

            # 4. Attempt upload if client is available
            upload_ok = False
            upload_err = None
            if self.hub_client:
                try:
                    upload_res = self.hub_client.submit_candidate(exp)
                    if upload_res.get("ok"):
                        upload_ok = True
                    else:
                        upload_err = upload_res.get("error") or "upload_rejected"
                except Exception as exc:
                    upload_err = str(exc)
                    LOGGER.warning("Experience Hub upload failed for %s: %s", exp.experience_id, exc)
            else:
                upload_err = "no_hub_client"

            conn = _get_collector_db(self.db_path)
            try:
                with conn:
                    if upload_ok:
                        conn.execute("""
                            UPDATE local_collector_records
                            SET status = 'uploaded', last_error = NULL, retry_count = 0, next_retry_at = 0, updated_at = ?
                            WHERE task_id = ? AND run_id = ?
                        """, (time.time(), task_id, run_id))
                        return {"ok": True, "status": "uploaded", "experience_id": exp.experience_id, "reason": "ready"}
                    else:
                        conn.execute("""
                            UPDATE local_collector_records
                            SET status = 'pending_upload', last_error = ?, retry_count = 0, next_retry_at = 0, updated_at = ?
                            WHERE task_id = ? AND run_id = ?
                        """, (upload_err, time.time(), task_id, run_id))
                        return {"ok": True, "status": "pending_upload", "experience_id": exp.experience_id, "reason": upload_err}
            finally:
                conn.close()

        except Exception as exc:
            LOGGER.error("process_task_settlement crashed unexpectedly: %s", exc)
            return {"ok": False, "status": "error", "reason": str(exc)}

    def process_pending(self, state_db_path: Optional[Path] = None, limit: int = 50) -> int:
        """Process tasks queued as pending_collection."""
        conn = _get_collector_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT task_id, run_id, outcome_json FROM local_collector_records WHERE status = 'pending_collection' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        processed = 0
        for row in rows:
            tid = row["task_id"]
            outcome = None
            if row["outcome_json"]:
                try:
                    outcome = json.loads(row["outcome_json"])
                except Exception:
                    pass
            self.process_task_settlement(tid, outcome=outcome, state_db_path=state_db_path)
            processed += 1
        return processed

    def retry_pending_uploads(self, limit: int = 50, force: bool = False) -> Dict[str, Any]:
        """Retry pending uploads that were delayed due to Hub offline or network errors."""
        if not self.hub_client:
            return {"retried": 0, "succeeded": 0, "failed": 0, "reason": "no_hub_client"}

        now = time.time()
        conn = _get_collector_db(self.db_path)
        try:
            if force:
                rows = conn.execute(
                    """
                    SELECT * FROM local_collector_records
                    WHERE status = 'pending_upload'
                    ORDER BY updated_at ASC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM local_collector_records
                    WHERE status = 'pending_upload' AND (next_retry_at <= ? OR next_retry_at IS NULL)
                    ORDER BY updated_at ASC LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
        finally:
            conn.close()

        retried = len(rows)
        succeeded = 0
        failed = 0

        for row in rows:
            candidate_raw = row["candidate_json"]
            if not candidate_raw:
                continue
            try:
                candidate_data = json.loads(candidate_raw)
                exp = BusinessRuleExperience.from_dict(candidate_data)
                res = self.hub_client.submit_candidate(exp)
                c = _get_collector_db(self.db_path)
                try:
                    with c:
                        if res.get("ok"):
                            succeeded += 1
                            c.execute(
                                """
                                UPDATE local_collector_records
                                SET status = 'uploaded', last_error = NULL, retry_count = 0, next_retry_at = 0, updated_at = ?
                                WHERE task_id = ? AND run_id = ?
                                """,
                                (time.time(), row["task_id"], row["run_id"]),
                            )
                        else:
                            failed += 1
                            err = res.get("error") or "upload_rejected"
                            curr_retries = int(row["retry_count"] or 0) + 1
                            backoff = min(300.0, 5.0 * (2 ** curr_retries))
                            c.execute(
                                """
                                UPDATE local_collector_records
                                SET last_error = ?, retry_count = ?, next_retry_at = ?, updated_at = ?
                                WHERE task_id = ? AND run_id = ?
                                """,
                                (err, curr_retries, time.time() + backoff, time.time(), row["task_id"], row["run_id"]),
                            )
                finally:
                    c.close()
            except Exception as e:
                failed += 1
                curr_retries = int(row["retry_count"] or 0) + 1
                backoff = min(300.0, 5.0 * (2 ** curr_retries))
                c = _get_collector_db(self.db_path)
                try:
                    with c:
                        c.execute(
                            """
                            UPDATE local_collector_records
                            SET last_error = ?, retry_count = ?, next_retry_at = ?, updated_at = ?
                            WHERE task_id = ? AND run_id = ?
                            """,
                            (str(e), curr_retries, time.time() + backoff, time.time(), row["task_id"], row["run_id"]),
                        )
                finally:
                    c.close()
                LOGGER.debug("Retry upload failed for %s: %s", row["task_id"], e)

        return {"retried": retried, "succeeded": succeeded, "failed": failed}

    def recover_and_sync(self, state_db_path: Optional[Path] = None) -> Dict[str, Any]:
        """Drains pending collections and retries pending uploads. Safe to call periodically."""
        with self._lock:
            collected = self.process_pending(state_db_path=state_db_path)
            upload_stats = self.retry_pending_uploads()
            return {
                "ok": True,
                "pending_collected": collected,
                "retried": upload_stats.get("retried", 0),
                "succeeded": upload_stats.get("succeeded", 0),
                "failed": upload_stats.get("failed", 0),
            }

    def process_task_settlement_async(
        self,
        task_id: str,
        outcome: Optional[Dict[str, Any]] = None,
        state_db_path: Optional[Path] = None,
    ) -> threading.Thread:
        """Asynchronously process task settlement in a daemon thread.

        Guaranteed not to block caller or raise exception.
        """
        def _worker():
            try:
                self.process_task_settlement(task_id, outcome, state_db_path)
                self.recover_and_sync(state_db_path)
            except Exception as exc:
                LOGGER.debug("Async collector worker error: %s", exc)

        thread = threading.Thread(
            target=_worker,
            daemon=True,
            name=f"collector-{task_id}",
        )
        thread.start()
        return thread
