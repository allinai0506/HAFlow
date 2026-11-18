import hashlib
import sqlite3

import pytest


def test_state_db_intake_migration_and_durable_connection(tmp_path):
    from herdr import state_db
    from herdr.intake.schema import initialize, open_db

    path = tmp_path / "state.db"
    state_db.init_db(path)
    with state_db.get_db_connection(path) as db:
        db.execute("INSERT INTO workflows(workflow_id) VALUES ('existing')")
    initialize(path)
    with open_db(path) as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT workflow_id FROM workflows").fetchone()[0] == "existing"
        assert db.execute("SELECT count(*) FROM intake_events").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO intake_cursors(tenant_id, connection_id, stream_key, source_capability, "
                       "cursor, next_poll_at, config_revision) VALUES ('a','wrong','stream','changes','{}',0,1)")


def test_jcs_number_unicode_and_duplicate_properties():
    from herdr.intake.protocol import canonical_json, parse_json

    assert canonical_json({"\ue000": 1e-7, "😀": -0.0, "a": 1e-6}) == (
        '{"a":0.000001,"😀":0,"\ue000":1e-7}'
    )
    with pytest.raises(ValueError):
        parse_json(b'{"same":1,"same":2}')
    with pytest.raises(ValueError):
        canonical_json({"bad": 2**53})


def envelope(event_id="event", data=None):
    import json

    return json.dumps({
        "specversion": "1.0",
        "source": "urn:haflow:intake:synthetic:account:changes",
        "id": event_id,
        "type": "com.haflow.intake.synthetic.changed.v1",
        "data": {"a": 1} if data is None else data,
    }).encode()


def delivery(event_id="event", data=None, *, transport="delivery-1", scope=None, revision=1):
    from herdr.intake.protocol import Delivery, Scope

    return Delivery(envelope(event_id, data), transport.encode(), "changes",
                    "LOCAL_JOB", scope or Scope("tenant", "one"), revision, transport_id=transport)


@pytest.fixture
def clock():
    value = [1700000000000]
    return value


@pytest.fixture
def store(tmp_path, clock):
    from herdr.intake.schema import initialize
    from herdr.intake.store import EventStore, Scope

    store = EventStore(initialize(tmp_path / "state.db"), Scope("tenant", "one"),
                       authorizer=lambda action, scope: "operator", clock=lambda: clock[0])
    store.register_connection(provider="synthetic", instance="account",
                              display_name="Synthetic", connector_version="1.0.0")
    return store


def test_commit_receipt_duplicate_and_collision(store):
    first = store.accept_event(delivery())
    claim = store.claim_next_event()
    store.complete_event(claim)
    duplicate = store.accept_event(delivery(transport="delivery-2"))
    conflict = store.accept_event(delivery(data={"a": 2}))
    assert (first.outcome, duplicate.outcome, conflict.outcome) == (
        "ACCEPTED", "DUPLICATE", "REJECTED")
    assert first.event_id == duplicate.event_id != conflict.event_id
    assert len({first.receipt_id, duplicate.receipt_id, conflict.receipt_id}) == 3
    event = store.get_event(first.event_id)
    assert event["raw_digest"] == hashlib.sha256(b"delivery-1").hexdigest()
    assert event["status"] == "PROCESSED"
    assert event["attempt_count"] == 1
    assert store.claim_next_event() is None


def test_lease_fencing_recovery_and_authorized_replay(store, clock):
    from herdr.intake.store import LeaseLost

    first = store.accept_event(delivery())
    claim = store.claim_next_event()
    clock[0] = claim.lease_until
    with pytest.raises(LeaseLost):
        store.complete_event(claim)
    assert store.recover_expired() == 1
    waiting = store.get_event(first.event_id)
    assert waiting["status"] == "RETRY_WAIT"
    clock[0] = waiting["next_retry_at"]
    successor = store.claim_next_event()
    with pytest.raises(LeaseLost):
        store.complete_event(claim)
    store.fail_event(successor, "UNSUPPORTED_FORMAT", permanent=True)
    before = store.get_event(first.event_id)
    store.replay_event(first.event_id, successor.epoch, reason="Recheck service support")
    replay = store.claim_next_event()
    store.complete_event(replay)
    after = store.get_event(first.event_id)
    assert tuple(before[k] for k in ("event_id", "id", "source", "fact_digest", "data_json")) == tuple(
        after[k] for k in ("event_id", "id", "source", "fact_digest", "data_json"))
    assert after["status"] == "PROCESSED"
    assert [attempt["trigger"] for attempt in store.list_attempts(first.event_id)] == [
        "INITIAL", "RETRY", "REPLAY"]


