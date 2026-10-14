"""herdr.experience.hub_server — Enterprise Intranet Experience Hub Service.

Features:
- Standalone service, deployable on customer's intranet server.
- Independent SQLite database, fully decoupled from employee laptops.
- Bearer Token authentication & RBAC (contributor, reviewer, admin).
- Customer boundary protection (anti cross-customer access).
- Candidate submission (idempotent), review, publish, and revoke.
- Published rules read API (strictly filters status == 'published').
- Comprehensive audit trail.
- Pure Python standard library (http.server + sqlite3).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    AuditEvent,
    BusinessRuleExperience,
    ExperienceStatus,
    ExperienceType,
    ReviewInfo,
)

LOGGER = logging.getLogger("herdr.experience.hub")

DEFAULT_HUB_DB_PATH = Path.home() / ".herdr-controller" / "experience_hub.db"


def init_hub_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS experiences (
                experience_id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                contributor TEXT NOT NULL,
                customer TEXT,
                task_type TEXT,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                applies_to_json TEXT NOT NULL,
                source_json TEXT NOT NULL,
                review_json TEXT,
                audit_trail_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_exp_status_customer
            ON experiences (status, customer, task_type)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                allowed_customers_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                experience_id TEXT,
                details_json TEXT,
                timestamp REAL NOT NULL
            )
        """)
    return conn


