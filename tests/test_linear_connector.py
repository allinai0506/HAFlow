"""Tests for Linear Connector V1 infrastructure, normalization, verification, and Intake integration."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from herdr.intake.protocol import Delivery, Scope
from herdr.intake.schema import initialize
from herdr.intake.store import EventStore
from herdr.inbox.store import InboxStore
from herdr.intake.inbox import IntakeInbox
from herdr.intake import worker


ORG_ID = "org-test-uuid-123"
SECRET = "test-webhook-secret-456"
SCOPE = Scope("tenant-test", "conn-linear")


def sign(payload_bytes: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


def sample_issue_payload(
    action="create",
    state_type="unstarted",
    *,
    issue_id="issue-uuid-001",
    identifier="TEST-101",
    title="Fix memory leak in engine",
    description="Engine exhausts memory after 100 iterations.",
    updated_at="2026-11-19T08:00:00.000Z",
    created_at="2026-11-19T08:00:00.000Z",
    org_id=ORG_ID,
    assignee_name="Alice Smith",
):
    return {
        "action": action,
        "type": "Issue",
        "createdAt": created_at,
        "webhookTimestamp": int(datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp() * 1000),
        "organizationId": org_id,
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": title,
            "description": description,
            "priority": 1,
            "url": f"https://linear.app/test-org/issue/{identifier}",
            "state": {
                "id": f"state-{state_type}",
                "name": state_type.capitalize(),
                "type": state_type,
            },
            "team": {
                "id": "team-test-uuid",
                "key": "TEST",
                "name": "Testing Team",
            },
            "assignee": {
                "id": "user-uuid-alice",
                "name": assignee_name,
            } if assignee_name else None,
            "updatedAt": updated_at,
            "createdAt": created_at,
        },
        "url": f"https://linear.app/test-org/issue/{identifier}",
        "webhookId": "wh-test-uuid-789",
    }


# =====================================================================
# Unit Tests: Manifest and Configuration
# =====================================================================


def test_manifest_and_config_validation():
    from herdr.intake.connectors.linear.manifest import LINEAR_MANIFEST, validate_config

    assert LINEAR_MANIFEST.provider == "linear"
    assert LINEAR_MANIFEST.manifest_version == "1"
    assert LINEAR_MANIFEST.connector_version == "1.0.0"
    assert "webhook" in LINEAR_MANIFEST.transports

    # Valid config
    valid = {
        "organization_id": ORG_ID,
        "webhook_signing_secret_ref": "secret-ref",
        "team_keys": ["TEST", "ENG"],
    }
    report = validate_config(valid)
    assert report.valid is True
    assert len(report.errors) == 0

    # Missing organization_id
    invalid_1 = {"webhook_signing_secret_ref": "secret-ref"}
    report_1 = validate_config(invalid_1)
    assert report_1.valid is False
    assert any("organization_id" in e for e in report_1.errors)

    # Missing secret_ref
    invalid_2 = {"organization_id": ORG_ID}
    report_2 = validate_config(invalid_2)
    assert report_2.valid is False
    assert any("webhook_signing_secret_ref" in e for e in report_2.errors)

    # Invalid team_keys format
    invalid_3 = {**valid, "team_keys": ["invalid-lowercase-key"]}
    report_3 = validate_config(invalid_3)
    assert report_3.valid is False


# =====================================================================
# Unit Tests: Signature Verification and Clock Skew
# =====================================================================


def test_verifier_valid_and_invalid_signatures():
    from herdr.intake.connectors.linear.verifier import (
        LinearVerifier,
        SignatureVerificationError,
        TimestampSkewError,
    )

    payload = json.dumps(sample_issue_payload()).encode("utf-8")
    sig = sign(payload, SECRET)
    now_ms = 1780747500000

    headers = {
        "Linear-Signature": sig,
        "Linear-Timestamp": str(now_ms),
        "Linear-Delivery": "delivery-uuid-1",
        "Linear-Event": "Issue",
    }

    # Valid signature and timestamp
    verifier = LinearVerifier(SECRET, max_skew_seconds=60)
    verified = verifier.verify(payload, headers, now_ms=now_ms)
    assert verified.signature_verified is True
    assert verified.delivery_id == "delivery-uuid-1"
    assert verified.event_type == "Issue"

    # Tampered payload
    tampered = payload + b" "
    with pytest.raises(SignatureVerificationError, match="INVALID_SIGNATURE"):
        verifier.verify(tampered, headers, now_ms=now_ms)

    # Wrong secret
    wrong_verifier = LinearVerifier("wrong-secret", max_skew_seconds=60)
    with pytest.raises(SignatureVerificationError, match="INVALID_SIGNATURE"):
        wrong_verifier.verify(payload, headers, now_ms=now_ms)

    # Missing signature header
    no_sig_headers = {k: v for k, v in headers.items() if k != "Linear-Signature"}
    with pytest.raises(SignatureVerificationError, match="MISSING_SIGNATURE"):
        verifier.verify(payload, no_sig_headers, now_ms=now_ms)

    # Signature with prefix (GitHub style 'sha256=') should fail because Linear is bare hex
    prefixed_headers = {**headers, "Linear-Signature": f"sha256={sig}"}
    with pytest.raises(SignatureVerificationError, match="INVALID_SIGNATURE"):
        verifier.verify(payload, prefixed_headers, now_ms=now_ms)

    # Clock skew past 60s
    skewed_past_headers = {**headers, "Linear-Timestamp": str(now_ms - 61_000)}
    with pytest.raises(TimestampSkewError, match="TIMESTAMP_SKEW"):
        verifier.verify(payload, skewed_past_headers, now_ms=now_ms)

    # Clock skew future > 60s
    skewed_future_headers = {**headers, "Linear-Timestamp": str(now_ms + 61_000)}
    with pytest.raises(TimestampSkewError, match="TIMESTAMP_SKEW"):
        verifier.verify(payload, skewed_future_headers, now_ms=now_ms)


# =====================================================================
# Unit Tests: Normalization and Defensive Sanitization
# =====================================================================


def test_normalizer_issue_actions():
    from herdr.intake.connectors.linear.normalizer import LinearNormalizer, OrganizationMismatchError

    normalizer = LinearNormalizer(organization_id=ORG_ID)

    # 1. Active Issue create -> work
    payload_create = sample_issue_payload("create", "unstarted")
    fact = normalizer.normalize(payload_create)
    assert fact["action"] == "work"
    assert fact["object_type"] == "issue"
    assert fact["external_id"] == "issue-uuid-001"
    assert fact["title"] == "[TEST-101] Fix memory leak in engine"
    assert fact["description"] == "Engine exhausts memory after 100 iterations."
    assert fact["identities"] == {"assignee": "Alice Smith"}

    # 2. Active Issue update -> update
    payload_update = sample_issue_payload("update", "started")
    fact_up = normalizer.normalize(payload_update)
    assert fact_up["action"] == "update"

    # 3. Closed Issue update (completed) -> closed
    payload_completed = sample_issue_payload("update", "completed")
    fact_comp = normalizer.normalize(payload_completed)
    assert fact_comp["action"] == "closed"

    # 4. Canceled Issue update -> closed
    payload_canceled = sample_issue_payload("update", "canceled")
    fact_canc = normalizer.normalize(payload_canceled)
    assert fact_canc["action"] == "closed"

    # 5. Issue remove action -> closed
    payload_remove = sample_issue_payload("remove", "unstarted")
    fact_rem = normalizer.normalize(payload_remove)
    assert fact_rem["action"] == "closed"

    # 6. Issue create with already completed state -> closed
    payload_create_closed = sample_issue_payload("create", "completed")
    fact_cc = normalizer.normalize(payload_create_closed)
    assert fact_cc["action"] == "closed"

    # 7. Organization mismatch raises error
    payload_wrong_org = sample_issue_payload("create", "unstarted", org_id="wrong-org-uuid")
    with pytest.raises(OrganizationMismatchError, match="ORGANIZATION_MISMATCH"):
        normalizer.normalize(payload_wrong_org)


def test_normalizer_defensive_sanitization():
    from herdr.intake.connectors.linear.normalizer import LinearNormalizer

    normalizer = LinearNormalizer(organization_id=ORG_ID)

    # 1. Null / empty description fallback
    payload_empty_desc = sample_issue_payload("create", "unstarted", description=None)
    fact = normalizer.normalize(payload_empty_desc)
    assert fact["description"]  # must not be empty!
    assert "TEST-101" in fact["description"]
    assert "No description provided" in fact["description"]

    # Whitespace-only description fallback
    payload_ws_desc = sample_issue_payload("create", "unstarted", description="   \n\t  ")
    fact_ws = normalizer.normalize(payload_ws_desc)
    assert fact_ws["description"]
    assert "No description provided" in fact_ws["description"]

    # 2. Ultra long title truncation (> 240 bytes)
    long_title = "A" * 300
    payload_long_title = sample_issue_payload("create", "unstarted", title=long_title)
    fact_lt = normalizer.normalize(payload_long_title)
    assert len(fact_lt["title"].encode("utf-8")) <= 240
    assert fact_lt["title"].endswith("...")
    # Original title preserved in description
    assert long_title in fact_lt["description"]

    # 3. Ultra long description truncation (> 8000 bytes)
    long_desc = "Line of text\n" * 800
    assert len(long_desc.encode("utf-8")) > 8000
    payload_long_desc = sample_issue_payload("create", "unstarted", description=long_desc)
    fact_ld = normalizer.normalize(payload_long_desc)
    assert len(fact_ld["description"].encode("utf-8")) <= 8000
    assert "truncated by Linear Connector" in fact_ld["description"]

    # 4. Strip illegal ASCII control characters (< 32, except \n, \r, \t)
    payload_ctrl = sample_issue_payload(
        "create", "unstarted",
        title="Title with \x00\x01\x02 forbidden chars",
        description="Desc with \x07 bell and \x1b escape but keeping \t and \n.",
    )
    fact_ctrl = normalizer.normalize(payload_ctrl)
    assert "\x00" not in fact_ctrl["title"]
    assert "\x01" not in fact_ctrl["title"]
    assert "\x07" not in fact_ctrl["description"]
    assert "\x1b" not in fact_ctrl["description"]
    assert "\t" in fact_ctrl["description"]
    assert "\n" in fact_ctrl["description"]


# =====================================================================
# Unit Tests: GraphQL Client and Health Checks
# =====================================================================


def test_linear_client_test_connection_and_health():
    from herdr.intake.connectors.linear.client import LinearClient

    client = LinearClient(api_token="lin_api_test_token")

    # Mock success GraphQL response
    mock_success = {
        "data": {
            "viewer": {"id": "user-1", "name": "Test User", "email": "test@example.com", "active": True},
            "organization": {"id": ORG_ID, "name": "Acme Org", "urlKey": "acme"},
            "teams": {"nodes": [{"id": "t-1", "key": "TEST", "name": "Testing Team"}]},
        }
    }

    with patch.object(client, "_post_graphql", return_value=mock_success):
        report = client.test_connection(expected_org_id=ORG_ID, expected_team_keys=["TEST"])
        assert report.ok is True
        assert report.organization_name == "Acme Org"

        health = client.health_check(expected_org_id=ORG_ID)
        assert health.status == "OK"

    # Mock org mismatch
    with patch.object(client, "_post_graphql", return_value=mock_success):
        report_mismatch = client.test_connection(expected_org_id="different-org-id")
        assert report_mismatch.ok is False
        assert "ORGANIZATION_MISMATCH" in report_mismatch.error

    # Mock auth failure
    mock_auth_error = {"errors": [{"message": "Authentication failed", "extensions": {"code": "UNAUTHENTICATED"}}]}
    with patch.object(client, "_post_graphql", return_value=mock_auth_error):
        report_auth = client.test_connection()
        assert report_auth.ok is False
        assert "UNAUTHENTICATED" in report_auth.error


# =====================================================================
# Integration Tests: Linear Connector -> EventStore -> Task Materializer -> Inbox
# =====================================================================


@pytest.fixture
def intake_env(tmp_path):
    path = tmp_path / "state.db"
    initialize(path)
    store = EventStore(
        path, SCOPE,
        authorizer=lambda action, scope: "operator",
        verifier=lambda d: d.verified_via == "SIGNATURE",
    )
    store.register_connection(
        provider="linear",
        instance=ORG_ID,
        display_name="Linear Production",
        connector_version="1.0.0",
        auth_ref="test-secret-ref",
    )
    return path, store


def test_end_to_end_linear_issue_lifecycle(intake_env):
    path, store = intake_env
    from herdr.intake.connectors.linear.connector import LinearConnector

    connector = LinearConnector(
        scope=SCOPE,
        organization_id=ORG_ID,
        signing_secret=SECRET,
    )
    policy = {
        "user_id": "operator",
        "auto_assign": True,
        "project_id": None,
        "workspace_id": None,
    }
    inbox_config = {
        "tenant_id": SCOPE.tenant_id,
        "user_id": "operator",
        "connections": {
            SCOPE.connection_id: {"auto_assign": True},
        },
    }

    # -------------------------------------------------------------
    # 1. Issue Create Webhook -> Task Materialized in Inbox as DRAFT
    # -------------------------------------------------------------
    raw_create = json.dumps(sample_issue_payload("create", "unstarted", updated_at="2026-11-19T08:00:00.000Z")).encode("utf-8")
    headers_create = {
        "Linear-Signature": sign(raw_create),
        "Linear-Delivery": "delivery-create-001",
        "Linear-Timestamp": "1780747500000",
        "Linear-Event": "Issue",
    }

    delivery_create = connector.process_webhook(raw_create, headers_create, now_ms=1780747500000)
    receipt_create = store.accept_event(delivery_create)
    assert receipt_create.outcome == "ACCEPTED"

    # Consume with worker
    consumed = worker.run_once(store, materialization=policy)
    assert consumed is True

    # Verify Inbox has exactly 1 DRAFT task
    tasks, count = InboxStore(path).list_tasks(user_id="operator")
    assert count == 1
    task = tasks[0]
    assert task["status"] == "DRAFT"
    assert task["title"] == "[TEST-101] Fix memory leak in engine"
    assert "Engine exhausts memory" in task["instruction"]

    # -------------------------------------------------------------
    # 2. Duplicate Webhook Resent -> Handled Idempotently
    # -------------------------------------------------------------
    receipt_dup = store.accept_event(delivery_create)
    assert receipt_dup.outcome == "DUPLICATE"
    assert receipt_dup.event_id == receipt_create.event_id

    # Running worker consumes nothing
    assert worker.run_once(store, materialization=policy) is False
    _, count_after_dup = InboxStore(path).list_tasks(user_id="operator")
    assert count_after_dup == 1

    # -------------------------------------------------------------
    # 3. Issue Update Webhook -> Source Snapshot Updated, No Duplicate Task
    # -------------------------------------------------------------
    raw_update = json.dumps(sample_issue_payload(
        "update", "started",
        title="Fix memory leak in buffer pool",
        description="Updated details: buffer pool leaks on restart.",
        updated_at="2026-11-19T08:05:00.000Z",
    )).encode("utf-8")
    headers_update = {
        "Linear-Signature": sign(raw_update),
        "Linear-Delivery": "delivery-update-002",
        "Linear-Timestamp": "1780747800000",
        "Linear-Event": "Issue",
    }

    delivery_update = connector.process_webhook(raw_update, headers_update, now_ms=1780747800000)
    receipt_update = store.accept_event(delivery_update)
    assert receipt_update.outcome == "ACCEPTED"
    assert receipt_update.event_id != receipt_create.event_id

    # Consume update
    assert worker.run_once(store, materialization=policy) is True

    # Inbox still has only 1 task
    _, count_after_update = InboxStore(path).list_tasks(user_id="operator")
    assert count_after_update == 1

    # Verify source snapshot updated in intake_objects via IntakeInbox
    inbox = IntakeInbox(path, inbox_config)
    items = inbox.list_items()
    assert len(items) == 1
    assert items[0]["source_title"] == "[TEST-101] Fix memory leak in buffer pool"
    assert items[0]["source_description"] == "Updated details: buffer pool leaks on restart."

    # -------------------------------------------------------------
    # 4. Issue Closed Webhook -> Task Projected as SOURCE_CLOSED
    # -------------------------------------------------------------
    raw_close = json.dumps(sample_issue_payload(
        "update", "completed",
        updated_at="2026-11-19T08:10:00.000Z",
    )).encode("utf-8")
    headers_close = {
        "Linear-Signature": sign(raw_close),
        "Linear-Delivery": "delivery-close-003",
        "Linear-Timestamp": "1780748100000",
        "Linear-Event": "Issue",
    }

    delivery_close = connector.process_webhook(raw_close, headers_close, now_ms=1780748100000)
    receipt_close = store.accept_event(delivery_close)
    assert receipt_close.outcome == "ACCEPTED"

    # Consume close
    assert worker.run_once(store, materialization=policy) is True

    # Verify Inbox status is SOURCE_CLOSED
    closed_items = inbox.list_items()
    assert len(closed_items) == 1
    assert closed_items[0]["status"] == "SOURCE_CLOSED"

    # Confirming a closed item is blocked by state machine
    with pytest.raises(Exception, match="external work item is closed"):
        inbox.change(closed_items[0]["link_id"], "confirm", closed_items[0]["revision"])

    # -------------------------------------------------------------
    # 5. Empty Description Webhook -> Successfully Materializes
    # -------------------------------------------------------------
    raw_empty = json.dumps(sample_issue_payload(
        "create", "unstarted",
        issue_id="issue-uuid-empty-desc",
        identifier="TEST-102",
        title="Issue with empty description",
        description=None,
        updated_at="2026-11-19T08:15:00.000Z",
    )).encode("utf-8")
    headers_empty = {
        "Linear-Signature": sign(raw_empty),
        "Linear-Delivery": "delivery-empty-004",
        "Linear-Timestamp": "1780748400000",
        "Linear-Event": "Issue",
    }
    delivery_empty = connector.process_webhook(raw_empty, headers_empty, now_ms=1780748400000)
    receipt_empty = store.accept_event(delivery_empty)
    assert receipt_empty.outcome == "ACCEPTED"
    assert worker.run_once(store, materialization=policy) is True

    tasks_after_empty, count_empty = InboxStore(path).list_tasks(user_id="operator")
    assert count_empty == 2
    empty_task = [t for t in tasks_after_empty if "TEST-102" in t["title"]][0]
    assert empty_task["status"] == "DRAFT"
    assert "No description provided" in empty_task["instruction"]


# =====================================================================
# Unit Tests: Receiver HTTP Handler Function
# =====================================================================


def test_receiver_http_handler_dispatch(intake_env):
    path, store = intake_env
    from herdr.intake.connectors.linear.receiver import handle_linear_webhook

    raw_body = json.dumps(sample_issue_payload("create", "unstarted")).encode("utf-8")
    headers = {
        "Linear-Signature": sign(raw_body),
        "Linear-Delivery": "delivery-http-001",
        "Linear-Timestamp": "1780747500000",
        "Linear-Event": "Issue",
    }

    # Success POST
    status, resp_headers, body = handle_linear_webhook(
        raw_body=raw_body,
        headers=headers,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=1780747500000,
    )
    assert status == 200
    assert body.get("received") is True
    assert body.get("outcome") == "ACCEPTED"

    # Duplicate POST
    status_dup, _, body_dup = handle_linear_webhook(
        raw_body=raw_body,
        headers=headers,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=1780747500000,
    )
    assert status_dup == 200
    assert body_dup.get("received") is True
    assert body_dup.get("outcome") == "DUPLICATE"

    # Bad signature POST -> 401
    bad_headers = {**headers, "Linear-Signature": "bad-sig"}
    status_bad, _, body_bad = handle_linear_webhook(
        raw_body=raw_body,
        headers=bad_headers,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=1780747500000,
    )
    assert status_bad == 401
    assert "error" in body_bad

    # Non-issue event -> 200 IGNORED
    comment_headers = {**headers, "Linear-Event": "Comment"}
    status_comm, _, body_comm = handle_linear_webhook(
        raw_body=raw_body,
        headers=comment_headers,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=1780747500000,
    )
    assert status_comm == 200
    assert body_comm.get("ignored") is True


def test_console_api_linear_webhook_endpoint(intake_env, monkeypatch):
    path, store = intake_env
    from console.inbox_api import api_linear_webhook

    # 1. Unconfigured -> 404
    monkeypatch.delenv("LINEAR_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)
    status, res = api_linear_webhook({}, b"{}")
    assert status == 404
    assert res.get("error") == "linear_webhook_not_configured"

    # 2. Configured -> Success
    now_ms = int(time.time() * 1000)
    raw_body = json.dumps(sample_issue_payload("create", "unstarted")).encode("utf-8")
    headers = {
        "Linear-Signature": sign(raw_body),
        "Linear-Delivery": "delivery-console-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    monkeypatch.setenv("LINEAR_ORGANIZATION_ID", ORG_ID)
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("HAFLOW_INTAKE_TENANT", SCOPE.tenant_id)
    monkeypatch.setenv("HAFLOW_LINEAR_CONNECTION", SCOPE.connection_id)
    monkeypatch.setattr("herdr.state_db.get_default_db_path", lambda: str(path))
    monkeypatch.setattr("console.inbox_api.get_inbox_store", lambda: MagicMock(db_path=str(path)))

    status_ok, res_ok = api_linear_webhook(headers, raw_body)
    assert status_ok == 200
    assert res_ok.get("received") is True
    assert res_ok.get("outcome") == "ACCEPTED"

