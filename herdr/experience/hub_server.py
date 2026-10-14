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

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import ssl
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
                token_hash TEXT PRIMARY KEY,
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
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp
            ON audit_logs (timestamp DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_logs_experience
            ON audit_logs (experience_id)
        """)

        # Migration from legacy auth_tokens schema (if table had 'token' instead of 'token_hash')
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(auth_tokens)").fetchall()}
        if "token" in cols and "token_hash" not in cols:
            rows = conn.execute("SELECT * FROM auth_tokens").fetchall()
            conn.execute("DROP TABLE auth_tokens")
            conn.execute("""
                CREATE TABLE auth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    allowed_customers_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            insecure_hash = hashlib.sha256("admin_secret_token_123".encode("utf-8")).hexdigest()
            for r in rows:
                raw_val = r["token"]
                if raw_val == "admin_secret_token_123":
                    continue
                # Legacy column always stored the RAW token (PR #209 compared it
                # verbatim), so it must always be hashed here. A 64-hex raw
                # token (e.g. `openssl rand -hex 32`) must NOT be mistaken for
                # an already-hashed value, or the admin is locked out.
                thash = hashlib.sha256(raw_val.encode("utf-8")).hexdigest()
                if thash == insecure_hash:
                    continue
                conn.execute("""
                    INSERT OR REPLACE INTO auth_tokens (token_hash, user_id, role, allowed_customers_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (thash, r["user_id"], r["role"], r["allowed_customers_json"], r["created_at"]))
        else:
            # Purge insecure default token if present
            insecure_hash = hashlib.sha256("admin_secret_token_123".encode("utf-8")).hexdigest()
            conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (insecure_hash,))
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
    ) -> str:
        raw = (token or "").strip()
        if not raw:
            raise ValueError("Token string cannot be empty")
        if raw == "admin_secret_token_123":
            raise ValueError("Insecure default token 'admin_secret_token_123' is prohibited")

        if allowed_customers is None:
            allowed_customers = ["*"]
        if not allowed_customers:
            raise ValueError("allowed_customers must not be empty; use ['*'] for wildcard scope")

        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("""
                    INSERT INTO auth_tokens (token_hash, user_id, role, allowed_customers_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(token_hash) DO UPDATE SET
                        user_id = excluded.user_id,
                        role = excluded.role,
                        allowed_customers_json = excluded.allowed_customers_json
                """, (token_hash, user_id, role, json.dumps(allowed_customers), time.time()))
            return token_hash
        finally:
            conn.close()

    def authenticate_token(self, token: str) -> Optional[Dict[str, Any]]:
        raw = (token or "").strip()
        if not raw:
            return None
        token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM auth_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if not row:
                return None
            if not hmac.compare_digest(row["token_hash"], token_hash):
                return None
            return {
                "token_hash": row["token_hash"],
                "user_id": row["user_id"],
                "role": row["role"],
                "allowed_customers": json.loads(row["allowed_customers_json"] or "[]"),
            }
        finally:
            conn.close()

    def has_admin(self) -> bool:
        """Check if any admin token is registered in the database."""
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT count(*) as c FROM auth_tokens WHERE role = 'admin'").fetchone()
            return bool(row and row["c"] > 0)
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
        """Save a new candidate experience.

        P0-02:
        - Server strictly forces status = pending_review.
        - Server forces contributor = authenticated actor.
        - Server clears review, version = 1, sets authoritative timestamps.
        - Idempotent on experience_id: returns already_exists if content matches, or conflict if content differs.
        """
        conn = self._get_conn()
        try:
            existing = self.get_experience(exp.experience_id)
            if existing:
                if existing.content_hash() == exp.content_hash():
                    return False, existing, "already_exists"
                else:
                    return False, existing, "conflict"

            now = time.time()
            exp.status = ExperienceStatus.PENDING_REVIEW.value
            exp.contributor = actor
            exp.review = None
            exp.version = 1
            exp.created_at = now
            exp.updated_at = now
            exp.audit_trail = [
                AuditEvent(
                    action="submit_candidate",
                    actor=actor,
                    timestamp=now,
                    note=f"提交候选业务规则: {exp.title}",
                )
            ]

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
                    None,
                    json.dumps([a.to_dict() for a in exp.audit_trail], ensure_ascii=False),
                    exp.created_at,
                    exp.updated_at,
                ))
            self.record_audit("submit_candidate", actor, exp.experience_id, {"title": exp.title, "customer": exp.applies_to.customer})
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
        contributor: Optional[str] = None,
    ) -> List[BusinessRuleExperience]:
        conn = self._get_conn()
        try:
            query = "SELECT * FROM experiences WHERE 1=1"
            params: List[Any] = []
            if status:
                query += " AND status = ?"
                params.append(status)
            if customer:
                query += " AND (customer = ? OR customer = '*' OR customer = 'unknown' OR customer IS NULL)"
                params.append(customer)
            if task_type:
                query += " AND (task_type = ? OR task_type IS NULL)"
                params.append(task_type)
            if contributor:
                query += " AND contributor = ?"
                params.append(contributor)
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

        # Publishing requires explicit customer scope; unknown/empty must be fixed first.
        if action == "approve":
            cust = (exp.applies_to.customer or "").strip()
            if cust in {"", "unknown", "default_customer"}:
                return False, exp, "customer_unknown_requires_explicit_scope"

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
            self.record_audit(f"review_{action}", reviewer, experience_id, {"comment": comment, "customer": exp.applies_to.customer})
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
            self.record_audit("revoke_rule", actor, experience_id, {"reason": reason, "customer": exp.applies_to.customer})
            return True, exp, "ok"
        finally:
            conn.close()

    def query_published_rules(
        self,
        customer: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> List[BusinessRuleExperience]:
        """Query active rules. Strictly enforces status == 'published' and customer boundary."""
        conn = self._get_conn()
        try:
            query = "SELECT * FROM experiences WHERE status = 'published'"
            params: List[Any] = []
            cust_str = str(customer).strip() if customer else ""
            if cust_str and cust_str not in {"unknown", "default_customer"}:
                query += " AND (customer = ? OR customer = '*')"
                params.append(cust_str)
            else:
                # P1-06: No customer context provided -> only explicit global rules ('*')
                query += " AND customer = '*'"

            type_str = str(task_type).strip() if task_type else ""
            if type_str and type_str not in {"general", "unknown"}:
                query += " AND (task_type = ? OR task_type = '*' OR task_type = 'general' OR task_type IS NULL)"
                params.append(type_str)
            elif type_str in {"general", "unknown"}:
                # Generic task context must not absorb business-specific rules.
                query += " AND (task_type = 'general' OR task_type = '*' OR task_type IS NULL)"
            # Empty task_type (review/listing without type filter) keeps all types.
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

    def list_audit_logs(
        self,
        limit: int = 100,
        allowed_customers: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            # Empty scope is deny-all (never wildcard); also avoids `IN ()`.
            if allowed_customers is not None and len(allowed_customers) == 0:
                return []
            # SQL prefilter on the authoritative experience customer before the
            # Python refinement, so unrelated customers never reach the scan.
            # Orphan log rows (no linked experience) are kept for the details
            # check below.
            if allowed_customers and allowed_customers != ["*"]:
                placeholders = ", ".join(["?"] * len(allowed_customers))
                cur = conn.execute(f"""
                    SELECT a.*, e.customer AS exp_customer
                    FROM audit_logs a LEFT JOIN experiences e
                      ON a.experience_id = e.experience_id
                    WHERE e.customer IN ({placeholders}) OR e.customer IS NULL
                    ORDER BY a.timestamp DESC
                """, tuple(allowed_customers))
            else:
                cur = conn.execute("""
                    SELECT a.*, e.customer AS exp_customer
                    FROM audit_logs a LEFT JOIN experiences e
                      ON a.experience_id = e.experience_id
                    ORDER BY a.timestamp DESC
                """)
            logs = []
            for row in cur:
                details = json.loads(row["details_json"] or "{}")
                if allowed_customers is not None and allowed_customers != ["*"]:
                    cust = details.get("customer") or row["exp_customer"]
                    if cust not in allowed_customers:
                        continue
                logs.append({
                    "id": row["id"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "experience_id": row["experience_id"],
                    "details": details,
                    "timestamp": row["timestamp"],
                })
                if len(logs) >= limit:
                    break
            return logs
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
        # P1-09: Prohibit passing token via URL query parameter
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
            if allowed != ["*"] and req_cust and req_cust != "*" and req_cust not in allowed:
                self._send_json(403, {
                    "error": "forbidden",
                    "message": f"User {user['user_id']} is not authorized for customer '{req_cust}'",
                })
                return

            rules = self.storage.query_published_rules(customer=req_cust or None, task_type=req_type or None)
            if allowed != ["*"]:
                rules = [r for r in rules if r.applies_to.customer == "*" or r.applies_to.customer in allowed]

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

            allowed = user["allowed_customers"]
            if allowed != ["*"] and cust_filter and cust_filter != "*" and cust_filter not in allowed:
                self._send_json(403, {"error": "forbidden", "message": "Customer scope exceeded"})
                return

            # P1-04: Contributors see only their own candidates within their
            # customer scope; same user with two tokens must not cross-read.
            if user["role"] == "contributor":
                candidates = self.storage.list_candidates(
                    status=status_filter,
                    customer=cust_filter,
                    task_type=type_filter,
                    contributor=user["user_id"],
                )
                if allowed != ["*"]:
                    candidates = [c for c in candidates if c.applies_to.customer in allowed]
            else:
                candidates = self.storage.list_candidates(
                    status=status_filter,
                    customer=cust_filter,
                    task_type=type_filter,
                )
                if allowed != ["*"]:
                    # Pending review scope is strictly limited to allowed customers.
                    # Published global rules remain readable via /rules; they are
                    # not exposed here as reviewable candidates.
                    candidates = [c for c in candidates if c.applies_to.customer in allowed]

            self._send_json(200, {
                "ok": True,
                "count": len(candidates),
                "candidates": [c.to_dict() for c in candidates],
            })
            return

        # 3. Audit logs (reviewer and admin only)
        if path == "/api/v1/experiences/audit":
            if user["role"] not in {"reviewer", "admin"}:
                self._send_json(403, {"error": "forbidden", "message": "Reviewer or Admin role required"})
                return
            logs = self.storage.list_audit_logs(allowed_customers=user["allowed_customers"])
            self._send_json(200, {"ok": True, "audit_logs": logs})
            return

        # 4. Get single experience detail
        target_exp_id = None
        if path.startswith("/api/v1/experiences/") and path not in {
            "/api/v1/experiences/rules",
            "/api/v1/experiences/candidates",
            "/api/v1/experiences/audit",
            "/api/v1/experiences/candidate",
        }:
            target_exp_id = path.split("/api/v1/experiences/")[1]
        elif path in {"/api/v1/experiences/candidate", "/api/v1/experience"}:
            target_exp_id = query.get("id", [""])[0] or query.get("experience_id", [""])[0]

        if target_exp_id and "/" not in target_exp_id:
            exp = self.storage.get_experience(target_exp_id)
            if not exp:
                self._send_json(404, {"error": "not_found", "message": "Experience not found"})
                return
            # Customer check: restricted users see only their own scope.
            # Published global rules ('*') stay readable; pending globals do not.
            allowed = user["allowed_customers"]
            if allowed != ["*"] and exp.applies_to.customer and exp.applies_to.customer not in allowed:
                if not (exp.applies_to.customer == "*" and exp.status == ExperienceStatus.PUBLISHED.value):
                    self._send_json(403, {"error": "forbidden", "message": "Customer scope exceeded"})
                    return
            # P1-04: Contributor can only view their own unapproved candidate
            if user["role"] == "contributor":
                if exp.status != ExperienceStatus.PUBLISHED.value and exp.contributor != user["user_id"]:
                    self._send_json(403, {"error": "forbidden", "message": "Contributors cannot access other contributors' unapproved candidates"})
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

            # Customer authorization check: only wildcard scope may write global rules.
            allowed = user["allowed_customers"]
            if allowed != ["*"] and exp.applies_to.customer and exp.applies_to.customer not in allowed:
                self._send_json(403, {"error": "forbidden", "message": "Cannot submit for unauthorized customer"})
                return

            ok, stored, reason = self.storage.save_candidate(exp, actor=user["user_id"])
            if reason == "conflict":
                self._send_json(409, {
                    "ok": False,
                    "error": "conflict",
                    "message": "Candidate with this ID already exists with different content",
                    "experience_id": exp.experience_id,
                })
                return

            status_code = 201 if ok else 200
            resp = {
                "ok": True,
                "experience_id": stored.experience_id,
                "status": stored.status,
                "reason": reason,
            }
            # Idempotent resubmits must not leak another employee's pending
            # detail: only the original contributor sees the full object.
            if stored.contributor == user["user_id"]:
                resp["experience"] = stored.to_dict()
            self._send_json(status_code, resp)
            return

        # 2. Review candidate (approve or reject) — requires reviewer or admin role
        if path.startswith("/api/v1/experiences/") and path.endswith("/review"):
            parts = path.split("/")
            exp_id = parts[4]

            if user["role"] not in {"reviewer", "admin"}:
                self._send_json(403, {"error": "forbidden", "message": "Reviewer or Admin role required for review"})
                return

            exp = self.storage.get_experience(exp_id)
            if not exp:
                self._send_json(404, {"error": "not_found", "message": "Experience not found"})
                return

            # P1-05: Enforce customer boundary on review; global rules need wildcard scope.
            allowed = user["allowed_customers"]
            if allowed != ["*"] and exp.applies_to.customer and exp.applies_to.customer not in allowed:
                self._send_json(403, {"error": "forbidden", "message": f"Reviewer not authorized for customer '{exp.applies_to.customer}'"})
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

            exp = self.storage.get_experience(exp_id)
            if not exp:
                self._send_json(404, {"error": "not_found", "message": "Experience not found"})
                return

            # P1-05: Enforce customer boundary on revoke; global rules need wildcard scope.
            allowed = user["allowed_customers"]
            if allowed != ["*"] and exp.applies_to.customer and exp.applies_to.customer not in allowed:
                self._send_json(403, {"error": "forbidden", "message": f"Reviewer not authorized for customer '{exp.applies_to.customer}'"})
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
        cert_file: Optional[Path] = None,
        key_file: Optional[Path] = None,
        allow_insecure_http: bool = False,
    ):
        self.host = host
        self.port = port
        self.db_path = db_path or Path(os.environ.get("HERDR_HUB_DB_PATH") or DEFAULT_HUB_DB_PATH)
        self.cert_file = Path(cert_file) if cert_file else None
        self.key_file = Path(key_file) if key_file else None
        self.allow_insecure_http = allow_insecure_http or (os.environ.get("HERDR_HUB_ALLOW_INSECURE_HTTP") == "1")
        self.storage = HubStorage(self.db_path)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, block: bool = False) -> None:
        is_loopback = self.host in ("127.0.0.1", "localhost", "::1")
        if not is_loopback and not (self.cert_file and self.key_file) and not self.allow_insecure_http:
            raise RuntimeError(
                f"Binding Experience Hub to non-loopback host '{self.host}' over plain HTTP is prohibited. "
                "Provide TLS certs (--cert-file and --key-file) or set --allow-insecure-http if behind a trusted TLS reverse proxy."
            )

        handler_cls = type(
            "ConfiguredHubHandler",
            (HubHTTPHandler,),
            {"storage": self.storage},
        )
        self._server = ThreadingHTTPServer((self.host, self.port), handler_cls)
        self.port = self._server.server_address[1]

        if self.cert_file and self.key_file:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(certfile=str(self.cert_file), keyfile=str(self.key_file))
            self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
            LOGGER.info("Experience Hub listening on https://%s:%d (TLS enabled)", self.host, self.port)
        else:
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