def test_intake_migration_rolls_back_all_ddl(tmp_path, monkeypatch):
    from contextlib import closing

    from herdr import state_db
    from herdr.intake import schema

    path = tmp_path / "fresh.db"
    original = schema.DDL
    monkeypatch.setattr(schema, "DDL", (*original, "SELECT missing_column FROM intake_events"))
    with pytest.raises(sqlite3.OperationalError):
        schema.initialize(path)
    with closing(state_db.get_db_connection(path)) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name GLOB 'intake_*'").fetchall()
        assert not db.execute("SELECT 1 FROM schema_meta WHERE key = ?", (schema.MIGRATION_KEY,)).fetchall()
    monkeypatch.setattr(schema, "DDL", original)
    schema.initialize(path)
    schema.initialize(path)
    with closing(schema.open_db(path)) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND "
                          "name GLOB 'intake_*'").fetchone()[0] == 7
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_independent_connections_and_tenant_scope(store, clock):
    from contextlib import closing

    from herdr.intake.schema import open_db
    from herdr.intake.store import EventStore, Scope, ScopeDenied

    def connection(tenant, key):
        result = EventStore(store.path, Scope(tenant, key), authorizer=store.authorizer,
                            clock=lambda: clock[0])
        result.register_connection(provider="synthetic", instance="account",
                                   display_name="Synthetic", connector_version="1.0.0")
        return result

    first = store.accept_event(delivery())
    sibling = connection("tenant", "two")
    duplicate = sibling.accept_event(delivery(scope=sibling.scope))
    assert (duplicate.outcome, duplicate.event_id) == ("DUPLICATE", first.event_id)
    assert sibling.get_event(first.event_id) is None
    assert sibling.get_event_by_identity("urn:haflow:intake:synthetic:account:changes", "event") is None
    assert not sibling.list_events()
    assert not sibling.list_attempts(first.event_id)
    assert sibling.claim_next_event() is None

    other = connection("other", "three")
    accepted = other.accept_event(delivery(scope=other.scope))
    assert accepted.outcome == "ACCEPTED" and accepted.event_id != first.event_id
    assert other.get_event(first.event_id) is None
    unknown = EventStore(store.path, Scope("other", "one"))
    with pytest.raises(ScopeDenied):
        unknown.accept_event(delivery(scope=unknown.scope))
    with pytest.raises(ScopeDenied):
        unknown.record_rejection("BAD_SIGNATURE")
    with closing(open_db(store.path)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""UPDATE intake_delivery_receipts SET canonical_event_id = ?
                WHERE tenant_id = 'other' AND connection_id = 'three'""", (first.event_id,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO intake_events SELECT * FROM intake_events
                WHERE event_id = ?""", (first.event_id,))
    assert len(store.list_receipts()) == 1
    assert len(sibling.list_receipts()) == 1
    assert len(other.list_receipts()) == 1


