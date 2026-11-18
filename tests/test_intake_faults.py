import random
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from contextlib import closing

import pytest

from herdr.intake.cursor import CursorStore
from herdr.intake.schema import BUSY_TIMEOUT_MS, open_db
from herdr.intake.store import EventStore, LeaseLost, Policy

from tests.test_intake_cursor import observed, submit
from tests.test_intake_store import clock, delivery, store, verify_synthetic


def crash(store, code):
    setup = """
import os
import sys
from pathlib import Path
from herdr.intake import store as module
from herdr.intake.cursor import CursorStore
from herdr.intake.store import EventStore, Policy, Scope
from tests.test_intake_cursor import observed, submit
from tests.test_intake_store import delivery, verify_synthetic

store = EventStore(Path(sys.argv[1]), Scope("tenant", "one"),
                   verifier=verify_synthetic, clock=lambda: int(sys.argv[2]))
real_open = module.open_db
def die_on_commit(*args):
    db = real_open(*args)
    db.set_trace_callback(lambda query: os._exit(37) if query.strip().upper() == "COMMIT" else None)
    return db
"""
    result = subprocess.run([sys.executable, "-c", setup + code, str(store.path), str(store.now())],
                            timeout=10, capture_output=True)
    assert result.returncode == 37, result.stderr.decode()


def test_process_crash_before_commit_and_after_commit_before_ack(store):
    crash(store, """
module.open_db = die_on_commit
store.accept_event(delivery())
""")
    assert store.list_events() == store.list_receipts() == []
    crash(store, """
store.accept_event(delivery())
os._exit(37)
""")
    duplicate = store.accept_event(delivery(transport="lost-ack-retry"))
    assert duplicate.outcome == "DUPLICATE"
    assert [row["event_id"] for row in store.list_events()] == [duplicate.event_id]
    assert Counter(row["outcome"] for row in store.list_receipts()) == {
        "ACCEPTED": 1, "DUPLICATE": 1}


