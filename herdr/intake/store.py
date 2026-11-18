"""Tenant/connection-scoped Intake persistence and fenced event processing."""

import json
import logging
import random
import secrets
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from herdr.state.events import record_event

from .protocol import (MAX_BODY_BYTES, Delivery, Fact, Scope, digest, error_code, integer,
                       prepare_fact, text, token)
from .schema import open_db

LOG = logging.getLogger(__name__)
AVAILABLE = ("ACTIVE", "DEGRADED")
VERIFICATION = ("SIGNATURE", "AUTHENTICATED_READ", "AUTHENTICATED_STREAM", "LOCAL_JOB")
CONNECTION_STATES = (*AVAILABLE, "DISABLED", "AUTH_REQUIRED")
RAW_FACT = "com.haflow.intake.raw.accepted.v1"
SCOPE_SQL = "tenant_id = ? AND connection_id = ?"
DAY_MS = 86_400_000


class ScopeDenied(PermissionError):
    pass


class LeaseLost(RuntimeError):
    pass


class StateConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    revision: ClassVar[int] = 1
    max_attempts: int = 6
    base_delay_ms: int = 5_000
    max_delay_ms: int = 900_000
    lease_ms: int = 30_000
    retention_ms: int = 90 * DAY_MS

    def __post_init__(self):
        integer(self.max_attempts, 1, 30)
        integer(self.base_delay_ms, 1, DAY_MS)
        integer(self.max_delay_ms, self.base_delay_ms, DAY_MS)
        integer(self.lease_ms, 1, DAY_MS)
        integer(self.retention_ms, 1, 3650 * DAY_MS)

    def retry_at(self, now, used, retry_after_ms):
        ceiling = min(self.max_delay_ms, self.base_delay_ms * 2 ** (used - 1))
        delay = max(random.randint((ceiling + 1) // 2, ceiling), retry_after_ms)
        return integer(now + delay)


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    outcome: str
    event_id: str | None
    reason: str | None


@dataclass(frozen=True)
class Prepared:
    delivery: Delivery
    fact: Fact | None
    raw_digest: str | None
    transport_digest: str | None
    reason: str | None


@dataclass(frozen=True)
class Claim:
    scope: Scope
    event_id: str
    epoch: int
    revision: int
    lease_until: int
    type: str
    owner_token: str = field(repr=False)


def prepare(delivery: Delivery) -> Prepared:
    raw_hash = transport_hash = None
    try:
        if not isinstance(delivery.raw, bytes) or len(delivery.raw) > MAX_BODY_BYTES:
            raise ValueError("invalid raw body size")
        raw_hash = digest(delivery.raw)
        if delivery.transport_id is not None:
            transport_hash = digest(text(delivery.transport_id).encode())
        if delivery.batch_id is not None:
            token(delivery.batch_id)
        if delivery.provider_event_id is not None:
            text(delivery.provider_event_id)
        if delivery.verified_via not in VERIFICATION or type(delivery.duplicate_only) is not bool:
            raise ValueError("missing verification evidence")
        token(delivery.capability)
        fact = prepare_fact(delivery)
        # This phase has no enrolled encrypted raw retention backend.
        if fact.type == RAW_FACT:
            return Prepared(delivery, None, raw_hash, transport_hash, "RAW_RETENTION_REQUIRED")
        return Prepared(delivery, fact, raw_hash, transport_hash, None)
    except (ValueError, OverflowError):
        return Prepared(delivery, None, raw_hash, transport_hash, "INVALID_DELIVERY")


class EventStore:
    def __init__(self, path: Path, scope: Scope, *, policy: Policy = Policy(),
                 authorizer=None, verifier: Callable[[Delivery], bool] | None = None, clock=None):
        self.path = Path(path)
        self.scope = scope
        self.policy = policy
        self.authorizer = authorizer
        self._verifier = verifier
        self.now = clock if clock is not None else lambda: time.time_ns() // 1_000_000

    @contextmanager
    def _write(self):
        with closing(open_db(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db

    def _actor(self, action):
        actor = self.authorizer(action, self.scope) if self.authorizer else None
        if not actor:
            raise ScopeDenied("intake authorization required")
        return token(actor)

    def _audit(self, db, kind, now, **fields):
        record_event({"source": "active_intake", "event_type": kind,
                      "timestamp": now / 1000,
                      "payload": {"tenant_id": self.scope.tenant_id,
                                  "connection_id": self.scope.connection_id,
                                  "occurred_at_ms": now, **fields}},
                     conn=db)

    def list_admin_audit(self, *, after=0, limit=100):
        self._actor("view_audit")
        with closing(open_db(self.path)) as db:
            return [
                {"id": row["id"], "event_type": row["event_type"], **json.loads(row["payload_json"])}
                for row in db.execute("""SELECT id, event_type, payload_json FROM events
                    WHERE source = 'active_intake' AND id > ?
                    AND CASE WHEN json_valid(payload_json)
                        THEN json_extract(payload_json, '$.tenant_id') END = ?
                    AND CASE WHEN json_valid(payload_json)
                        THEN json_extract(payload_json, '$.connection_id') END = ?
                    ORDER BY id LIMIT ?""", (integer(after), *self.scope.keys, integer(limit, 1, 500)))
            ]

    def _connection(self, db):
        row = db.execute(f"SELECT * FROM intake_connections WHERE {SCOPE_SQL}",
                         self.scope.keys).fetchone()
        if not row:
            raise ScopeDenied("unknown intake scope")
        return row

    def register_connection(self, *, provider, instance, display_name, connector_version,
                            auth_ref="none"):
        actor = self._actor("manage_connection")
        provider, instance, auth_ref = token(provider), token(instance), token(auth_ref)
        display_name, connector_version = text(display_name), text(connector_version, 64)
        with self._write() as db:
            now = self.now()
            db.execute("""INSERT INTO intake_connections
                (tenant_id, connection_id, provider, provider_instance_key, display_name,
                 auth_ref, connector_version, config_revision, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'ACTIVE', ?, ?)""",
                       (*self.scope.keys, provider, instance, display_name, auth_ref,
                        connector_version, now, now))
            self._audit(db, "intake_connection_registered", now, actor_ref=actor, revision=1)

    def update_connection_authorization(self, revision, *, status, reason, auth_ref=None):
        actor = self._actor("manage_connection")
        integer(revision, 1)
        reason = text(reason)
        if status not in CONNECTION_STATES:
            raise ValueError("invalid connection state")
        if auth_ref is not None:
            token(auth_ref)
        with self._write() as db:
            now = self.now()
            connection = self._connection(db)
            if connection["config_revision"] != revision:
                raise StateConflict("connection revision changed")
            db.execute(f"""UPDATE intake_connections SET status = ?, updated_at = ?,
                config_revision = config_revision + 1, auth_ref = COALESCE(?, auth_ref)
                WHERE {SCOPE_SQL} AND config_revision = ?""",
                       (status, now, auth_ref, *self.scope.keys, revision))
            self._audit(db, "intake_connection_authorization", now, actor_ref=actor,
                        reason=reason, revision=revision + 1, previous_status=connection["status"],
                        status=status, auth_ref_changed=auth_ref not in (None, connection["auth_ref"]))
        return revision + 1

    def _receipt(self, db, now, outcome, *, event_id=None, reason=None, raw_hash=None,
                 transport_hash=None, batch_id=None):
        receipt = Receipt(str(uuid4()), outcome, event_id, reason)
        db.execute("""INSERT INTO intake_delivery_receipts
            (receipt_id, tenant_id, connection_id, received_at, outcome, canonical_event_id,
             reason_code, raw_digest, transport_id_digest, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (receipt.receipt_id, *self.scope.keys, now, outcome, event_id,
                    reason, raw_hash, transport_hash, batch_id))
        return receipt

    def _accept(self, db, item, connection, now):
        delivery, fact = item.delivery, item.fact
        reason = item.reason
        if fact:
            expected = (f"urn:haflow:intake:{connection['provider']}:"
                        f"{connection['provider_instance_key']}:{delivery.capability}")
            if fact.source != expected:
                reason = "SOURCE_BINDING_MISMATCH"
        event_id = None
        outcome = "REJECTED"
        if not reason:
            # Tenant-wide deduplication by design. The trusted source binding
            # above permits sibling connections to observe this same fact.
            existing = db.execute("""SELECT event_id, fact_digest FROM intake_events
                WHERE tenant_id = ? AND source = ? AND id = ?""",
                                  (self.scope.tenant_id, fact.source, fact.event_id)).fetchone()
            if existing and existing["fact_digest"] != fact.digest:
                reason = "FACT_IDENTITY_COLLISION"
            elif existing:
                outcome, event_id = "DUPLICATE", existing["event_id"]
            elif delivery.duplicate_only:
                reason = "STALE_UNKNOWN_FACT"
            else:
                outcome, event_id = "ACCEPTED", str(uuid4())
                db.execute("""INSERT INTO intake_events
                    (event_id, tenant_id, connection_id, provider_event_id, source, id, type,
                     data_schema_uri, data_json, subject, occurred_at, occurred_time, received_at, raw_digest,
                     fact_digest, signature_verified, verified_via, status, correlation_id,
                     connector_version, config_revision, policy_revision)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'VERIFIED', ?, ?, ?, ?)""",
                           (event_id, *self.scope.keys, delivery.provider_event_id, fact.source,
                            fact.event_id, fact.type, fact.schema, fact.data, fact.subject,
                            fact.occurred_at, fact.occurred_time, now, item.raw_digest, fact.digest,
                            1 if delivery.verified_via == "SIGNATURE" else None,
                            delivery.verified_via, str(uuid4()), connection["connector_version"],
                            connection["config_revision"], self.policy.revision))
        return self._receipt(db, now, outcome, event_id=event_id, reason=reason,
                             raw_hash=item.raw_digest, transport_hash=item.transport_digest,
                             batch_id=delivery.batch_id if not item.reason else None)

    def _available(self, connection, revision):
        return connection["status"] in AVAILABLE and connection["config_revision"] == revision

    def _verify_delivery(self, delivery: Delivery) -> None:
        if delivery.scope != self.scope:
            raise ScopeDenied("delivery scope mismatch")
        if self._verifier is None or self._verifier(delivery) is not True:
            raise ScopeDenied("trusted ingress verification required")

    def accept_event(self, delivery: Delivery) -> Receipt:
        """Accept through the configured verifier. Return only after commit."""
        self._verify_delivery(delivery)
        revision = integer(delivery.config_revision, 1)
        item = prepare(delivery)
        with self._write() as db:
            now = self.now()
            connection = self._connection(db)
            if self._available(connection, revision):
                receipt = self._accept(db, item, connection, now)
            else:
                receipt = self._receipt(db, now, "REJECTED", reason="CONNECTION_UNAVAILABLE",
                                        raw_hash=item.raw_digest, transport_hash=item.transport_digest)
            self._audit_collision(db, receipt, now)
        if receipt.reason == "FACT_IDENTITY_COLLISION":
            LOG.warning("Intake fact identity collision, receipt %s", receipt.receipt_id)
        return receipt

    def _audit_collision(self, db, receipt, now):
        if receipt.reason == "FACT_IDENTITY_COLLISION":
            self._audit(db, "intake_identity_collision", now, receipt_id=receipt.receipt_id)

    def record_rejection(self, reason):
        """Record a rejection only when trusted ingress can establish scope."""
        self._actor("record_rejection")
        error_code(reason)
        with self._write() as db:
            self._connection(db)
            receipt = self._receipt(db, self.now(), "REJECTED", reason=reason)
        return receipt

    def _read_event(self, where, parameters):
        with closing(open_db(self.path)) as db:
            row = db.execute(f"""SELECT e.*, c.status AS connection_status FROM intake_events e
                JOIN intake_connections c ON (c.tenant_id, c.connection_id) = (e.tenant_id, e.connection_id)
                WHERE e.tenant_id = ? AND e.connection_id = ? AND {where}""",
                             (*self.scope.keys, *parameters)).fetchone()
            if not row:
                return None
            result = dict(row)
            for key in ("payload_json", "replay_actor", "replay_reason"):
                result.pop(key)
            if (result.pop("connection_status") not in AVAILABLE
                    or result["received_at"] <= self.now() - self.policy.retention_ms):
                result["data_json"] = None
            return result

    def get_event(self, event_id):
        return self._read_event("event_id = ?", (text(event_id),))

    def get_event_by_identity(self, source, event_id):
        return self._read_event("source = ? AND id = ?", (text(source), text(event_id)))

    def list_events(self, *, limit=100, after=None):
        integer(limit, 1, 500)
        since, last_id = (-1, "") if after is None else (integer(after[0]), text(after[1]))
        with closing(open_db(self.path)) as db:
            return [dict(row) for row in db.execute(f"""SELECT event_id, source, id, type,
                status, received_at, attempt_count FROM intake_events WHERE {SCOPE_SQL}
                AND (received_at, event_id) > (?, ?) ORDER BY received_at, event_id LIMIT ?""",
                                                   (*self.scope.keys, since, last_id, limit))]

    def list_receipts(self, *, limit=100, after=None):
        integer(limit, 1, 500)
        since, last_id = (-1, "") if after is None else (integer(after[0]), text(after[1]))
        with closing(open_db(self.path)) as db:
            return [dict(row) for row in db.execute(f"""SELECT * FROM intake_delivery_receipts
                WHERE {SCOPE_SQL} AND (received_at, receipt_id) > (?, ?)
                ORDER BY received_at, receipt_id LIMIT ?""", (*self.scope.keys, since, last_id, limit))]

    def list_attempts(self, event_id, *, limit=100, after=0):
        with closing(open_db(self.path)) as db:
            return [dict(row) for row in db.execute(f"""SELECT attempt_id, tenant_id, connection_id,
                event_id, attempt_number, started_at, finished_at, outcome, error, next_retry_at,
                claim_epoch, config_revision, trigger, actor_ref, replay_reason FROM intake_attempts
                WHERE {SCOPE_SQL} AND event_id = ? AND attempt_number > ?
                ORDER BY attempt_number LIMIT ?""",
                (*self.scope.keys, text(event_id), integer(after), integer(limit, 1, 500)))]

    def claim_next_event(self):
        with self._write() as db:
            now = self.now()
            connection = self._connection(db)
            if connection["status"] not in AVAILABLE:
                return None
            query = f"""SELECT * FROM intake_events WHERE {SCOPE_SQL}
                AND connector_version = ? AND policy_revision = ? AND status = ?"""
            keys = (*self.scope.keys, connection["connector_version"], self.policy.revision)
            verified = db.execute(query + """ AND next_retry_at IS NULL
                ORDER BY received_at, event_id LIMIT 1""", (*keys, "VERIFIED")).fetchone()
            retry = db.execute(query + """ AND next_retry_at <= ?
                ORDER BY next_retry_at, received_at, event_id LIMIT 1""",
                               (*keys, "RETRY_WAIT", now)).fetchone()
            if verified is None and retry is None:
                return None
            retry_first = retry is not None and (
                verified is None or retry["next_retry_at"] <= verified["received_at"])
            event = retry if retry_first else verified
            epoch = event["claim_epoch"] + 1
            number = event["attempt_count"] + 1
            expires = integer(now + self.policy.lease_ms)
            trigger = "INITIAL" if number == 1 else "RETRY"
            if event["replay_actor"]:
                trigger = "REPLAY"
            owner_token = secrets.token_hex(32)
            db.execute("""INSERT INTO intake_attempts
                (attempt_id, tenant_id, connection_id, event_id, attempt_number, started_at,
                 claim_epoch, ownership_digest, config_revision, trigger, actor_ref, replay_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (str(uuid4()), *self.scope.keys, event["event_id"], number, now,
                        epoch, digest(owner_token.encode("ascii")), connection["config_revision"],
                        trigger, event["replay_actor"], event["replay_reason"]))
            db.execute(f"""UPDATE intake_events SET status = 'PROCESSING', claim_epoch = ?,
                lease_until = ?, next_retry_at = NULL, attempt_count = ?,
                retry_budget_used = retry_budget_used + 1, replay_actor = NULL, replay_reason = NULL
                WHERE {SCOPE_SQL} AND event_id = ?""",
                       (epoch, expires, number, *self.scope.keys, event["event_id"]))
            claim = Claim(self.scope, event["event_id"], epoch, connection["config_revision"],
                          expires, event["type"], owner_token)
        return claim

    def _processing(self, db, claim, now):
        if claim.scope != self.scope or not self._available(self._connection(db), claim.revision):
            raise LeaseLost("intake scope or authorization changed")
        row = db.execute(f"""SELECT * FROM intake_events WHERE {SCOPE_SQL}
            AND event_id = ? AND status = 'PROCESSING' AND claim_epoch = ?
            AND lease_until = ? AND lease_until > ?
            AND EXISTS (SELECT 1 FROM intake_attempts a
                WHERE a.tenant_id = intake_events.tenant_id
                AND a.connection_id = intake_events.connection_id AND a.event_id = intake_events.event_id
                AND a.attempt_number = intake_events.attempt_count AND a.claim_epoch = intake_events.claim_epoch
                AND a.config_revision = ? AND a.ownership_digest = ? AND a.finished_at IS NULL)""",
                         (*self.scope.keys, claim.event_id, claim.epoch, claim.lease_until, now,
                          claim.revision, digest(text(claim.owner_token, 64).encode("utf-8")))).fetchone()
        if row is None:
            raise LeaseLost("intake claim expired or superseded")
        return row

    def _finish(self, db, row, now, status, reason=None, retry_at=None):
        event_id = row["event_id"]
        updated = db.execute(f"""UPDATE intake_attempts SET finished_at = ?, outcome = ?,
            error = ?, next_retry_at = ? WHERE {SCOPE_SQL} AND event_id = ?
            AND attempt_number = ? AND claim_epoch = ? AND finished_at IS NULL""",
                             (now, status, reason, retry_at, *self.scope.keys, event_id,
                              row["attempt_count"], row["claim_epoch"]))
        if updated.rowcount != 1:
            raise StateConflict("open intake attempt missing")
        db.execute(f"""UPDATE intake_events SET status = ?, lease_until = NULL,
            next_retry_at = ?, last_error = ? WHERE {SCOPE_SQL} AND event_id = ?""",
                   (status, retry_at, reason, *self.scope.keys, event_id))
        return status

    def complete_event(self, claim, *, outcome="PROCESSED", reason=None):
        if outcome not in ("PROCESSED", "IGNORED", "REJECTED"):
            raise ValueError("invalid intake completion")
        if outcome != "PROCESSED" or reason is not None:
            error_code(reason)
        with self._write() as db:
            now = self.now()
            self._finish(db, self._processing(db, claim, now), now, outcome, reason)

    def _fail(self, db, row, now, reason, permanent=False, retry_after_ms=0):
        used = row["retry_budget_used"]
        if permanent or used >= self.policy.max_attempts:
            return self._finish(db, row, now, "DEAD_LETTER", reason)
        retry_at = self.policy.retry_at(now, used, retry_after_ms)
        return self._finish(db, row, now, "RETRY_WAIT", reason, retry_at)

    def fail_event(self, claim, reason, *, permanent=False, retry_after_ms=0):
        error_code(reason)
        integer(retry_after_ms)
        if retry_after_ms > self.policy.max_delay_ms:
            LOG.warning("Intake Retry-After exceeds backoff limit, event %s", claim.event_id)
        with self._write() as db:
            now = self.now()
            return self._fail(db, self._processing(db, claim, now), now, reason,
                              permanent, retry_after_ms)

    def recover_expired(self, *, limit=50):
        integer(limit, 1, 100)
        with self._write() as db:
            now = self.now()
            rows = db.execute(f"""SELECT * FROM intake_events WHERE {SCOPE_SQL}
                AND status = 'PROCESSING' AND lease_until <= ?
                ORDER BY lease_until, event_id LIMIT ?""", (*self.scope.keys, now, limit)).fetchall()
            for row in rows:
                self._fail(db, row, now, "LEASE_EXPIRED")
            return len(rows)

    def replay_event(self, event_id, expected_epoch, *, reason):
        actor = self._actor("replay")
        reason = text(reason)
        text(event_id)
        integer(expected_epoch, 1)
        with self._write() as db:
            now = self.now()
            connection = self._connection(db)
            if connection["status"] not in AVAILABLE:
                raise ScopeDenied("inactive intake scope")
            change = db.execute(f"""UPDATE intake_events SET status = 'VERIFIED',
                retry_budget_used = 0, last_error = NULL, replay_actor = ?, replay_reason = ?
                WHERE {SCOPE_SQL} AND event_id = ? AND status = 'DEAD_LETTER'
                AND claim_epoch = ? AND connector_version = ? AND policy_revision = ?
                AND data_json IS NOT NULL AND received_at > ?""",
                                (actor, reason, *self.scope.keys, event_id, expected_epoch,
                                 connection["connector_version"], self.policy.revision,
                                 now - self.policy.retention_ms))
            if change.rowcount != 1:
                raise StateConflict("replay requires matching dead letter and retained compatible data")
            self._audit(db, "intake_replay_requested", now, event_id=event_id,
                        actor_ref=actor, reason=reason, previous_epoch=expected_epoch)

    def prune(self, *, limit=50):
        """Purge expired canonical bodies and receipts in bounded batches."""
        integer(limit, 1, 100)
        with self._write() as db:
            now = self.now()
            keys = self.scope.keys
            cutoff = now - self.policy.retention_ms
            purged = db.execute(f"""UPDATE intake_events SET data_json = NULL
                WHERE {SCOPE_SQL} AND event_id IN (
                    SELECT event_id FROM intake_events WHERE {SCOPE_SQL} AND received_at <= ?
                    AND data_json IS NOT NULL
                    ORDER BY received_at, event_id LIMIT ?)""", (*keys, *keys, cutoff, limit))
            receipts = db.execute(f"""DELETE FROM intake_delivery_receipts
                WHERE {SCOPE_SQL} AND receipt_id IN (
                    SELECT receipt_id FROM intake_delivery_receipts WHERE {SCOPE_SQL}
                    AND received_at <= ? ORDER BY received_at, receipt_id LIMIT ?)""",
                                  (*keys, *keys, cutoff, limit))
            return purged.rowcount, receipts.rowcount
