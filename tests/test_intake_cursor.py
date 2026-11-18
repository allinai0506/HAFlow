from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from tests.test_intake_store import clock, delivery, store


def submit(cursor, lease, rows, *, exhausted):
    from herdr.intake.cursor import PollPage

    return cursor.commit_page(lease, PollPage(lease, rows, exhausted))


def observed(event_id, at, key):
    from herdr.intake.cursor import PollRow, Position

    return PollRow(Position(at, key), replace(delivery(event_id),
                                              verified_via="AUTHENTICATED_READ"))


def test_page_commits_ledger_and_position_without_skipping(store, clock):
    from herdr.intake.cursor import CursorStore, Position
    from herdr.intake.store import LeaseLost

    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    lease = cursor.claim("orders", window_end=20)
    continuation = submit(
        cursor,         lease, (observed("a", 11, "a"), observed("b", 11, "b")), exhausted=False)
    assert continuation.state.position == Position(11, "b")
    with pytest.raises(LeaseLost):
        submit(cursor, lease, (observed("c", 12, "c"),), exhausted=True)
    with pytest.raises(ValueError):
        submit(cursor, continuation, (observed("c", 11, "a"),), exhausted=True)
    clock[0] = continuation.lease_until
    recovered = cursor.claim("orders", window_end=30)
    assert recovered.state == continuation.state
    assert recovered.epoch == continuation.epoch + 1
    with pytest.raises(LeaseLost):
        submit(cursor, continuation, (), exhausted=True)
    assert submit(cursor, recovered, (observed("c", 20, "c"),), exhausted=True) is None
    assert cursor.get("orders").watermark == 20
    assert len(store.list_events()) == 3


def test_only_one_same_page_cas_wins(store):
    from herdr.intake.cursor import CursorStore
    from herdr.intake.store import LeaseLost

    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    lease = cursor.claim("orders", window_end=20)
    barrier = Barrier(2)

    def writer(event_id):
        barrier.wait(timeout=5)
        try:
            submit(cursor, lease, (observed(event_id, 11, "a"),), exhausted=False)
            return True
        except LeaseLost:
            return False

    with ThreadPoolExecutor(2) as executor:
        assert sorted(executor.map(writer, ("a", "b"))) == [False, True]
    assert len(store.list_events()) == 1


def test_bad_row_does_not_advance_or_commit_partial_page(store):
    from herdr.intake.cursor import CursorStore, PageRejected

    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    lease = cursor.claim("orders", window_end=20)
    good = observed("a", 11, "a")
    bad = observed("b", 11, "b")
    wrong = replace(bad.delivery, envelope=b'{"specversion":"1.0"}')
    with pytest.raises(PageRejected) as failure:
        submit(cursor, lease, (good, replace(bad, delivery=wrong)), exhausted=True)
    assert failure.value.receipt.reason == "INVALID_DELIVERY"
    assert cursor.get("orders") == lease.state
    assert not store.list_events()
    assert {row["reason_code"] for row in store.list_receipts()} == {
        "INVALID_DELIVERY", "PAGE_ROLLED_BACK"}
    assert submit(cursor, lease, (good, bad), exhausted=True) is None
    assert cursor.get("orders").watermark == 20


def test_cursor_rebind_preserves_position_and_requires_current_revision(store):
    from herdr.intake.cursor import CursorStore
    from herdr.intake.store import LeaseLost

    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    lease = cursor.claim("orders", window_end=20)
    advanced = submit(cursor, lease, (observed("a", 11, "a"),), exhausted=False)
    store.update_connection_authorization(1, status="ACTIVE", reason="ROTATED")
    with pytest.raises(LeaseLost):
        submit(cursor, advanced, (), exhausted=True)
    assert cursor.claim("orders", window_end=21) is None
    cursor.rebind_revision("orders", previous_revision=1, reason="SAME_POSITION_PROTOCOL")
    resumed = cursor.claim("orders", window_end=22)
    assert resumed.state == advanced.state
    assert resumed.revision == 2


def test_foreign_stream_response_and_source_do_not_move_watermark(store):
    from herdr.intake.cursor import CursorStore, PollPage
    from herdr.intake.store import LeaseLost

    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    cursor.register("invoices", capability="invoices", initial_watermark=10)
    orders = cursor.claim("orders", window_end=20)
    invoices = cursor.claim("invoices", window_end=20)
    with pytest.raises(LeaseLost):
        cursor.commit_page(invoices, PollPage(orders, (), True))
    with pytest.raises(ValueError):
        cursor.commit_page(invoices, PollPage(invoices, (observed("a", 11, "a"),), True))
    assert cursor.get("invoices") == invoices.state
    assert cursor.get("orders") == orders.state
    assert store.list_events() == store.list_receipts() == []


def test_cursor_position_metadata_does_not_grant_ownership(store):
    from herdr.intake.cursor import CursorStore
    from herdr.intake.store import LeaseLost

    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    claim = cursor.claim("orders", window_end=20)
    with pytest.raises(LeaseLost):
        submit(cursor, replace(claim, owner_token="0" * 64), (), exhausted=True)
    assert cursor.get("orders") == claim.state
    assert submit(cursor, claim, (), exhausted=True) is None


def test_structurally_attributable_bad_position_receipts_without_advancing(store):
    from herdr.intake.cursor import CursorStore, PageRejected

    cursor = CursorStore(store)
    cursor.register("orders", capability="changes", initial_watermark=10)
    lease = cursor.claim("orders", window_end=20)
    with pytest.raises(PageRejected) as caught:
        submit(cursor, lease, (observed("a", 11, "a"), observed("b", 21, "b")), exhausted=True)
    assert {receipt.reason for receipt in caught.value.receipts} == {
        "INVALID_POLL_POSITION", "PAGE_ROLLED_BACK"}
    assert cursor.get("orders") == lease.state
    assert len(store.list_receipts()) == 2 and store.list_events() == []
