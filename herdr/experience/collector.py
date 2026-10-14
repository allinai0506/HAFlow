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
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (task_id, run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_collector_status
            ON local_collector_records (status)
        """)
    return conn


class ExperienceCollector:
    """Local machine experience collector and sync manager."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        hub_client: Optional[Any] = None,
        contributor: Optional[str] = None,
    ):
        self.db_path = db_path
        self.hub_client = hub_client
        self.contributor = contributor or os.environ.get("HERDR_EMPLOYEE_ID") or os.environ.get("USER") or "local_employee"

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

            # 1. Check local deduplication
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
                if not exp:
                    conn.execute("""
                        INSERT INTO local_collector_records
                        (task_id, run_id, outcome_id, experience_id, status, candidate_json, last_error, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id, run_id) DO UPDATE SET
                            status = excluded.status,
                            updated_at = excluded.updated_at
                    """, (task_id, run_id, outcome_id, None, "no_candidate", None, reason, now, now))
                    conn.commit()
                    return {"ok": True, "status": "no_candidate", "reason": reason}

                candidate_json = json.dumps(exp.to_dict(), ensure_ascii=False)
                # Save locally as pending_upload
                conn.execute("""
                    INSERT INTO local_collector_records
                    (task_id, run_id, outcome_id, experience_id, status, candidate_json, last_error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, run_id) DO UPDATE SET
                        experience_id = excluded.experience_id,
                        status = excluded.status,
                        candidate_json = excluded.candidate_json,
                        updated_at = excluded.updated_at
                """, (task_id, run_id, outcome_id, exp.experience_id, "pending_upload", candidate_json, None, now, now))
                conn.commit()
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

            conn = _get_collector_db(self.db_path)
            try:
                if upload_ok:
                    conn.execute("""
                        UPDATE local_collector_records
                        SET status = 'uploaded', last_error = NULL, updated_at = ?
                        WHERE task_id = ? AND run_id = ?
                    """, (time.time(), task_id, run_id))
                    conn.commit()
                    return {"ok": True, "status": "uploaded", "experience_id": exp.experience_id, "reason": "ready"}
                else:
                    conn.execute("""
                        UPDATE local_collector_records
                        SET last_error = ?, updated_at = ?
                        WHERE task_id = ? AND run_id = ?
                    """, (upload_err, time.time(), task_id, run_id))
                    conn.commit()
                    return {"ok": True, "status": "pending_upload", "experience_id": exp.experience_id, "reason": upload_err or "hub_offline"}
            finally:
                conn.close()

        except Exception as exc:
            LOGGER.error("process_task_settlement crashed unexpectedly: %s", exc)
            return {"ok": False, "status": "error", "reason": str(exc)}

    def process_task_settlement_async(
        self,
        task_id: str,
        outcome: Optional[Dict[str, Any]] = None,
        state_db_path: Optional[Path] = None,
    ) -> threading.Thread:
        """Asynchronously process task settlement in a daemon thread.

        Guaranteed not to block caller or raise exception.
        """
        thread = threading.Thread(
            target=self.process_task_settlement,
            args=(task_id, outcome, state_db_path),
            daemon=True,
            name=f"collector-{task_id}",
        )
        thread.start()
        return thread

    def retry_pending_uploads(self) -> Dict[str, Any]:
        """Retry pending uploads that were delayed due to Hub offline or network errors."""
        if not self.hub_client:
            return {"retried": 0, "succeeded": 0, "failed": 0, "reason": "no_hub_client"}

        conn = _get_collector_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM local_collector_records WHERE status = 'pending_upload'"
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
                if res.get("ok"):
                    succeeded += 1
                    c = _get_collector_db(self.db_path)
                    try:
                        c.execute(
                            "UPDATE local_collector_records SET status = 'uploaded', last_error = NULL, updated_at = ? WHERE task_id = ? AND run_id = ?",
                            (time.time(), row["task_id"], row["run_id"]),
                        )
                        c.commit()
                    finally:
                        c.close()
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                LOGGER.debug("Retry upload failed for %s: %s", row["task_id"], e)

        return {"retried": retried, "succeeded": succeeded, "failed": failed}