class HubStorage:
    """Persistence and query manager for Experience Hub."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        init_hub_db(self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def register_token(
        self,
        token: str,
        user_id: str,
        role: str,
        allowed_customers: Optional[List[str]] = None,
    ) -> None:
        if allowed_customers is None:
            allowed_customers = ["*"]
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO auth_tokens (token, user_id, role, allowed_customers_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(token) DO UPDATE SET
                        user_id = excluded.user_id,
                        role = excluded.role,
                        allowed_customers_json = excluded.allowed_customers_json
                """, (token, user_id, role, json.dumps(allowed_customers), time.time()))
        finally:
            conn.close()

    def authenticate_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM auth_tokens WHERE token = ?", (token,)).fetchone()
            if not row:
                return None
            return {
                "token": row["token"],
                "user_id": row["user_id"],
                "role": row["role"],
                "allowed_customers": json.loads(row["allowed_customers_json"] or "[]"),
            }
        finally:
            conn.close()

    def record_audit(
        self,
        action: str,
        actor: str,
        experience_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO audit_logs (action, actor, experience_id, details_json, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """, (action, actor, experience_id, json.dumps(details or {}, ensure_ascii=False), time.time()))
        finally:
            conn.close()

    def save_candidate(self, exp: BusinessRuleExperience, actor: str) -> Tuple[bool, BusinessRuleExperience, str]:
        """Save a new candidate experience. Idempotent on experience_id."""
        conn = self._get_conn()
        try:
            existing = self.get_experience(exp.experience_id)
            if existing:
                return False, existing, "already_exists"

            exp_dict = exp.to_dict()
            with conn:
                conn.execute("""
                    INSERT INTO experiences (
                        experience_id, type, title, content, contributor,
                        customer, task_type, status, version,
                        applies_to_json, source_json, review_json, audit_trail_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    exp.experience_id,
                    exp.type,
                    exp.title,
                    exp.content,
                    exp.contributor,
                    exp.applies_to.customer,
                    exp.applies_to.task_type,
                    exp.status,
                    exp.version,
                    json.dumps(exp.applies_to.to_dict(), ensure_ascii=False),
                    json.dumps(exp.source.to_dict(), ensure_ascii=False),
                    json.dumps(exp.review.to_dict() if exp.review else None, ensure_ascii=False),
                    json.dumps([a.to_dict() for a in exp.audit_trail], ensure_ascii=False),
                    exp.created_at,
                    exp.updated_at,
                ))
            self.record_audit("submit_candidate", actor, exp.experience_id, {"title": exp.title})
            return True, exp, "created"
        finally:
            conn.close()

    def get_experience(self, experience_id: str) -> Optional[BusinessRuleExperience]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM experiences WHERE experience_id = ?", (experience_id,)).fetchone()
            if not row:
                return None
            data = {
                "experience_id": row["experience_id"],
                "type": row["type"],
                "title": row["title"],
                "content": row["content"],
                "contributor": row["contributor"],
                "status": row["status"],
                "version": row["version"],
                "applies_to": json.loads(row["applies_to_json"] or "{}"),
                "source": json.loads(row["source_json"] or "{}"),
                "review": json.loads(row["review_json"]) if row["review_json"] else None,
                "audit_trail": json.loads(row["audit_trail_json"] or "[]"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            return BusinessRuleExperience.from_dict(data)
        finally:
            conn.close()

    def list_candidates(
        self,
        status: Optional[str] = None,
        customer: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> List[BusinessRuleExperience]:
        conn = self._get_conn()
        try:
            query = "SELECT * FROM experiences WHERE 1=1"
            params: List[Any] = []
            if status:
                query += " AND status = ?"
                params.append(status)
            if customer:
                query += " AND (customer = ? OR customer IS NULL)"
                params.append(customer)
            if task_type:
                query += " AND (task_type = ? OR task_type IS NULL)"
                params.append(task_type)
            query += " ORDER BY created_at DESC"

            cur = conn.execute(query, tuple(params))
            results = []
            for row in cur.fetchall():
                data = {
                    "experience_id": row["experience_id"],
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "contributor": row["contributor"],
                    "status": row["status"],
                    "version": row["version"],
                    "applies_to": json.loads(row["applies_to_json"] or "{}"),
                    "source": json.loads(row["source_json"] or "{}"),
                    "review": json.loads(row["review_json"]) if row["review_json"] else None,
                    "audit_trail": json.loads(row["audit_trail_json"] or "[]"),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                results.append(BusinessRuleExperience.from_dict(data))
            return results
        finally:
            conn.close()

    def review_candidate(
        self,
        experience_id: str,
        action: str,  # 'approve' | 'reject'
        reviewer: str,
        comment: str = "",
    ) -> Tuple[bool, Optional[BusinessRuleExperience], str]:
        exp = self.get_experience(experience_id)
        if not exp:
            return False, None, "not_found"

        if exp.status not in {ExperienceStatus.PENDING_REVIEW.value, ExperienceStatus.REJECTED.value}:
            return False, exp, f"cannot_review_from_{exp.status}"

        now = time.time()
        new_status = (
            ExperienceStatus.PUBLISHED.value
            if action == "approve"
            else ExperienceStatus.REJECTED.value
        )

        review = ReviewInfo(
            reviewer=reviewer,
            reviewed_at=now,
            comment=comment or ("审核通过并发布" if action == "approve" else "审核驳回"),
        )
        audit_event = AuditEvent(
            action=f"review_{action}",
            actor=reviewer,
            timestamp=now,
            note=comment or f"规则审核: {action}",
        )

        exp.status = new_status
        exp.review = review
        exp.updated_at = now
        exp.audit_trail.append(audit_event)

        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    UPDATE experiences
                    SET status = ?, review_json = ?, audit_trail_json = ?, updated_at = ?
                    WHERE experience_id = ?
                """, (
                    exp.status,
                    json.dumps(review.to_dict(), ensure_ascii=False),
                    json.dumps([a.to_dict() for a in exp.audit_trail], ensure_ascii=False),
                    exp.updated_at,
                    exp.experience_id,
                ))
            self.record_audit(f"review_{action}", reviewer, experience_id, {"comment": comment})
            return True, exp, "ok"
        finally:
            conn.close()

    def revoke_rule(
        self,
        experience_id: str,
        actor: str,
        reason: str = "",
    ) -> Tuple[bool, Optional[BusinessRuleExperience], str]:
        exp = self.get_experience(experience_id)
        if not exp:
            return False, None, "not_found"

        if exp.status != ExperienceStatus.PUBLISHED.value:
            return False, exp, f"cannot_revoke_non_published_rule ({exp.status})"

        now = time.time()
        exp.status = ExperienceStatus.REVOKED.value
        exp.updated_at = now
        audit_event = AuditEvent(
            action="revoke",
            actor=actor,
            timestamp=now,
            note=reason or "管理员撤回已发布规则",
        )
        exp.audit_trail.append(audit_event)

        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    UPDATE experiences
                    SET status = ?, audit_trail_json = ?, updated_at = ?
                    WHERE experience_id = ?
                """, (
                    exp.status,
                    json.dumps([a.to_dict() for a in exp.audit_trail], ensure_ascii=False),
                    exp.updated_at,
                    exp.experience_id,
                ))
            self.record_audit("revoke_rule", actor, experience_id, {"reason": reason})
            return True, exp, "ok"
        finally:
            conn.close()

    def query_published_rules(
        self,
        customer: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> List[BusinessRuleExperience]:
        """Query active rules. Strictly enforces status == 'published'."""
        conn = self._get_conn()
        try:
            query = "SELECT * FROM experiences WHERE status = 'published'"
            params: List[Any] = []
            if customer:
                query += " AND (customer = ? OR customer IS NULL OR customer = '' OR customer = 'default_customer')"
                params.append(customer)
            if task_type:
                query += " AND (task_type = ? OR task_type IS NULL OR task_type = '' OR task_type = 'general')"
                params.append(task_type)
            query += " ORDER BY version DESC, updated_at DESC"

            cur = conn.execute(query, tuple(params))
            results = []
            for row in cur.fetchall():
                data = {
                    "experience_id": row["experience_id"],
                    "type": row["type"],
                    "title": row["title"],
                    "content": row["content"],
                    "contributor": row["contributor"],
                    "status": row["status"],
                    "version": row["version"],
                    "applies_to": json.loads(row["applies_to_json"] or "{}"),
                    "source": json.loads(row["source_json"] or "{}"),
                    "review": json.loads(row["review_json"]) if row["review_json"] else None,
                    "audit_trail": json.loads(row["audit_trail_json"] or "[]"),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                results.append(BusinessRuleExperience.from_dict(data))
            return results
        finally:
            conn.close()

    def list_audit_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ?", (limit,))
            return [
                {
                    "id": row["id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "experience_id": row["experience_id"],
                    "details": json.loads(row["details_json"] or "{}"),
                    "timestamp": row["timestamp"],
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()


class HubHTTPHandler(BaseHTTPRequestHandler):
    """HTTP Request handler for Experience Hub API."""

    storage: HubStorage

    def _send_json(self, status: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _parse_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _extract_token(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        custom = self.headers.get("X-HAFlow-Token", "")
        if custom:
            return custom.strip()
        # Query parameter fallback for development/testing
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "token" in q:
            return q["token"][0]
        return None

    def _authenticate(self) -> Optional[Dict[str, Any]]:
        token = self._extract_token()
        if not token:
            return None
        return self.storage.authenticate_token(token)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)

        # Health endpoint: public
        if path == "/api/v1/health":
            self._send_json(200, {"status": "ok", "service": "experience_hub", "version": "1.0"})
            return

        user = self._authenticate()
        if not user:
            self._send_json(401, {"error": "unauthorized", "message": "Valid Bearer Token is required"})
            return

        # 1. Query published rules for Agent / Task execution
        if path == "/api/v1/experiences/rules":
            req_cust = query.get("customer", [""])[0]
            req_type = query.get("task_type", [""])[0]

            # Cross-customer permission check
            allowed = user["allowed_customers"]
            if allowed != ["*"] and req_cust and req_cust not in allowed:
                self._send_json(403, {
                    "error": "forbidden",
                    "message": f"User {user['user_id']} is not authorized for customer '{req_cust}'",
                })
                return

            rules = self.storage.query_published_rules(customer=req_cust or None, task_type=req_type or None)
            # Further filter by user's allowed customers if user has restricted access and query was general
            if allowed != ["*"]:
                rules = [r for r in rules if not r.applies_to.customer or r.applies_to.customer in allowed or r.applies_to.customer == "default_customer"]

            self._send_json(200, {
                "ok": True,
                "count": len(rules),
                "rules": [r.to_dict() for r in rules],
            })
            return

        # 2. Query candidate list for review
        if path == "/api/v1/experiences/candidates":
            status_filter = query.get("status", [None])[0]
            cust_filter = query.get("customer", [None])[0]
            type_filter = query.get("task_type", [None])[0]

            # Regular contributors can only see candidates for their allowed customers
            allowed = user["allowed_customers"]
            if allowed != ["*"] and cust_filter and cust_filter not in allowed:
                self._send_json(403, {"error": "forbidden", "message": "Customer scope exceeded"})
                return

            candidates = self.storage.list_candidates(status=status_filter, customer=cust_filter, task_type=type_filter)
            if allowed != ["*"]:
                candidates = [c for c in candidates if not c.applies_to.customer or c.applies_to.customer in allowed]

            self._send_json(200, {
                "ok": True,
                "count": len(candidates),
                "candidates": [c.to_dict() for c in candidates],
            })
            return

        # 3. Audit logs
        if path == "/api/v1/experiences/audit":
            if user["role"] not in {"reviewer", "admin"}:
                self._send_json(403, {"error": "forbidden", "message": "Reviewer or Admin role required"})
                return
            logs = self.storage.list_audit_logs()
            self._send_json(200, {"ok": True, "audit_logs": logs})
            return

        # 4. Get single experience detail
        if path.startswith("/api/v1/experiences/"):
            exp_id = path.split("/api/v1/experiences/")[1]
            if "/" not in exp_id and exp_id:
                exp = self.storage.get_experience(exp_id)
                if not exp:
                    self._send_json(404, {"error": "not_found", "message": "Experience not found"})
                    return
                # Customer check
                allowed = user["allowed_customers"]
                if allowed != ["*"] and exp.applies_to.customer and exp.applies_to.customer not in allowed:
                    self._send_json(403, {"error": "forbidden", "message": "Customer scope exceeded"})
                    return
                self._send_json(200, {"ok": True, "experience": exp.to_dict()})
                return

        self._send_json(404, {"error": "not_found", "message": f"Endpoint {path} not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._parse_body()

        user = self._authenticate()
        if not user:
            self._send_json(401, {"error": "unauthorized", "message": "Valid Bearer Token is required"})
            return

        # 1. Submit candidate (contributor, reviewer, admin)
        if path == "/api/v1/experiences/candidates":
            try:
                exp = BusinessRuleExperience.from_dict(body)
            except Exception as e:
                self._send_json(400, {"error": "invalid_payload", "message": str(e)})
                return

            # Customer authorization check
            allowed = user["allowed_customers"]
            if allowed != ["*"] and exp.applies_to.customer and exp.applies_to.customer not in allowed:
                self._send_json(403, {"error": "forbidden", "message": "Cannot submit for unauthorized customer"})
                return

            ok, stored, reason = self.storage.save_candidate(exp, actor=user["user_id"])
            status_code = 201 if ok else 200
            self._send_json(status_code, {
                "ok": True,
                "experience_id": stored.experience_id,
                "status": stored.status,
                "reason": reason,
                "experience": stored.to_dict(),
            })
            return

        # 2. Review candidate (approve or reject) — requires reviewer or admin role
        if path.startswith("/api/v1/experiences/") and path.endswith("/review"):
            parts = path.split("/")
            exp_id = parts[4]

            if user["role"] not in {"reviewer", "admin"}:
                self._send_json(403, {"error": "forbidden", "message": "Reviewer or Admin role required for review"})
                return

            action = str(body.get("action") or "approve").lower()
            if action not in {"approve", "reject"}:
                self._send_json(400, {"error": "invalid_action", "message": "Action must be 'approve' or 'reject'"})
                return

            comment = str(body.get("comment") or "")
            ok, updated, reason = self.storage.review_candidate(
                exp_id, action=action, reviewer=user["user_id"], comment=comment
            )
            if not ok:
                self._send_json(400, {"error": "review_failed", "reason": reason})
                return

            self._send_json(200, {
                "ok": True,
                "experience_id": exp_id,
                "status": updated.status,
                "experience": updated.to_dict(),
            })
            return

        # 3. Revoke published rule — requires reviewer or admin role
        if path.startswith("/api/v1/experiences/") and path.endswith("/revoke"):
            parts = path.split("/")
            exp_id = parts[4]

            if user["role"] not in {"reviewer", "admin"}:
                self._send_json(403, {"error": "forbidden", "message": "Reviewer or Admin role required for revocation"})
                return

            reason = str(body.get("reason") or "")
            ok, updated, err = self.storage.revoke_rule(exp_id, actor=user["user_id"], reason=reason)
            if not ok:
                self._send_json(400, {"error": "revoke_failed", "reason": err})
                return

            self._send_json(200, {
                "ok": True,
                "experience_id": exp_id,
                "status": updated.status,
                "experience": updated.to_dict(),
            })
            return

        self._send_json(404, {"error": "not_found", "message": f"Endpoint {path} not found"})


class ExperienceHubServer:
    """Runnable Experience Hub Server instance."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8780,
        db_path: Optional[Path] = None,
    ):
        self.host = host
        self.port = port
        self.db_path = db_path or Path(os.environ.get("HERDR_HUB_DB_PATH") or DEFAULT_HUB_DB_PATH)
        self.storage = HubStorage(self.db_path)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, block: bool = False) -> None:
        handler_cls = type(
            "ConfiguredHubHandler",
            (HubHTTPHandler,),
            {"storage": self.storage},
        )
        self._server = ThreadingHTTPServer((self.host, self.port), handler_cls)
        # Handle port 0 dynamic assignment
        self.port = self._server.server_address[1]
        LOGGER.info("Experience Hub listening on http://%s:%d", self.host, self.port)

        if block:
            try:
                self._server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        else:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="hub-server")
            self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
