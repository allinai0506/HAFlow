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
                state_db_path TEXT,
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
        if "state_db_path" not in existing_cols:
            conn.execute("ALTER TABLE local_collector_records ADD COLUMN state_db_path TEXT")
    return conn


def _absolute_db_path(p) -> str:
    """Bind a database path lexically without resolving filesystem links."""
    return os.path.abspath(os.path.expanduser(str(p)))


def _resolved_db_str(db_path: Optional[Path]) -> Optional[str]:
    """Persist the concrete state database identity, not just None-vs-set.

    Recovery (Sentinel background sync) runs without an explicit path, so a
    row must carry which database holds its task or recovery resolves the
    default database and quarantines a valid row as task_not_found.
    """
    if db_path is None:
        try:
            return _absolute_db_path(state_db.get_default_db_path())
        except Exception:
            return None
    return _absolute_db_path(db_path)


def _row_state_db_path(row) -> Optional[Path]:
    """Recover the per-row state database recorded at enqueue time."""
    try:
        stored = row["state_db_path"]
    except (IndexError, KeyError):
        return None
    if not stored:
        return None
    try:
        return Path(stored)
    except TypeError:
        return None


def _note_epoch(note: Dict[str, Any]) -> float:
    for key in ("ts", "raised_at", "reviewed_at", "created_at"):
        try:
            val = note.get(key)
            if val is not None:
                return float(val)
        except (TypeError, ValueError):
            continue
    return 0.0


