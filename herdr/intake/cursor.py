"""Fenced, fixed-window keyset polling over the Intake ledger."""

import json
import logging
import secrets
from contextlib import closing
from dataclasses import dataclass, field, replace

from .protocol import Delivery, digest, error_code, integer, text, token
from .schema import open_db
from .store import AVAILABLE, SCOPE_SQL, EventStore, LeaseLost, Scope, StateConflict, prepare

LOG = logging.getLogger(__name__)
MAX_PAGE_ROWS = 100
MAX_PAGE_BYTES = 2_097_152


class PageRejected(ValueError):
    def __init__(self, receipts):
        self.receipts = tuple(receipts)
        self.receipt = next(receipt for receipt in self.receipts if receipt.reason != "PAGE_ROLLED_BACK")
        super().__init__(self.receipt.reason)


@dataclass(frozen=True, order=True)
class Position:
    timestamp: int
    key: str

    def __post_init__(self):
        integer(self.timestamp)
        text(self.key)


@dataclass(frozen=True)
class Cursor:
    watermark: int
    window_end: int | None = None
    position: Position | None = None

    def encode(self):
        return json.dumps({
            "version": 1, "completed_watermark": self.watermark,
            "window_start": self.watermark if self.window_end is not None else None,
            "window_end": self.window_end,
            "page_position": [self.position.timestamp, self.position.key] if self.position else None,
        }, separators=(",", ":"))

    @classmethod
    def decode(cls, encoded):
        data = json.loads(encoded)
        if data["version"] != 1:
            raise StateConflict("unsupported cursor version")
        return cls(data["completed_watermark"], data["window_end"],
                   Position(*data["page_position"]) if data["page_position"] else None)


@dataclass(frozen=True)
class Lease:
    scope: Scope
    stream: str
    epoch: int
    revision: int
    lease_until: int
    state: Cursor
    source_capability: str
    owner_token: str = field(repr=False)


@dataclass(frozen=True)
class PollRow:
    position: Position
    delivery: Delivery


@dataclass(frozen=True)
class PollPage:
    """Response carries the exact request lease used to fetch the page."""

    request: Lease
    rows: tuple[PollRow, ...]
    exhausted: bool


