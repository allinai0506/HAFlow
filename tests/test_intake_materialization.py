"""Source facts through durable Intake, materialization and the existing Inbox."""

from dataclasses import replace
import json

import pytest

from tests.test_intake_store import clock, delivery, store


def fact(event_id="one", *, version=1, action="work", **overrides):
    candidate = delivery(event_id)
    envelope = json.loads(candidate.envelope)
    envelope["type"] = "com.haflow.intake.work-item.observed.v1"
    envelope["dataschema"] = "urn:haflow:intake:work-item:v1"
    envelope["time"] = "2026-01-01T12:00:00Z"
    envelope["data"] = {
        "object_type": "issue", "external_id": "ISSUE-123", "version": version,
        "action": action, "title": "Review issue", "description": "Review the report",
        "identities": {"assignee": "external-alice"},
        **overrides,
    }
    return replace(candidate, envelope=json.dumps(envelope).encode())


def test_worker_materializes_once_and_preserves_source(store, clock):
    from herdr.intake import worker
    from herdr.inbox.store import InboxStore

    first = store.accept_event(fact())
    duplicate = store.accept_event(fact())
    clock[0] += 1
    second = store.accept_event(fact("two", version=2, action="update", title="Updated issue"))
    assert first.event_id == duplicate.event_id
    for _ in range(2):
        worker.run_once(store, materialization={"user_id": "operator", "auto_assign": True})
    assert not worker.run_once(store, materialization={"user_id": "operator", "auto_assign": True})
    tasks, total = InboxStore(store.path).list_tasks(user_id="operator")
    assert total == 1
    assert tasks[0]["status"] == "DRAFT"
    assert tasks[0]["title"] == "Review issue"
    assert tasks[0]["project_id"] is None
    assert tasks[0]["workspace_id"] is None
    assert store.get_event(first.event_id)["link_id"] == store.get_event(second.event_id)["link_id"]
    assert store.list_attempts(first.event_id)[0]["outcome"] == "PROCESSED"


@pytest.mark.parametrize(("action", "title", "status"), [
    ("metadata", "Metadata only", "IGNORED"),
    ("closed", "Closed before opening", "PROCESSED"),
    ("work", 42, "REJECTED"),
])
def test_non_work_and_invalid_facts_never_create_tasks(store, action, title, status):
    from herdr.intake import worker
    from herdr.inbox.store import InboxStore

    receipt = store.accept_event(fact(action=action, title=title))
    worker.run_once(store, materialization={"user_id": "operator", "auto_assign": True})
    assert store.get_event(receipt.event_id)["status"] == status
    assert InboxStore(store.path).list_tasks() == ([], 0)


def test_operator_confirms_without_dispatch_and_can_remove_assignment(store, monkeypatch):
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.worker import run_once
    from herdr.inbox.dispatcher import InboxDispatcher
    from herdr.inbox.store import InboxStore

    registry = {"a": {"project_id": "project", "workspace_id": "workspace"}}
    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": registry})
    policy = {"user_id": "operator", "project_id": "project", "workspace_id": "workspace"}
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {
        "one": {"project_id": "project", "workspace_id": "workspace"},
    }}
    store.accept_event(fact())
    run_once(store, materialization={**policy, "auto_assign": False})
    inbox = IntakeInbox(store.path, config)
    item, = inbox.list_items()
    assert item["status"] == "PENDING_ASSIGNMENT"
    assert item["task_id"] is None
    assert item["identities"] == {"assignee": "external-alice"}
    assert item["receipt_id"]
    item = inbox.change(item["link_id"], "assign", item["revision"])
    task_id = item["task_id"]
    assert item["assignment_source"] == "MANUAL"
    assert item["status"] == "DRAFT"
    item = inbox.change(item["link_id"], "confirm", item["revision"])
    assert item["status"] == "CONFIRMED"
    assert InboxStore(store.path).get_task(task_id)["status"] == "DRAFT"
    assert InboxDispatcher(db_path=store.path).claim_next_task("workspace") is None
    item = inbox.change(item["link_id"], "unassign", item["revision"])
    assert item["status"] == "PENDING_ASSIGNMENT"
    store.accept_event(fact("later", version=2))
    run_once(store, materialization={**policy, "auto_assign": True})
    item, = inbox.list_items()
    assert item["status"] == "PENDING_ASSIGNMENT"
    assert item["assignment_source"] == "MANUAL"
    assert item["task_id"] == task_id
    with pytest.raises(Exception, match="may not be dispatched"):
        InboxStore(store.path).confirm_task_scope(task_id, "project", "workspace")
    history = store.list_admin_audit()
    assert [r["origin"] for r in history if r["event_type"] == "intake_assignment"] == [
        "AUTO", "MANUAL", "MANUAL",
    ]