def test_receipt_write_failure_rolls_back_event_and_retry_is_first_acceptance(store):
    with closing(open_db(store.path)) as db:
        db.execute("""CREATE TRIGGER fail_receipt BEFORE INSERT ON intake_delivery_receipts
            BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
            store.accept_event(delivery())
        assert store.list_events() == store.list_receipts() == []
        db.execute("DROP TRIGGER fail_receipt")
    assert store.accept_event(delivery()).outcome == "ACCEPTED"


def test_lock_contention_has_bounded_failure_and_no_ack(store):
    with closing(open_db(store.path)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.accept_event(delivery())
        elapsed = time.monotonic() - start
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert BUSY_TIMEOUT_MS / 1000 * 0.8 <= elapsed < 3
        assert store.list_events() == store.list_receipts() == []
        blocker.rollback()
    assert store.accept_event(delivery()).outcome == "ACCEPTED"


def test_consumer_crash_with_durable_attempt_and_retry_after_restart(store, clock):
    accepted = store.accept_event(delivery())
    crash(store, """
assert store.claim_next_event()
os._exit(37)
""")
    event = store.get_event(accepted.event_id)
    assert event["status"] == "PROCESSING"
    assert store.list_attempts(accepted.event_id)[0]["outcome"] is None
    successor = EventStore(store.path, store.scope, clock=lambda: clock[0])
    clock[0] = event["lease_until"]
    assert successor.recover_expired() == 1
    due = successor.get_event(accepted.event_id)["next_retry_at"]
    crash(store, """
assert store.claim_next_event() is None
os._exit(37)
""")
    clock[0] = due
    crash(store, """
assert store.claim_next_event()
os._exit(37)
""")
    clock[0] = store.get_event(accepted.event_id)["lease_until"]
    assert successor.recover_expired() == 1
    clock[0] = successor.get_event(accepted.event_id)["next_retry_at"]
    successor.complete_event(successor.claim_next_event())
    assert [item["outcome"] for item in store.list_attempts(accepted.event_id)] == [
        "RETRY_WAIT", "RETRY_WAIT", "PROCESSED"]


def test_cursor_process_crash_rolls_back_whole_page_then_resumes(store, clock):
    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    first = cursor.claim("orders", window_end=20)
    committed = submit(cursor, first, (observed("a", 11, "a"),), exhausted=False)
    clock[0] = committed.lease_until
    crash(store, """
cursor = CursorStore(store)
lease = cursor.claim("orders", window_end=20)
module.open_db = die_on_commit
submit(cursor, lease, (observed("b", 12, "b"),), exhausted=True)
""")
    assert cursor.get("orders") == committed.state
    assert [event["id"] for event in store.list_events()] == ["a"]
    assert len(store.list_receipts()) == 1
    clock[0] += store.policy.lease_ms
    crash(store, """
cursor = CursorStore(store)
lease = cursor.claim("orders", window_end=30)
submit(cursor, lease, (observed("b", 12, "b"),), exhausted=True)
os._exit(37)
""")
    assert cursor.get("orders").watermark == 20
    latest = cursor.claim("orders", window_end=30)
    assert latest.state.watermark == 20 and latest.state.position is None
    with pytest.raises(LeaseLost):
        submit(cursor, committed, (), exhausted=True)
    assert {event["id"] for event in store.list_events()} == {"a", "b"}
    assert len(store.list_receipts()) == 2


def test_thousand_events_with_retries_duplicates_and_restarts(store, clock):
    from herdr.intake.worker import run_once

    rng = random.Random(8785)
    policy = Policy(base_delay_ms=1, max_delay_ms=8, lease_ms=20)

    def restart():
        return EventStore(store.path, store.scope, authorizer=store.authorizer,
                          policy=policy, verifier=verify_synthetic, clock=lambda: clock[0])

    consumer = restart()
    cursor = CursorStore(consumer)
    cursor.register("orders", capability="changes", initial_watermark=0)
    lease = cursor.claim("orders", window_end=1000)
    for page in range(0, 1000, 40):
        rows = tuple(observed(str(i), i, str(i)) for i in range(page + 1, page + 41))
        lease = submit(cursor, lease, rows, exhausted=page == 960)
        if lease is not None:
            assert lease.state.position == rows[-1].position
        if rng.random() < 0.25:
            consumer = restart()
            cursor = CursorStore(consumer)
    for repeated in rng.sample(range(1, 1001), 200):
        assert consumer.accept_event(delivery(str(repeated), transport=f"retry-{repeated}")).outcome == "DUPLICATE"

    failures = restarts = 0
    for _ in range(5000):
        if rng.random() < 0.19:
            if rng.random() < 0.17:
                crash(consumer, """
store = EventStore(store.path, store.scope, clock=store.now,
                   policy=Policy(base_delay_ms=1, max_delay_ms=8, lease_ms=20))
store.claim_next_event()
os._exit(37)
""")
                consumer = restart()
                clock[0] += policy.lease_ms
                recovered = consumer.recover_expired()
                restarts += recovered
                failures += recovered
            else:
                claim = consumer.claim_next_event()
                if claim is None:
                    break
                consumer.fail_event(claim, "SYNTHETIC_TEMPORARY")
                failures += 1
        elif not run_once(consumer):
            break
        clock[0] += 10
    clock[0] += policy.max_delay_ms
    while run_once(consumer):
        pass

    assert failures > 100 and restarts > 10
    assert cursor.get("orders").watermark == 1000
    with closing(open_db(store.path)) as db:
        assert {row[0] for row in db.execute(
            "SELECT id FROM intake_events WHERE tenant_id=? AND connection_id=?", store.scope.keys
        )} == {str(index) for index in range(1, 1001)}
        assert dict(db.execute("""SELECT outcome, count(*) FROM intake_delivery_receipts
            WHERE tenant_id=? AND connection_id=? GROUP BY outcome""", store.scope.keys)) == {
            "ACCEPTED": 1000, "DUPLICATE": 200}
        assert db.execute("""SELECT count(*) FROM intake_attempts WHERE tenant_id=?
            AND connection_id=? AND outcome='PROCESSED'""", store.scope.keys).fetchone()[0] == 1000
        assert not db.execute("""SELECT event_id FROM intake_attempts
            WHERE tenant_id=? AND connection_id=? AND outcome='PROCESSED'
            GROUP BY event_id HAVING count(*) <> 1""", store.scope.keys).fetchall()
        assert db.execute("""SELECT count(*) FROM intake_events WHERE tenant_id=?
            AND connection_id=? AND status <> 'PROCESSED'""", store.scope.keys).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