class CursorStore:
    def __init__(self, events: EventStore):
        self.events = events
        self.scope = events.scope

    def register(self, stream, *, capability, initial_watermark):
        keys = (*self.scope.keys, token(stream), token(capability))
        integer(initial_watermark)
        actor = self.events._actor("manage_cursor")
        with self.events._write() as db:
            connection = self.events._connection(db)
            now = self.events.now()
            db.execute("""INSERT INTO intake_cursors
                (tenant_id, connection_id, stream_key, source_capability, cursor, next_poll_at, config_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                       (*keys, Cursor(initial_watermark).encode(), now, connection["config_revision"]))
            self.events._audit(db, "intake_cursor_registered", now, actor_ref=actor, stream_key=stream)

    def _row(self, db, stream):
        return db.execute(f"""SELECT * FROM intake_cursors WHERE {SCOPE_SQL} AND stream_key = ?""",
                          (*self.scope.keys, stream)).fetchone()

    def get(self, stream):
        with closing(open_db(self.events.path)) as db:
            row = self._row(db, token(stream))
            return Cursor.decode(row["cursor"]) if row else None

    def claim(self, stream, *, window_end):
        token(stream)
        integer(window_end)
        with self.events._write() as db:
            now = self.events.now()
            connection = self.events._connection(db)
            row = self._row(db, stream)
            if row is None:
                raise StateConflict("polling stream is not registered")
            if (not self.events._available(connection, row["config_revision"])
                    or row["sync_status"] == "PAUSED" or row["next_poll_at"] > now
                    or (row["lease_until"] is not None and row["lease_until"] > now)):
                return None
            cursor = Cursor.decode(row["cursor"])
            if cursor.window_end is None:
                if not cursor.watermark < window_end <= now:
                    raise ValueError("invalid polling window")
                cursor = replace(cursor, window_end=window_end)
            epoch = row["claim_epoch"] + 1
            expiry = integer(now + self.events.policy.lease_ms)
            owner = secrets.token_hex(32)
            db.execute(f"""UPDATE intake_cursors SET cursor = ?, sync_status = 'RUNNING',
                lease_until = ?, claim_epoch = ?, ownership_digest = ?
                WHERE {SCOPE_SQL} AND stream_key = ?""",
                       (cursor.encode(), expiry, epoch, digest(owner.encode("ascii")),
                        *self.scope.keys, stream))
            lease = Lease(self.scope, stream, epoch, row["config_revision"], expiry, cursor,
                          row["source_capability"], owner)
        return lease

    def _validate_lease(self, db, lease, now):
        if lease.scope != self.scope or lease.lease_until <= now:
            raise LeaseLost("polling lease expired or outside scope")
        connection = self.events._connection(db)
        row = self._row(db, lease.stream)
        if (not self.events._available(connection, lease.revision) or row is None
                or (row["sync_status"], row["claim_epoch"], row["lease_until"],
                    row["config_revision"], row["cursor"], row["source_capability"],
                    row["ownership_digest"]) != (
                        "RUNNING", lease.epoch, lease.lease_until, lease.revision,
                        lease.state.encode(), lease.source_capability,
                        digest(text(lease.owner_token, 64).encode("utf-8")))):
            raise LeaseLost("polling position or authorization changed")
        return connection

    def _prepare_page(self, lease, rows, exhausted):
        if (type(exhausted) is not bool or not isinstance(rows, (list, tuple))
                or len(rows) > MAX_PAGE_ROWS or (not rows and not exhausted)):
            raise ValueError("invalid polling page")
        size = 0
        previous = lease.state.position
        details = []
        for row in rows:
            pos = row.position
            in_order = (lease.state.watermark < pos.timestamp <= lease.state.window_end
                        and (previous is None or pos > previous))
            delivery = row.delivery
            if delivery.scope != lease.scope or delivery.config_revision != lease.revision:
                raise LeaseLost("polling verification scope or generation mismatch")
            if (delivery.verified_via != "AUTHENTICATED_READ" or delivery.duplicate_only
                    or delivery.capability != lease.source_capability):
                raise ValueError("polling rows require current authenticated read evidence")
            if not isinstance(delivery.raw, bytes) or not isinstance(delivery.envelope, bytes):
                raise ValueError("polling row must contain bytes")
            size += len(delivery.raw) + len(delivery.envelope)
            if size > MAX_PAGE_BYTES:
                raise ValueError("polling page exceeds byte budget")
            item = prepare(delivery)
            if not in_order:
                item = replace(item, fact=None, reason="INVALID_POLL_POSITION")
            details.append(item)
            previous = pos
        return details, previous

    def commit_page(self, lease, page: PollPage, *, poll_interval_ms=0):
        integer(poll_interval_ms, 0, 86_400_000)
        if page.request != lease:
            raise LeaseLost("polling response belongs to a different request")
        exhausted = page.exhausted
        details, position = self._prepare_page(lease, page.rows, exhausted)
        rejected = None
        with self.events._write() as db:
            now = self.events.now()
            connection = self._validate_lease(db, lease, now)
            db.execute("SAVEPOINT intake_page")
            tentative = [self.events._accept(db, item, connection, now) for item in details]
            if any(receipt.outcome == "REJECTED" for receipt in tentative):
                db.execute("ROLLBACK TO intake_page")
                rejected = []
                for item, receipt in zip(details, tentative):
                    audit = self.events._receipt(
                        db, now, "REJECTED", reason=receipt.reason or "PAGE_ROLLED_BACK",
                        raw_hash=item.raw_digest, transport_hash=item.transport_digest,
                        batch_id=item.delivery.batch_id if not item.reason else None,
                    )
                    self.events._audit_collision(db, audit, now)
                    rejected.append(audit)
            db.execute("RELEASE intake_page")
            if rejected is None:
                next_state = (Cursor(lease.state.window_end) if exhausted
                              else replace(lease.state, position=position))
                # Check time again after processing the whole bounded page.
                now = self.events.now()
                self._validate_lease(db, lease, now)
                next_poll = integer(now + poll_interval_ms) if exhausted else now
                updated = db.execute(f"""UPDATE intake_cursors SET cursor = ?, sync_status = ?,
                    lease_until = ?, ownership_digest = ?, next_poll_at = ?, last_error = NULL,
                    last_sync_at = COALESCE(?, last_sync_at)
                    WHERE {SCOPE_SQL} AND stream_key = ? AND claim_epoch = ? AND cursor = ?""",
                                     (next_state.encode(), "IDLE" if exhausted else "RUNNING",
                                      None if exhausted else lease.lease_until,
                                      None if exhausted else digest(lease.owner_token.encode("ascii")), next_poll,
                                      now if exhausted else None,
                                      *self.scope.keys, lease.stream, lease.epoch, lease.state.encode()))
                if updated.rowcount != 1:
                    raise LeaseLost("polling page compare-and-swap failed")
        if rejected is not None:
            for receipt in rejected:
                if receipt.reason == "FACT_IDENTITY_COLLISION":
                    LOG.warning("Intake fact identity collision, receipt %s", receipt.receipt_id)
            raise PageRejected(rejected)
        return None if exhausted else replace(lease, state=next_state)

    def fail_page(self, lease, reason, *, retry_after_ms):
        error_code(reason)
        integer(retry_after_ms, 1)
        with self.events._write() as db:
            now = self.events.now()
            self._validate_lease(db, lease, now)
            db.execute(f"""UPDATE intake_cursors SET sync_status = 'RETRY_WAIT',
                lease_until = NULL, ownership_digest = NULL, last_error = ?, next_poll_at = ?
                WHERE {SCOPE_SQL} AND stream_key = ?""",
                       (reason, integer(now + retry_after_ms), *self.scope.keys, lease.stream))

    def rebind_revision(self, stream, *, previous_revision, reason):
        """Authorized confirmation that the keyset semantics still match."""
        token(stream)
        integer(previous_revision, 1)
        reason = text(reason)
        actor = self.events._actor("manage_cursor")
        with self.events._write() as db:
            now = self.events.now()
            connection = self.events._connection(db)
            revision = connection["config_revision"]
            if connection["status"] not in AVAILABLE or revision <= previous_revision:
                raise StateConflict("polling rebind requires a newer active connection revision")
            changed = db.execute(f"""UPDATE intake_cursors SET sync_status = 'IDLE',
                config_revision = ?, lease_until = NULL, ownership_digest = NULL,
                next_poll_at = ?, last_error = NULL
                WHERE {SCOPE_SQL} AND stream_key = ? AND config_revision = ?""",
                                 (revision, now, *self.scope.keys, stream, previous_revision))
            if changed.rowcount != 1:
                raise StateConflict("polling configuration revision changed")
            self.events._audit(db, "intake_cursor_rebound", now, actor_ref=actor, stream_key=stream,
                               previous_revision=previous_revision, revision=revision, reason=reason)