def test_parallel_receipt_claim_and_replay(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from herdr.intake.store import StateConflict

    def race(operation):
        barrier = Barrier(2)

        def run(index):
            barrier.wait(timeout=5)
            return operation(index)

        with ThreadPoolExecutor(2) as executor:
            return list(executor.map(run, (1, 2)))

    receipts = race(lambda index: store.accept_event(delivery(transport=str(index))))
    assert sorted(receipt.outcome for receipt in receipts) == ["ACCEPTED", "DUPLICATE"]
    event_id = receipts[0].event_id
    assert receipts[1].event_id == event_id
    claims = [claim for claim in race(lambda _: store.claim_next_event()) if claim is not None]
    assert len(claims) == 1
    store.fail_event(claims[0], "PERMANENT", permanent=True)

    def replay(_):
        try:
            store.replay_event(event_id, claims[0].epoch, reason="Recovered")
            return True
        except StateConflict:
            return False

    assert sorted(race(replay)) == [False, True]
    claim = store.claim_next_event()
    store.complete_event(claim)
    assert len(store.list_attempts(event_id)) == 2


def test_retry_after_budget_retention_and_revocation(store, clock):
    from contextlib import closing

    from herdr.intake.schema import open_db
    from herdr.intake.store import EventStore, LeaseLost, ScopeDenied, StateConflict

    accepted = store.accept_event(delivery())
    first = store.claim_next_event()
    with closing(open_db(store.path)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE intake_events SET status = 'PROCESSED', lease_until = NULL "
                       "WHERE tenant_id = 'tenant' AND connection_id = 'one'")
    revision = store.update_connection_authorization(1, status="DISABLED", reason="REVOKE")
    assert store.get_event(accepted.event_id)["data_json"] is None
    assert store.claim_next_event() is None
    with pytest.raises(LeaseLost):
        store.complete_event(first)
    denied = store.accept_event(delivery("other", revision=revision))
    assert denied.outcome == "REJECTED"
    clock[0] = first.lease_until
    assert store.recover_expired() == 1
    store.update_connection_authorization(revision, status="ACTIVE", reason="REAUTHENTICATE")
    for i in range(2, store.policy.max_attempts + 1):
        clock[0] = store.get_event(accepted.event_id)["next_retry_at"]
        claim = store.claim_next_event()
        result = store.fail_event(claim, "TEMPORARY", retry_after_ms=901_000)
        if i != store.policy.max_attempts:
            assert result == "RETRY_WAIT"
            assert store.get_event(accepted.event_id)["next_retry_at"] == clock[0] + 901_000
    assert result == "DEAD_LETTER"
    unauthorized = EventStore(store.path, store.scope, clock=lambda: clock[0])
    with pytest.raises(ScopeDenied):
        unauthorized.replay_event(accepted.event_id, claim.epoch, reason="UNAUTHORIZED")
    clock[0] += store.policy.retention_ms
    assert store.prune() == (1, 2)
    with pytest.raises(StateConflict):
        store.replay_event(accepted.event_id, claim.epoch, reason="AFTER_EXPIRY")
    assert store.accept_event(delivery(revision=3)).outcome == "DUPLICATE"
    assert store.get_event(accepted.event_id)["status"] == "DEAD_LETTER"


def test_stale_and_invalid_envelope_rejected_with_scoped_receipt(store):
    from dataclasses import replace

    sample = delivery()
    stale = replace(sample, duplicate_only=True)
    assert store.accept_event(stale).reason == "STALE_UNKNOWN_FACT"
    invalid = replace(sample, envelope=sample.envelope.replace(b"synthetic:account", b"synthetic:foreign"))
    assert store.accept_event(invalid).reason == "SOURCE_BINDING_MISMATCH"
    unsupported = replace(sample, envelope=sample.envelope.replace(b'"1.0"', b'"1.0.2"'))
    assert store.accept_event(unsupported).reason == "INVALID_DELIVERY"
    assert not store.list_events()
    assert store.accept_event(sample).outcome == "ACCEPTED"
    assert store.accept_event(stale).outcome == "DUPLICATE"
    assert len(store.list_receipts()) == 5


def test_verified_delivery_cannot_cross_scope_or_revision(store):
    from herdr.intake.store import EventStore, Scope, ScopeDenied

    peer = EventStore(store.path, Scope("another", "peer"), authorizer=store.authorizer)
    peer.register_connection(provider="synthetic", instance="account", display_name="Peer",
                             connector_version="1.0.0")
    proof = delivery()
    with pytest.raises(ScopeDenied):
        peer.accept_event(proof)
    assert peer.list_receipts() == []
    store.update_connection_authorization(1, status="ACTIVE", reason="ROTATION")
    assert store.accept_event(proof).reason == "CONNECTION_UNAVAILABLE"
    assert store.accept_event(delivery(revision=2)).outcome == "ACCEPTED"


def test_admin_audit_is_scoped_and_private(store):
    from herdr import state_db
    from herdr.intake.store import EventStore, Scope, ScopeDenied

    peer = EventStore(store.path, Scope("foreign", "peer"), authorizer=store.authorizer)
    peer.register_connection(provider="synthetic", instance="account", display_name="Peer",
                             connector_version="1.0.0")
    accepted = store.accept_event(delivery())
    store.accept_event(delivery(data={"a": 2}))
    claim = store.claim_next_event()
    store.fail_event(claim, "PERMANENT", permanent=True)
    store.replay_event(accepted.event_id, claim.epoch, reason="Approved replay")
    items = store.list_admin_audit()
    assert [row["event_type"] for row in items] == [
        "intake_connection_registered", "intake_identity_collision", "intake_replay_requested"]
    assert items[-1]["reason"] == "Approved replay"
    assert items[-1]["actor_ref"] == "operator"
    assert {row["tenant_id"] for row in items} == {"tenant"}
    assert [row["tenant_id"] for row in peer.list_admin_audit()] == ["foreign"]
    assert not state_db.list_events(db_path=store.path, source="active_intake")
    assert not state_db.list_events(db_path=store.path)
    event = {"event_type": "intake_connection_registered", "source": "system",
             "payload": {"actor_ref": "operator"}}
    dedup = state_db.record_event_if_absent(event, identity_field="actor_ref",
                                            identity_value="operator", db_path=store.path)
    assert dedup["status"] == "created"
    assert "tenant_id" not in dedup["event"]["payload"]
    event["source"] = "active_intake"
    denied = state_db.record_event_if_absent(event, identity_field="actor_ref",
                                             identity_value="operator", db_path=store.path)
    assert denied["reason"] == "reserved_source"
    with pytest.raises(ScopeDenied):
        EventStore(store.path, store.scope).list_admin_audit()


def test_claim_requires_private_capability(store):
    from dataclasses import replace

    from herdr.intake.store import LeaseLost

    accepted = store.accept_event(delivery())
    claim = store.claim_next_event()
    assert "ownership_digest" not in store.list_attempts(accepted.event_id)[0]
    with pytest.raises(LeaseLost):
        store.complete_event(replace(claim, owner_token="0" * 64))
    assert store.get_event(accepted.event_id)["status"] == "PROCESSING"
    store.complete_event(claim)


def test_due_retry_does_not_starve_behind_newer_work(store, clock):
    receipt = store.accept_event(delivery("retry"))
    first = store.claim_next_event()
    store.fail_event(first, "TEMPORARY")
    clock[0] = store.get_event(receipt.event_id)["next_retry_at"] + 1
    store.accept_event(delivery("new"))
    assert store.claim_next_event().event_id == receipt.event_id


def test_exact_time_and_ijson_numbers_at_ingress(store):
    from dataclasses import replace

    from herdr.intake.protocol import canonical_json, parse_json

    base = delivery()

    def timed(stamp):
        return replace(base, envelope=base.envelope[:-1] + b',"time":"' + stamp + b'"}')

    exact = store.accept_event(timed(b"2025-01-01T00:00:00.00123Z"))
    assert exact.outcome == "ACCEPTED"
    assert store.get_event(exact.event_id)["occurred_at"] % 1000 == 1
    assert store.get_event(exact.event_id)["occurred_time"] == "2025-01-01T00:00:00.00123Z"
    assert store.accept_event(timed(b"2025-01-01T01:00:00.001230+01:00")).outcome == "DUPLICATE"
    assert store.accept_event(timed(b"2025-01-01T00:00:00.00124Z")).reason == "FACT_IDENTITY_COLLISION"
    assert store.accept_event(timed(b"2025-01-01T00:00:00+00:60")).reason == "INVALID_DELIVERY"
    for sample in (b"1e99999999999999999999999", b"1e-400", b"9007199254740993.0",
                   b"9007199254740992.1", br'"\ufdd0"', br'"\uffff"'):
        candidate = replace(base, envelope=base.envelope.replace(b'{"a": 1}', sample))
        assert store.accept_event(candidate).reason == "INVALID_DELIVERY"
    canonical = canonical_json(parse_json(b'{"n":1e20}'))
    assert canonical_json(parse_json(canonical.encode())) == canonical


def test_reinitialize_replaced_database_at_same_path(tmp_path):
    from contextlib import closing

    from herdr.intake.schema import initialize, open_db

    path = initialize(tmp_path / "state.db")
    path.unlink()
    initialize(path)
    with closing(open_db(path)) as db:
        assert not db.execute("SELECT 1 FROM intake_events").fetchall()
