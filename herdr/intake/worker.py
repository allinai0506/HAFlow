"""Bounded consumption for one trusted Intake scope."""

import json
import logging
import sqlite3
import time
from threading import Event

from .materializer import EVENT_TYPE, materialize
from .schema import initialize_materialization
from .store import EventStore, LeaseLost

LOG = logging.getLogger(__name__)
IDLE_SECONDS = 0.25
MAINTENANCE_SECONDS = 5


def _lock_contention(exc):
    return getattr(exc, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _process(event):
    data = event["data_json"]
    if data is None:
        return "IGNORED", "NO_RETAINED_CANONICAL_DATA"
    # Stored facts have already passed the structured ingress validation.
    # Decode independently to distinguish damaged storage from input rejection.
    json.loads(data)
    return "PROCESSED", None


def run_once(store: EventStore, *, materialization=None):
    """Consume at most one event. The only result is a committed disposition."""
    if materialization is not None:
        initialize_materialization(store.path)
    claim = store.claim_next_event()
    if claim is None:
        return False
    try:
        try:
            if claim.type == EVENT_TYPE:
                if materialization is None:
                    store.fail_event(claim, "MATERIALIZATION_POLICY_UNAVAILABLE")
                else:
                    materialize(store, claim, materialization)
                return True
            event = store.get_event(claim.event_id)
            if event is None:
                raise RuntimeError("claimed intake event missing")
            outcome, reason = _process(event)
            store.complete_event(claim, outcome=outcome, reason=reason)
        except json.JSONDecodeError:
            store.fail_event(claim, "CANONICAL_DATA_CORRUPT", permanent=True)
        except sqlite3.IntegrityError:
            if claim.type != EVENT_TYPE:
                raise
            store.fail_event(claim, "MATERIALIZATION_INTEGRITY_ERROR")
        except sqlite3.OperationalError as exc:
            if not _lock_contention(exc):
                raise
            # If the short lock persists, run() backs off. The durable open
            # attempt will be recovered after its lease expires.
            store.fail_event(claim, "SQLITE_CONTENTION")
    except LeaseLost:
        LOG.info("Intake lost claim, event %s", claim.event_id)
    return True


def run(store: EventStore, stop: Event, *, materialization=None):
    """One in-flight claim with graceful shutdown."""
    next_maintenance = 0.0
    while not stop.is_set():
        try:
            tick = time.monotonic()
            if tick >= next_maintenance:
                store.recover_expired()
                store.prune()
                next_maintenance = tick + MAINTENANCE_SECONDS
            if not stop.is_set() and run_once(store, materialization=materialization):
                continue
        except sqlite3.OperationalError as exc:
            if not _lock_contention(exc):
                raise
            LOG.warning("Intake SQLite write contention")
        stop.wait(IDLE_SECONDS)