def test_competing_claims_and_duplicate_materialization(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from herdr.inbox.store import InboxStore
    from herdr.intake.materializer import materialize
    from herdr.intake.schema import initialize_materialization
    from herdr.intake.store import EventStore, LeaseLost

    initialize_materialization(store.path)
    store.accept_event(fact("a"))
    store.accept_event(fact("b"))
    workers = [EventStore(store.path, store.scope, clock=store.now) for _ in range(2)]
    claims = [item.claim_next_event() for item in workers]
    gate = Barrier(2)

    def apply(pair):
        worker, claim = pair
        gate.wait(timeout=5)
        return materialize(worker, claim, {"user_id": "operator"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(apply, zip(workers, claims))) == ["IGNORED", "PROCESSED"]
    with pytest.raises(LeaseLost):
        materialize(store, claims[0], {"user_id": "operator"})
    tasks, total = InboxStore(store.path).list_tasks()
    assert total == 1
    events = [store.get_event(claim.event_id) for claim in claims]
    assert events[0]["link_id"] == events[1]["link_id"] == tasks[0]["source_id"]


@pytest.mark.parametrize("point", ["task_insert", "link_update", "event_finish"])
def test_sql_failure_rolls_back_all_effects_and_restart_recovers(store, clock, point):
    import sqlite3
    from contextlib import closing

    from herdr.intake.materializer import materialize
    from herdr.intake.schema import initialize_materialization, open_db
    from herdr.intake.store import EventStore
    from herdr.intake.worker import run_once

    initialize_materialization(store.path)
    receipt = store.accept_event(fact())
    claim = store.claim_next_event()
    failure = {
        "task_insert": "INSERT ON user_inbox_tasks",
        "link_update": "UPDATE OF task_id ON intake_task_links WHEN NEW.task_id IS NOT NULL",
        "event_finish": "UPDATE OF status ON intake_events WHEN NEW.status = 'PROCESSED'",
    }
    with closing(open_db(store.path)) as db:
        db.execute(f"""CREATE TRIGGER inject_failure BEFORE {failure[point]}
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        materialize(store, claim, {"user_id": "operator"})
    with closing(open_db(store.path)) as db:
        for table in ("user_inbox_tasks", "intake_task_links", "intake_objects"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        db.execute("DROP TRIGGER inject_failure")
    assert store.get_event(receipt.event_id)["status"] == "PROCESSING"
    assert store.list_attempts(receipt.event_id)[0]["outcome"] is None
    successor = EventStore(store.path, store.scope, clock=store.now)
    clock[0] = claim.lease_until
    assert successor.recover_expired() == 1
    clock[0] = store.get_event(receipt.event_id)["next_retry_at"]
    assert run_once(successor, materialization={"user_id": "operator"})
    assert store.get_event(receipt.event_id)["status"] == "PROCESSED"
    with closing(open_db(store.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM user_inbox_tasks").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.parametrize("point", ["before", "after"])
def test_worker_process_exit_cannot_leave_orphan(store, clock, point):
    import subprocess
    import sys
    from contextlib import closing

    from herdr.intake.schema import initialize_materialization, open_db
    from herdr.intake.worker import run_once

    initialize_materialization(store.path)
    receipt = store.accept_event(fact())
    code = """
import os, sys
from pathlib import Path
from herdr.inbox.store import InboxStore
from herdr.intake.store import EventStore, Scope
from herdr.intake.worker import run_once
original = InboxStore.insert_task
def interrupt(*args, **kwargs):
    if sys.argv[3] == 'after':
        original(*args, **kwargs)
    os._exit(74)
InboxStore.insert_task = staticmethod(interrupt)
store = EventStore(Path(sys.argv[1]), Scope('tenant','one'), clock=lambda: int(sys.argv[2]))
run_once(store, materialization={'user_id':'operator'})
"""
    result = subprocess.run([sys.executable, "-c", code, str(store.path), str(clock[0]), point],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 74, result.stderr
    event = store.get_event(receipt.event_id)
    assert event["status"] == "PROCESSING"
    with closing(open_db(store.path)) as db:
        for table in ("user_inbox_tasks", "intake_task_links", "intake_objects"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    clock[0] = event["lease_until"]
    assert store.recover_expired() == 1
    clock[0] = store.get_event(receipt.event_id)["next_retry_at"]
    assert run_once(store, materialization={"user_id": "operator"})
    assert store.get_event(receipt.event_id)["link_id"]


def test_edit_mapping_survives_external_events_and_closed_item_stays_closed(store, clock, monkeypatch):
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.store import StateConflict
    from herdr.intake.worker import run_once

    registry = {"a": {"project_id": "project-a", "workspace_id": "ws-a"},
                "b": {"project_id": "project-b", "workspace_id": "ws-b"}}
    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": registry})
    settings = {"user_id": "operator", "project_id": "project-a", "workspace_id": "ws-a"}
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {
        "one": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"},
    }}
    store.accept_event(fact())
    run_once(store, materialization=settings)
    inbox = IntakeInbox(store.path, config)
    item, = inbox.list_items()
    assert (item["project_id"], item["workspace_id"]) == ("project-a", "ws-a")
    item = inbox.change(item["link_id"], "edit", item["revision"], {
        "title": "Operator edit", "description": "Local description", "priority": "HIGH",
        "project_id": "project-b", "workspace_id": "ws-b",
    })
    with pytest.raises(StateConflict, match="refresh"):
        inbox.change(item["link_id"], "confirm", item["revision"] - 1)
    clock[0] += 1
    store.accept_event(fact("updated", version=2, title="External edit", action="update"))
    run_once(store, materialization=settings)
    item, = inbox.list_items()
    assert (item["title"], item["project_id"], item["workspace_id"], item["priority"]) == (
        "Operator edit", "project-b", "ws-b", "HIGH")
    item = inbox.change(item["link_id"], "ignore", item["revision"])
    assert item["status"] == "IGNORED"
    item = inbox.change(item["link_id"], "reactivate", item["revision"])
    assert item["status"] == "PENDING_ASSIGNMENT"
    old_task = item["task_id"]
    item = inbox.change(item["link_id"], "assign", item["revision"])
    assert item["task_id"] == old_task
    for n, action in [(3, "closed"), (4, "work")]:
        clock[0] += 1
        store.accept_event(fact(str(n), version=n, action=action))
        run_once(store, materialization=settings)
    item, = inbox.list_items()
    assert item["task_id"] == old_task
    assert item["status"] == "SOURCE_CLOSED"
    with pytest.raises(StateConflict, match="closed"):
        inbox.change(item["link_id"], "confirm", item["revision"])


def test_scope_mapping_conflict_and_update_before_work(store, clock, monkeypatch):
    from herdr.inbox.store import InboxStore
    from herdr.intake.worker import run_once

    registry = {"a": {"project_id": "project", "workspace_id": "ws"},
                "b": {"project_id": "project", "workspace_id": "ws-other"}}
    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": registry})
    settings = {"user_id": "operator", "project_id": "project", "workspace_id": "ws"}
    store.accept_event(fact("update", version=2, title="Latest title", action="update"))
    run_once(store, materialization=settings)
    clock[0] += 1
    store.accept_event(fact("older-work"))
    run_once(store, materialization=settings)
    tasks, total = InboxStore(store.path).list_tasks(user_id="operator")
    assert total == 1
    assert tasks[0]["title"] == "Latest title"
    assert tasks[0]["project_id"] is None and tasks[0]["workspace_id"] is None


def test_console_http_auth_and_no_legacy_inbox_access(store, monkeypatch):
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    from threading import Thread

    from console.http_server import Handler
    from herdr.intake.worker import run_once

    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": {
        "p": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"},
    }})
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {
        "one": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"}
    }}
    monkeypatch.setenv("HERDR_STATE_DB", str(store.path))
    monkeypatch.setenv("HAFLOW_USER", "admin")
    monkeypatch.setenv("HAFLOW_INTAKE_INBOX_CONFIG", json.dumps(config))
    monkeypatch.setenv("HAFLOW_INTAKE_INBOX_TOKEN", "a" * 40)
    monkeypatch.setattr("console.inbox_api._store_instance", None)
    store.accept_event(fact())
    run_once(store, materialization={"user_id": "operator", "project_id": "haflow-1de29d2d", "workspace_id": "w1H"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, body=None, credential=None):
        headers = {"X-HAFlow-Console-Request": "1"}
        if credential:
            headers["X-HAFlow-Intake-Inbox-Token"] = credential
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET" if body is None else "POST", path,
                               body=json.dumps(body) if body is not None else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        assert request("/api/inbox/intake")[0] == 401
        status, payload = request("/api/inbox/intake", credential="a" * 40)
        assert status == 200
        item, = payload["data"]["items"]
        assert item["provider"] == "synthetic" and item["receipt_id"]
        assert item["occurred_time"] == "2026-01-01T12:00:00Z"
        assert request("/api/inbox/tasks?user_id=all")[1]["data"]["tasks"] == []
        assert request("/api/inbox/task?id=" + item["task_id"])[0] == 404
        body = {"action": "confirm", "link_id": item["link_id"], "revision": item["revision"]}
        assert request("/api/inbox/intake", {**body, "user_id": "other"}, "a" * 40)[0] == 400
        assert request("/api/inbox/intake", body)[0] == 401
        assert request("/api/inbox/intake", body, "a" * 40)[1]["data"]["item"]["status"] == "CONFIRMED"
        config["tenant_id"] = "foreign"
        monkeypatch.setenv("HAFLOW_INTAKE_INBOX_CONFIG", json.dumps(config))
        assert request("/api/inbox/intake", credential="a" * 40)[0] == 404
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_policy_unavailable_is_recoverable(store, clock):
    from herdr.intake.worker import run_once

    receipt = store.accept_event(fact())
    assert run_once(store)
    delayed = store.get_event(receipt.event_id)
    assert (delayed["status"], delayed["last_error"]) == (
        "RETRY_WAIT", "MATERIALIZATION_POLICY_UNAVAILABLE")
    clock[0] = delayed["next_retry_at"]
    assert run_once(store, materialization={"user_id": "operator"})
    assert store.get_event(receipt.event_id)["link_id"]


def test_durable_claim_recovers_after_real_sqlite_writer_contention(store, clock):
    import sqlite3
    from contextlib import closing

    from herdr.inbox.store import InboxStore
    from herdr.intake.materializer import materialize
    from herdr.intake.schema import initialize_materialization, open_db
    from herdr.intake.worker import run_once

    initialize_materialization(store.path)
    receipt = store.accept_event(fact())
    claim = store.claim_next_event()
    with closing(open_db(store.path)) as locker:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as failure:
            materialize(store, claim, {"user_id": "operator"})
        assert failure.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
    assert InboxStore(store.path).list_tasks() == ([], 0)
    assert not store.get_event(receipt.event_id)["link_id"]
    clock[0] = claim.lease_until
    assert store.recover_expired() == 1
    clock[0] = store.get_event(receipt.event_id)["next_retry_at"]
    assert run_once(store, materialization={"user_id": "operator"})
    assert InboxStore(store.path).list_tasks()[1] == 1


def test_authorization_rotation_hides_old_draft_and_prevents_old_ingress_materialization(store, clock):
    from herdr.inbox.store import InboxStore
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.worker import run_once

    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {
        "one": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"},
    }}
    inbox = IntakeInbox(store.path, config)
    settings = {"user_id": "operator"}
    store.accept_event(fact())
    run_once(store, materialization=settings)
    assert len(inbox.list_items()) == 1
    receipt = store.accept_event(fact("old", external_id="ISSUE-456"))
    store.update_connection_authorization(1, status="DISABLED", reason="Revoke visibility")
    assert inbox.list_items() == []
    store.update_connection_authorization(2, status="ACTIVE", reason="Approve new credentials",
                                          auth_ref="replacement")
    assert inbox.list_items() == []
    run_once(store, materialization=settings)
    assert store.get_event(receipt.event_id)["last_error"] == "SOURCE_AUTHORIZATION_CHANGED"
    clock[0] += 1
    replacement = store.accept_event(replace(fact("new", version=2), config_revision=3))
    run_once(store, materialization=settings)
    assert store.get_event(replacement.event_id)["status"] == "PROCESSED"
    assert inbox.list_items() == []
    assert InboxStore(store.path).list_tasks()[1] == 1


def test_second_authorized_connection_can_close_same_object(store, clock):
    from herdr.inbox.store import InboxStore
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.store import EventStore, Scope
    from herdr.intake.worker import run_once

    other = EventStore(store.path, Scope("tenant", "two"), clock=store.now,
                       verifier=lambda _: True, authorizer=store.authorizer)
    other.register_connection(provider="synthetic", instance="account",
                              display_name="Other", connector_version="1.0.0")
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {"one": {}, "two": {}}}
    settings = {"user_id": "operator", "allowed_connections": ("one", "two")}
    first = store.accept_event(fact())
    run_once(store, materialization=settings)
    clock[0] += 1
    receipt = other.accept_event(replace(fact("other-close", version=2, action="closed"),
                                         scope=other.scope))
    run_once(other, materialization=settings)
    assert other.get_event(receipt.event_id)["link_id"] == store.get_event(first.event_id)["link_id"]
    item, = IntakeInbox(store.path, config).list_items()
    assert (item["status"], item["connection_id"]) == ("SOURCE_CLOSED", "two")
    assert InboxStore(store.path).list_tasks()[1] == 1
    other_only = {**config, "connections": {"two": {}}}
    assert IntakeInbox(store.path, other_only).list_items() == []


def test_late_close_and_conflicting_equal_version_block_confirmation(store, clock):
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.store import StateConflict
    from herdr.intake.worker import run_once

    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {
        "one": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"},
    }}
    settings = {"user_id": "operator"}
    inbox = IntakeInbox(store.path, config)
    store.accept_event(fact(version=4))
    run_once(store, materialization=settings)
    clock[0] += 1
    store.accept_event(fact("conflict", version=4, title="Untrusted equal version"))
    run_once(store, materialization=settings)
    item, = inbox.list_items()
    assert item["status"] == "SOURCE_CONFLICT"
    with pytest.raises(StateConflict):
        inbox.change(item["link_id"], "confirm", item["revision"])
    clock[0] += 1
    close = store.accept_event(fact("old-close", version=3, action="closed"))
    run_once(store, materialization=settings)
    item, = inbox.list_items()
    assert item["status"] == "SOURCE_CLOSED"
    assert item["source_title"] == "Review issue"
    assert (item["event_id"], item["receipt_id"]) == (close.event_id, close.receipt_id)


def test_worker_cli_reads_trusted_instance_policy(store, clock, monkeypatch):
    import os
    import signal
    import subprocess
    import sys
    import time

    from herdr.intake.inbox import IntakeInbox

    from pathlib import Path
    ctrl = Path.home() / ".herdr-controller"
    ctrl.mkdir(parents=True, exist_ok=True)
    (ctrl / "projects.json").write_text(json.dumps({
        "version": 1,
        "projects": {"/path": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"}},
    }))
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {
        "one": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"},
    }}
    clock[0] = time.time_ns() // 1_000_000
    receipt = store.accept_event(fact())
    monkeypatch.setenv("HERDR_STATE_DB", str(store.path))
    monkeypatch.setenv("HAFLOW_INTAKE_INBOX_CONFIG", json.dumps(config))
    cmd = [sys.executable, "services/herdr-intake.py", "--tenant", "tenant", "--connection", "one"]
    with subprocess.Popen(cmd, env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE) as worker:
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and worker.poll() is None:
                if store.get_event(receipt.event_id)["status"] == "PROCESSED":
                    break
                time.sleep(0.02)
            item, = IntakeInbox(store.path, config).list_items()
            assert item["status"] == "DRAFT" and item["receipt_id"] == receipt.receipt_id
        finally:
            if worker.poll() is None:
                worker.send_signal(signal.SIGTERM)
            _, stderr = worker.communicate(timeout=5)
    assert worker.returncode == 0, stderr.decode()


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_task_body_source_revocation_applies_after_last_observation_moves(store, clock, mode):
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.store import EventStore, Scope
    from herdr.intake.worker import run_once

    other = EventStore(store.path, Scope("tenant", "two"), clock=store.now,
                       verifier=lambda _: True, authorizer=store.authorizer)
    other.register_connection(provider="synthetic", instance="account",
                              display_name="Other", connector_version="1.0.0")
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {"one": {}, "two": {}}}
    settings = {"user_id": "operator", "allowed_connections": ("one", "two"),
                "auto_assign": mode == "auto"}
    inbox = IntakeInbox(store.path, config)
    if mode == "manual":
        store.accept_event(fact())
        run_once(store, materialization=settings)
    other.accept_event(replace(fact("source-two", version=2, action="update", title="Source Two"),
                               scope=other.scope))
    run_once(other, materialization=settings)
    if mode == "auto":
        store.accept_event(fact())
        run_once(store, materialization=settings)
    else:
        item, = inbox.list_items()
        inbox.change(item["link_id"], "assign", item["revision"])
    store.accept_event(fact("source-one", version=3, action="update", title="Source One"))
    run_once(store, materialization=settings)
    item, = inbox.list_items()
    assert (item["title"], item["source_title"], item["connection_id"]) == (
        "Source Two", "Source One", "one")
    other.update_connection_authorization(1, status="DISABLED", reason="Revoke body access")
    assert inbox.list_items() == []


def test_projection_filters_before_pagination(store, clock, monkeypatch):
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.worker import run_once

    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": {
        "a": {"project_id": "alpha", "workspace_id": "ws"},
        "b": {"project_id": "beta", "workspace_id": "ws"},
    }})
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {
        "one": {"project_id": "haflow-1de29d2d", "workspace_id": "w1H"},
    }}
    for name, project in [("old-match", "alpha"), ("new-other", "beta"), ("new-match", "alpha")]:
        clock[0] += 1
        store.accept_event(fact(name, external_id=name))
        run_once(store, materialization={"user_id": "operator", "project_id": project,
                                         "workspace_id": "ws"})
    inbox = IntakeInbox(store.path, config)
    assert [row["external_id"] for row in inbox.list_items(project="alpha", limit=1)] == ["new-match"]
    assert [row["external_id"] for row in inbox.list_items(project="alpha", limit=1, offset=1)] == ["old-match"]


def test_upgrade_preserves_manual_rows_and_reserves_no_source_flag(store):
    from herdr.inbox.store import InboxStore
    from herdr.intake.schema import initialize_materialization

    inbox = InboxStore(store.path)
    task = inbox.create_task("Historical manual task", "Still editable", source_type="active_intake")
    initialize_materialization(store.path)
    initialize_materialization(store.path)
    manual, = inbox.list_tasks(manual_only=True)[0]
    assert manual["id"] == task["id"]
    assert inbox.confirm_task_scope(task["id"], "project", "workspace")["status"] == "PENDING"


def test_expired_event_beyond_retention_never_materializes_task(store, clock):
    from herdr.intake.worker import run_once
    from herdr.inbox.store import InboxStore

    receipt = store.accept_event(fact())
    # Advance clock beyond retention_ms (90 days)
    clock[0] += store.policy.retention_ms + 1000
    assert run_once(store, materialization={"user_id": "operator", "auto_assign": True})
    event = store.get_event(receipt.event_id)
    assert event["status"] == "IGNORED"
    assert event["last_error"] == "NO_RETAINED_CANONICAL_DATA"
    assert InboxStore(store.path).list_tasks() == ([], 0)


def test_closed_and_conflict_items_support_unassign_and_ignore(store, clock, monkeypatch):
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.worker import run_once

    registry = {"a": {"project_id": "p", "workspace_id": "w"}}
    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": registry})
    policy = {"user_id": "operator", "project_id": "p", "workspace_id": "w"}
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {"one": {
        "project_id": "p", "workspace_id": "w",
    }}}
    inbox = IntakeInbox(store.path, config)

    # 1. Closed external item: support unassign and ignore
    store.accept_event(fact("item1", external_id="EXT-1", version=1))
    run_once(store, materialization=policy)
    clock[0] += 1
    store.accept_event(fact("close1", external_id="EXT-1", version=2, action="closed"))
    run_once(store, materialization=policy)
    item, = inbox.list_items()
    assert item["status"] == "SOURCE_CLOSED"
    assert item["disposition"] == "LINKED"
    item = inbox.change(item["link_id"], "unassign", item["revision"])
    assert item["disposition"] == "UNASSIGNED"
    item = inbox.change(item["link_id"], "ignore", item["revision"])
    assert item["status"] == "IGNORED"

    # 2. Conflicting external item: support unassign and ignore
    clock[0] += 1
    store.accept_event(fact("item2", external_id="EXT-2", version=1))
    run_once(store, materialization=policy)
    clock[0] += 1
    store.accept_event(fact("conflict2", external_id="EXT-2", version=1, title="Conflicting title"))
    run_once(store, materialization=policy)
    item = [i for i in inbox.list_items() if i["external_id"] == "EXT-2"][0]
    assert item["status"] == "SOURCE_CONFLICT"
    assert item["disposition"] == "LINKED"
    item = inbox.change(item["link_id"], "unassign", item["revision"])
    assert item["disposition"] == "UNASSIGNED"
    item = inbox.change(item["link_id"], "ignore", item["revision"])
    assert item["status"] == "IGNORED"


def test_missing_scope_enters_pending_assignment_and_blocks_confirmation_until_assigned(store, monkeypatch):
    import pytest
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.store import StateConflict
    from herdr.intake.worker import run_once
    from herdr.inbox.store import InboxStore

    registry = {"a": {"project_id": "p-valid", "workspace_id": "w-valid"}}
    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": registry})
    config = {"tenant_id": "tenant", "user_id": "operator", "connections": {"one": {}}}
    # Materialize without project or workspace
    store.accept_event(fact())
    run_once(store, materialization={"user_id": "operator", "auto_assign": True})

    inbox = IntakeInbox(store.path, config)
    item, = inbox.list_items()
    # In SQLite, task exists as DRAFT
    tasks, total = InboxStore(store.path).list_tasks(user_id="operator")
    assert total == 1 and tasks[0]["status"] == "DRAFT"
    assert tasks[0]["project_id"] is None and tasks[0]["workspace_id"] is None

    # But visible projection is PENDING_ASSIGNMENT because execution scope is missing
    assert item["status"] == "PENDING_ASSIGNMENT"
    assert item["disposition"] == "LINKED"
    assert item["task_id"] == tasks[0]["id"]
    assert len(inbox.list_items(view="PENDING_ASSIGNMENT")) == 1
    assert len(inbox.list_items(view="DRAFT")) == 0

    # Cannot confirm without project/workspace
    with pytest.raises(StateConflict, match="project/workspace binding required"):
        inbox.change(item["link_id"], "confirm", item["revision"])

    # Edit to assign project and workspace
    item = inbox.change(item["link_id"], "edit", item["revision"], {
        "title": item["title"], "description": item["description"], "priority": "HIGH",
        "project_id": "p-valid", "workspace_id": "w-valid",
    })
    # Now project and workspace are set, item transitions to DRAFT
    assert item["status"] == "DRAFT"
    assert len(inbox.list_items(view="DRAFT")) == 1
    assert len(inbox.list_items(view="PENDING_ASSIGNMENT")) == 0

    # Now it can be confirmed
    item = inbox.change(item["link_id"], "confirm", item["revision"])
    assert item["status"] == "CONFIRMED"