def fold_decisions_by_id(notes: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Fold append-only decision notes: newest note per decision_id wins.

    Notes without a decision_id are kept as-is (no identity to fold on).
    Callers must run `workflow_docs.annotate_notes` on the FULL note stream
    first: annotating a pre-filtered subset would miss invalidation records
    and silently clear stale flags.
    """
    if not notes:
        return []
    latest: Dict[str, Dict[str, Any]] = {}
    kept: List[Dict[str, Any]] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        did = str(note.get("decision_id") or "").strip()
        if not did:
            kept.append(note)
            continue
        prev = latest.get(did)
        if prev is None or _note_epoch(note) >= _note_epoch(prev):
            latest[did] = note
    kept.extend(latest.values())
    return kept


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
                from .client import ExperienceClient
                self.hub_client = ExperienceClient()
            except Exception as exc:
                LOGGER.debug("Could not auto-initialize ExperienceClient: %s", exc)
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
            resolved_db = _resolved_db_str(state_db_path)
            task = state_db.get_task(task_id, db_path=Path(resolved_db) if resolved_db else None)
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
                        (task_id, run_id, outcome_id, experience_id, status, outcome_json, state_db_path, retry_count, next_retry_at, created_at, updated_at)
                        VALUES (?, ?, ?, NULL, 'pending_collection', ?, ?, 0, 0, ?, ?)
                        ON CONFLICT(task_id, run_id) DO UPDATE SET
                            outcome_id = COALESCE(excluded.outcome_id, local_collector_records.outcome_id),
                            outcome_json = COALESCE(excluded.outcome_json, local_collector_records.outcome_json),
                            state_db_path = COALESCE(excluded.state_db_path, local_collector_records.state_db_path),
                            updated_at = excluded.updated_at
                        WHERE local_collector_records.status NOT IN ('uploaded', 'no_candidate')
                    """, (task_id, run_id, outcome_id, outcome_json,
                          resolved_db, now, now))
                return {"ok": True, "status": "pending_collection", "task_id": task_id, "run_id": run_id}
            finally:
                conn.close()
        except Exception as exc:
            LOGGER.error("Failed to enqueue task settlement for %s: %s", task_id, exc)
            return {"ok": False, "status": "error", "reason": str(exc)}

    def _note_collection_failure(
        self,
        task_id: str,
        error: str,
        run_id: Optional[str] = None,
        terminal: bool = False,
    ) -> None:
        """Advance a stuck collection row so one bad record cannot pin the queue.

        Non-terminal failures keep `pending_collection` but schedule backoff
        (process_pending skips unripe rows); terminal ones (e.g. task gone)
        move to `error` and leave the queue for good. Never raises.
        """
        if not run_id:
            LOGGER.error("collection failure note without run identity: task=%s", task_id)
            return
        try:
            conn = _get_collector_db(self.db_path)
            try:
                with conn:
                    rows = conn.execute(
                        "SELECT run_id, retry_count FROM local_collector_records "
                        "WHERE task_id = ? AND run_id = ? AND status = 'pending_collection'",
                        (str(task_id), str(run_id)),
                    ).fetchall()
                    for r in rows:
                        if terminal:
                            conn.execute(
                                "UPDATE local_collector_records SET status = 'error', "
                                "last_error = ?, updated_at = ? WHERE task_id = ? AND run_id = ?",
                                (error[:300], time.time(), str(task_id), r["run_id"]),
                            )
                        else:
                            nxt = int(r["retry_count"] or 0) + 1
                            conn.execute(
                                "UPDATE local_collector_records SET last_error = ?, retry_count = ?, "
                                "next_retry_at = ?, updated_at = ? WHERE task_id = ? AND run_id = ?",
                                (error[:300], nxt, time.time() + min(300.0, 5.0 * (2 ** nxt)),
                                 time.time(), str(task_id), r["run_id"]),
                            )
            finally:
                conn.close()
        except Exception as exc:
            LOGGER.debug("Could not advance collection row for %s: %s", task_id, exc)

    def process_task_settlement(
        self,
        task_id: str,
        outcome: Optional[Dict[str, Any]] = None,
        state_db_path: Optional[Path] = None,
        queued_run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process a settled task outcome. Idempotent and fail-safe.

        queued_run_id is the run identity carried by the queue row being
        recovered; it scopes terminal failures so one missing source task
        cannot mark every pending row of the same task id, and it guards
        against cross-run settlement when the task id was reused for a newer
        run (a run mismatch fails closed before any extraction or write).

        Returns status dict:
          {"ok": bool, "status": str, "experience_id": Optional[str], "reason": str}
        """
        bound_run_id = str(queued_run_id) if queued_run_id else None
        run_id = None
        try:
            task = state_db.get_task(task_id, db_path=state_db_path)
            if not task:
                # Scope the terminal update to this row: other pending rows for
                # the same task id may point at another run or another database.
                self._note_collection_failure(str(task_id), "task_not_found",
                                              run_id=str(queued_run_id) if queued_run_id else None,
                                              terminal=True)
                return {"ok": False, "status": "skipped", "reason": "task_not_found"}

            # Identity isolation (Codex review P1): the queue row names the run
            # whose outcome it holds. When the task id has been reused for a
            # newer run, the task no longer carries this row's facts; settling
            # under the current run would upload a cross-run candidate and
            # strand the queued row. Fail closed before any extraction or
            # write, scoped to the queued run.
            task_run_id = str(task.get("run_id") or "")
            if bound_run_id and task_run_id and task_run_id != bound_run_id:
                self._note_collection_failure(str(task_id), "run_identity_mismatch",
                                              run_id=bound_run_id, terminal=True)
                return {"ok": False, "status": "skipped", "reason": "run_identity_mismatch"}

            run_id = str(task.get("run_id") or (outcome.get("run_id") if outcome else "") or f"run_{task_id}")
            outcome_id = outcome.get("outcome_id") if outcome else None

            # 1. Check local deduplication and recover outcome if omitted
            conn = _get_collector_db(self.db_path)
            existing_retry = 0
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
                    # Respect upload backoff: repeated settlement must not hammer Hub.
                    if existing_status == "pending_upload":
                        try:
                            existing_retry = int(row["retry_count"] or 0)
                        except Exception:
                            existing_retry = 0
                        nxt = row["next_retry_at"] if "next_retry_at" in row.keys() else 0
                        if nxt and float(nxt) > time.time():
                            return {
                                "ok": True,
                                "status": "pending_upload",
                                "experience_id": row["experience_id"],
                                "reason": "backoff",
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
                    # Annotate the FULL stream first: invalidation records are
                    # kind=invalidation and would be lost by early filtering.
                    annotated = workflow_docs.annotate_notes(all_notes)
                    scoped = [
                        n for n in annotated
                        if n.get("kind") == "decision" and (not n.get("task_id") or str(n.get("task_id")) == task_id)
                    ]
                    decisions = fold_decisions_by_id(scoped)
                except Exception as e:
                    LOGGER.debug("load_notes failed: %s", e)

            # 3. Extract candidate
            exp, reason = extract_candidate_from_facts(
                task=task,
                outcome=outcome,
                steers=steers,
                decisions=decisions,
                contributor=self.contributor,
                state_db_path=state_db_path,
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
                            WHERE local_collector_records.status NOT IN ('uploaded', 'no_candidate')
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
                        WHERE local_collector_records.status NOT IN ('uploaded', 'no_candidate')
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
                        cur = conn.execute("""
                            UPDATE local_collector_records
                            SET status = 'uploaded', last_error = NULL, retry_count = 0, next_retry_at = 0, updated_at = ?
                            WHERE task_id = ? AND run_id = ? AND experience_id = ?
                        """, (time.time(), task_id, run_id, exp.experience_id))
                        if cur.rowcount:
                            return {"ok": True, "status": "uploaded", "experience_id": exp.experience_id, "reason": "ready"}
                        current = conn.execute(
                            "SELECT status, experience_id FROM local_collector_records WHERE task_id = ? AND run_id = ?",
                            (task_id, run_id),
                        ).fetchone()
                        if current is None:
                            return {"ok": True, "status": "uploaded", "experience_id": exp.experience_id, "reason": "ready"}
                        return {"ok": True, "status": current["status"],
                                "experience_id": current["experience_id"], "reason": "superseded"}
                    else:
                        # Every settlement failure schedules backoff immediately:
                        # repeated task/eval writes must not each trigger an
                        # upload. Explicit forced recovery stays the immediate
                        # retry mechanism.
                        curr_retries = existing_retry + 1
                        backoff = min(300.0, 5.0 * (2 ** curr_retries))
                        conn.execute("""
                            UPDATE local_collector_records
                            SET status = 'pending_upload', last_error = ?, retry_count = ?, next_retry_at = ?, updated_at = ?
                            WHERE task_id = ? AND run_id = ? AND status != 'uploaded'
                        """, (upload_err, curr_retries, time.time() + backoff, time.time(), task_id, run_id))
                        return {"ok": True, "status": "pending_upload", "experience_id": exp.experience_id, "reason": upload_err}
            finally:
                conn.close()

        except Exception as exc:
            failure_run_id = bound_run_id or run_id
            LOGGER.error("process_task_settlement crashed unexpectedly: task=%s run=%s: %s",
                         task_id, failure_run_id or "unknown", exc)
            self._note_collection_failure(str(task_id), str(exc), run_id=failure_run_id)
            return {"ok": False, "status": "error", "reason": str(exc)}

    def process_pending(self, state_db_path: Optional[Path] = None, limit: int = 50) -> Dict[str, Any]:
        """Process tasks queued as pending_collection.

        Rows in backoff (next_retry_at in the future) are skipped so one bad
        record cannot pin the head of the queue. Returns processed/failed
        counts; failures keep their rows (with backoff) and must be surfaced
        by callers instead of reported as success.
        """
        now = time.time()
        conn = _get_collector_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT task_id, run_id, outcome_json, state_db_path FROM local_collector_records "
                "WHERE status = 'pending_collection' AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (now, limit,),
            ).fetchall()
        finally:
            conn.close()

        processed = 0
        failed = 0
        task_ids: List[str] = []
        for row in rows:
            tid = row["task_id"]
            outcome = None
            if row["outcome_json"]:
                try:
                    outcome = json.loads(row["outcome_json"])
                except Exception:
                    pass
            # The row's own database identity wins: a worker recovering
            # database A must never read database B's rows from A, which would
            # report task_not_found and quarantine valid work. The caller path
            # is only a fallback for rows written before the column existed.
            row_db = _row_state_db_path(row) or state_db_path
            if row_db and not Path(row_db).exists():
                self._note_collection_failure(tid, "state_db_missing", run_id=row["run_id"],
                                              terminal=False)
                failed += 1
                task_ids.append(str(tid))
                continue
            res = self.process_task_settlement(tid, outcome=outcome, state_db_path=row_db,
                                               queued_run_id=row["run_id"])
            if isinstance(res, dict) and res.get("ok"):
                processed += 1
            else:
                failed += 1
            task_ids.append(str(tid))
        return {"processed": processed, "failed": failed, "task_ids": task_ids}

    def retry_pending_uploads(self, limit: int = 50, force: bool = False,
                              exclude_task_id: Optional[str] = None,
                              exclude_task_ids: Optional[List[str]] = None,
                              exclude_rows: Optional[set] = None) -> Dict[str, Any]:
        """Retry pending uploads that were delayed due to Hub offline or network errors."""
        if not self.hub_client:
            return {"retried": 0, "succeeded": 0, "failed": 0, "reason": "no_hub_client"}
        excluded = {str(exclude_task_id)} if exclude_task_id else set()
        excluded |= {str(t) for t in (exclude_task_ids or [])}

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
            if excluded:
                rows = [r for r in rows if str(r["task_id"]) not in excluded]
            if exclude_rows:
                rows = [r for r in rows if (str(r["task_id"]), str(r["run_id"])) not in exclude_rows]
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
                            cur = c.execute(
                                """
                                UPDATE local_collector_records
                                SET status = 'uploaded', last_error = NULL, retry_count = 0, next_retry_at = 0, updated_at = ?
                                WHERE task_id = ? AND run_id = ? AND experience_id = ?
                                """,
                                (time.time(), row["task_id"], row["run_id"], exp.experience_id),
                            )
                            if cur.rowcount:
                                succeeded += 1
                            else:
                                LOGGER.debug("Upload superseded for %s; row changed concurrently", row["task_id"])
                        else:
                            failed += 1
                            err = res.get("error") or "upload_rejected"
                            curr_retries = int(row["retry_count"] or 0) + 1
                            backoff = min(300.0, 5.0 * (2 ** curr_retries))
                            c.execute(
                                """
                                UPDATE local_collector_records
                                SET last_error = ?, retry_count = ?, next_retry_at = ?, updated_at = ?
                                WHERE task_id = ? AND run_id = ? AND status != 'uploaded'
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
                            WHERE task_id = ? AND run_id = ? AND status != 'uploaded'
                            """,
                            (str(e), curr_retries, time.time() + backoff, time.time(), row["task_id"], row["run_id"]),
                        )
                finally:
                    c.close()
                LOGGER.debug("Retry upload failed for %s: %s", row["task_id"], e)

        return {"retried": retried, "succeeded": succeeded, "failed": failed,
                "attempted": [(str(r["task_id"]), str(r["run_id"])) for r in rows]}

    def recover_and_sync(self, state_db_path: Optional[Path] = None, force: bool = False,
                           exclude_task_id: Optional[str] = None) -> Dict[str, Any]:
        """Drains pending collections and retries pending uploads. Safe to call periodically.

        Rows collected in this same pass are excluded from the immediate retry
        (unless force drains everything): their first failure stays
        immediately eligible, and retrying them here would double every Hub call.
        """
        with self._lock:
            pending_stats = self.process_pending(state_db_path=state_db_path)
            same_pass = [str(t) for t in (pending_stats.get("task_ids") or [])]
            if exclude_task_id:
                same_pass.append(str(exclude_task_id))
            upload_stats = {"retried": 0, "succeeded": 0, "failed": 0}
            if force:
                # Explicit operator sync drains every batch (bounded loop), but
                # each row is attempted only once per invocation: forced retries
                # ignore backoff, so without this a failing row would be
                # re-selected on every iteration (retry storm).
                attempted: set = set()
                for _ in range(20):
                    batch = self.retry_pending_uploads(force=True, exclude_rows=attempted)
                    upload_stats["retried"] += batch.get("retried", 0)
                    upload_stats["succeeded"] += batch.get("succeeded", 0)
                    upload_stats["failed"] += batch.get("failed", 0)
                    attempted |= {(str(t), str(r)) for t, r in batch.get("attempted", [])}
                    if batch.get("retried", 0) == 0:
                        upload_stats["reason"] = batch.get("reason") or ""
                        break
                else:
                    upload_stats["reason"] = ""
            else:
                upload_stats = self.retry_pending_uploads(exclude_task_ids=same_pass)
            remaining = 0
            pending_remaining = 0
            quarantined = 0
            conn = _get_collector_db(self.db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM local_collector_records WHERE status = 'pending_upload'"
                ).fetchone()
                remaining = int(row["n"] or 0) if row else 0
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM local_collector_records WHERE status = 'pending_collection'"
                ).fetchone()
                pending_remaining = int(row["n"] or 0) if row else 0
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM local_collector_records WHERE status = 'error'"
                ).fetchone()
                quarantined = int(row["n"] or 0) if row else 0
            except Exception:
                pass
            finally:
                conn.close()
            return {
                "ok": True,
                "pending_collected": pending_stats.get("processed", 0),
                "pending_collection_failed": pending_stats.get("failed", 0),
                "pending_collection_remaining": pending_remaining,
                "pending_collection_error": quarantined,
                "retried": upload_stats.get("retried", 0),
                "succeeded": upload_stats.get("succeeded", 0),
                "failed": upload_stats.get("failed", 0),
                "reason": upload_stats.get("reason") or "",
                "pending_upload_remaining": remaining,
            }

    def process_task_settlement_async(
        self,
        task_id: str,
        outcome: Optional[Dict[str, Any]] = None,
        state_db_path: Optional[Path] = None,
    ) -> Optional[threading.Thread]:
        """Asynchronously process task settlement in a daemon thread.

        Per-task single-flight: repeated settlement writes while a worker is
        still running are skipped (returns None) instead of stacking threads
        and duplicating Hub I/O. Guaranteed not to block caller or raise.
        """
        with _settlement_lock:
            if str(task_id) in _settlement_inflight:
                LOGGER.debug("Settlement worker already running for %s; skipping", task_id)
                return None
            _settlement_inflight.add(str(task_id))

        def _worker():
            try:
                self.process_task_settlement(task_id, outcome, state_db_path)
                # Exclude the just-attempted task: its first failure stays
                # immediately eligible, and an unconditional recover here would
                # re-submit it a second time before any backoff.
                self.recover_and_sync(state_db_path, exclude_task_id=str(task_id))
            except Exception as exc:
                LOGGER.debug("Async collector worker error: %s", exc)
            finally:
                with _settlement_lock:
                    _settlement_inflight.discard(str(task_id))

        thread = threading.Thread(
            target=_worker,
            daemon=True,
            name=f"collector-{task_id}",
        )
        thread.start()
        return thread

    def collect_and_sync_lessons(
        self,
        lessons_path: Optional[Path] = None,
        min_lesson_num: int = 160,
        sync_to_hub: bool = True,
    ) -> Dict[str, Any]:
        """Extract and sync corporate business rules from lessons-learned.md."""
        if lessons_path:
            target = Path(lessons_path)
        else:
            repo_root = Path(__file__).resolve().parent.parent.parent
            target = repo_root / "docs" / "lessons" / "lessons-learned.md"
        if not target.is_file():
            return {"ok": False, "reason": "lessons_file_missing"}
        try:
            from .extractor import extract_candidates_from_lessons
            content = target.read_text(encoding="utf-8")
            candidates = extract_candidates_from_lessons(content, contributor=self.contributor, min_lesson_num=min_lesson_num)
            uploaded = 0
            failed = 0
            skipped = 0
            now = time.time()
            conn = _get_collector_db(self.db_path)
            try:
                for exp in candidates:
                    # 已上传去重（PR #377）：DB 行 status='uploaded' 的候选
                    # 不再重复提交——旧行为每次调用全量重发（50 requests）。
                    existing = conn.execute(
                        "SELECT status FROM local_collector_records WHERE task_id = ? AND run_id = ?",
                        (exp.source.task_id, exp.source.run_id),
                    ).fetchone()
                    if existing and existing["status"] == "uploaded":
                        skipped += 1
                        continue
                    cand_json = json.dumps(exp.to_dict(), ensure_ascii=False)
                    with conn:
                        conn.execute("""
                            INSERT INTO local_collector_records
                            (task_id, run_id, outcome_id, experience_id, status, candidate_json, created_at, updated_at)
                            VALUES (?, ?, NULL, ?, 'pending_upload', ?, ?, ?)
                            ON CONFLICT(task_id, run_id) DO UPDATE SET
                                candidate_json = excluded.candidate_json,
                                updated_at = excluded.updated_at
                            WHERE local_collector_records.status != 'uploaded'
                        """, (exp.source.task_id, exp.source.run_id, exp.experience_id, cand_json, now, now))
                    # close 路径 local-only（sync_to_hub=False）：只本地持久化
                    # pending，Hub 上传交给后台单飞 drain（Hub 不可达时 close
                    # 不被 8s×N 请求超时拖住）。
                    if self.hub_client and sync_to_hub:
                        try:
                            res = self.hub_client.submit_candidate(exp)
                            if isinstance(res, dict) and res.get("ok"):
                                with conn:
                                    conn.execute(
                                        "UPDATE local_collector_records SET status = 'uploaded', updated_at = ? WHERE task_id = ? AND run_id = ?",
                                        (now, exp.source.task_id, exp.source.run_id)
                                    )
                                uploaded += 1
                            else:
                                err_msg = (res.get("error") if isinstance(res, dict) else str(res)) or "submit_rejected"
                                LOGGER.debug("Upload rejected for lesson %s: %s", exp.experience_id, err_msg)
                                failed += 1
                        except Exception as e:
                            LOGGER.debug("Upload failed for lesson %s: %s", exp.experience_id, e)
                            failed += 1
            finally:
                conn.close()
            return {"ok": True, "total": len(candidates), "uploaded": uploaded,
                    "failed": failed, "skipped": skipped}
        except Exception as exc:
            LOGGER.error("collect_and_sync_lessons failed: %s", exc)
            return {"ok": False, "reason": str(exc)}

    def collect_workflow_tasks(
        self,
        workflow_id: str,
        state_db_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Enqueue settlement for all terminal tasks in a workflow."""
        try:
            conn = state_db.get_db_connection(state_db_path)
            try:
                rows = conn.execute(
                    "SELECT task_id, status FROM tasks WHERE workflow_id = ? ORDER BY created_at",
                    (str(workflow_id),),
                ).fetchall()
            finally:
                conn.close()
            enqueued = 0
            for r in rows:
                tid = r["task_id"]
                st = r["status"]
                if st in ("completed", "cleaned"):
                    self.enqueue_task_settlement(tid, state_db_path=state_db_path)
                    enqueued += 1
            return {"ok": True, "workflow_id": workflow_id, "enqueued": enqueued}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}


_bg_sync_lock = threading.Lock()
_bg_sync_inflight = False
_settlement_lock = threading.Lock()
_settlement_inflight: set = set()


def trigger_background_sync(state_db_path: Optional[Path] = None) -> bool:
    """Start one bounded daemon sync; skip while another is in flight.

    Single-flight guard for dispatch/sentinel hot paths: returns True when a
    worker was started, False when a previous sync is still running.
    """
    global _bg_sync_inflight
    with _bg_sync_lock:
        if _bg_sync_inflight:
            return False
        _bg_sync_inflight = True

    def _worker():
        global _bg_sync_inflight
        try:
            ExperienceCollector().recover_and_sync(state_db_path=state_db_path)
        except Exception as exc:
            LOGGER.debug("Background experience sync failed: %s", exc)
        finally:
            with _bg_sync_lock:
                _bg_sync_inflight = False

    threading.Thread(target=_worker, daemon=True, name="exp-sync-bg").start()
    return True
