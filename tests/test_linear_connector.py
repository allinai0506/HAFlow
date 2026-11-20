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
    webhook_timestamp=None,
    event_type="Issue",
):
    if webhook_timestamp is None:
        webhook_timestamp = int(datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp() * 1000)
    return {
        "action": action,
        "type": event_type,
        "createdAt": created_at,
        "webhookTimestamp": webhook_timestamp,
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

    now_ms = 1780747500000
    payload = json.dumps(sample_issue_payload(webhook_timestamp=now_ms)).encode("utf-8")
    sig = sign(payload, SECRET)

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
    assert verified.payload["data"]["id"] == "issue-uuid-001"

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

    # P1-01: Replay attack with historical valid signed body + signature and updated header timestamp
    past_ms = now_ms - 300_000  # 5 minutes ago
    past_payload = json.dumps(sample_issue_payload(webhook_timestamp=past_ms)).encode("utf-8")
    past_sig = sign(past_payload, SECRET)
    # Attacker sets Linear-Timestamp header to CURRENT time (now_ms)
    spoofed_headers = {
        "Linear-Signature": past_sig,
        "Linear-Timestamp": str(now_ms),
        "Linear-Delivery": "replay-attack-001",
        "Linear-Event": "Issue",
    }
    with pytest.raises(TimestampSkewError, match="TIMESTAMP_SKEW"):
        verifier.verify(past_payload, spoofed_headers, now_ms=now_ms)

    # Attacker deletes Linear-Timestamp header entirely
    no_ts_header = {
        "Linear-Signature": past_sig,
        "Linear-Delivery": "replay-attack-002",
        "Linear-Event": "Issue",
    }
    with pytest.raises(TimestampSkewError, match="TIMESTAMP_SKEW"):
        verifier.verify(past_payload, no_ts_header, now_ms=now_ms)

    # Missing webhookTimestamp in signed payload body -> rejected
    no_body_ts_dict = sample_issue_payload(webhook_timestamp=now_ms)
    del no_body_ts_dict["webhookTimestamp"]
    no_body_ts_raw = json.dumps(no_body_ts_dict).encode("utf-8")
    with pytest.raises(TimestampSkewError, match="missing webhookTimestamp in signed payload"):
        verifier.verify(no_body_ts_raw, {"Linear-Signature": sign(no_body_ts_raw)}, now_ms=now_ms)

    # Clock skew past 60s
    skewed_past_headers = {**headers, "Linear-Timestamp": str(now_ms - 61_000)}
    with pytest.raises(TimestampSkewError, match="TIMESTAMP_SKEW"):
        verifier.verify(payload, skewed_past_headers, now_ms=now_ms)

    # Clock skew future > 60s
    skewed_future_headers = {**headers, "Linear-Timestamp": str(now_ms + 61_000)}
    with pytest.raises(TimestampSkewError, match="TIMESTAMP_SKEW"):
        verifier.verify(payload, skewed_future_headers, now_ms=now_ms)

    # P2: Event type mismatch between header and body -> rejected
    mismatch_headers = {**headers, "Linear-Event": "Project"}
    with pytest.raises(ValueError, match="EVENT_TYPE_MISMATCH"):
        verifier.verify(payload, mismatch_headers, now_ms=now_ms)


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
def intake_env(tmp_path, monkeypatch):
    from herdr.intake.connectors.linear.credentials import clear_secret_registry, register_secret
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
    register_secret("test-secret-ref", SECRET)
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET_REF", "test-secret-ref")
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SECRET)
    yield path, store
    clear_secret_registry()


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
    t1_iso = "2026-11-19T08:00:00.000Z"
    t1_ms = int(datetime.fromisoformat(t1_iso.replace("Z", "+00:00")).timestamp() * 1000)
    raw_create = json.dumps(sample_issue_payload("create", "unstarted", updated_at=t1_iso, created_at=t1_iso, webhook_timestamp=t1_ms)).encode("utf-8")
    headers_create = {
        "Linear-Signature": sign(raw_create),
        "Linear-Delivery": "delivery-create-001",
        "Linear-Timestamp": str(t1_ms),
        "Linear-Event": "Issue",
    }

    delivery_create = connector.process_webhook(raw_create, headers_create, now_ms=t1_ms)
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
    t2_iso = "2026-11-19T08:05:00.000Z"
    t2_ms = int(datetime.fromisoformat(t2_iso.replace("Z", "+00:00")).timestamp() * 1000)
    raw_update = json.dumps(sample_issue_payload(
        "update", "started",
        title="Fix memory leak in buffer pool",
        description="Updated details: buffer pool leaks on restart.",
        updated_at=t2_iso,
        created_at=t2_iso,
        webhook_timestamp=t2_ms,
    )).encode("utf-8")
    headers_update = {
        "Linear-Signature": sign(raw_update),
        "Linear-Delivery": "delivery-update-002",
        "Linear-Timestamp": str(t2_ms),
        "Linear-Event": "Issue",
    }

    delivery_update = connector.process_webhook(raw_update, headers_update, now_ms=t2_ms)
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
    t3_iso = "2026-11-19T08:10:00.000Z"
    t3_ms = int(datetime.fromisoformat(t3_iso.replace("Z", "+00:00")).timestamp() * 1000)
    raw_close = json.dumps(sample_issue_payload(
        "update", "completed",
        updated_at=t3_iso,
        created_at=t3_iso,
        webhook_timestamp=t3_ms,
    )).encode("utf-8")
    headers_close = {
        "Linear-Signature": sign(raw_close),
        "Linear-Delivery": "delivery-close-003",
        "Linear-Timestamp": str(t3_ms),
        "Linear-Event": "Issue",
    }

    delivery_close = connector.process_webhook(raw_close, headers_close, now_ms=t3_ms)
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
    t4_iso = "2026-11-19T08:15:00.000Z"
    t4_ms = int(datetime.fromisoformat(t4_iso.replace("Z", "+00:00")).timestamp() * 1000)
    raw_empty = json.dumps(sample_issue_payload(
        "create", "unstarted",
        issue_id="issue-uuid-empty-desc",
        identifier="TEST-102",
        title="Issue with empty description",
        description=None,
        updated_at=t4_iso,
        created_at=t4_iso,
        webhook_timestamp=t4_ms,
    )).encode("utf-8")
    headers_empty = {
        "Linear-Signature": sign(raw_empty),
        "Linear-Delivery": "delivery-empty-004",
        "Linear-Timestamp": str(t4_ms),
        "Linear-Event": "Issue",
    }
    delivery_empty = connector.process_webhook(raw_empty, headers_empty, now_ms=t4_ms)
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

    t_ms = 1780747500000
    raw_body = json.dumps(sample_issue_payload("create", "unstarted", webhook_timestamp=t_ms)).encode("utf-8")
    headers = {
        "Linear-Signature": sign(raw_body),
        "Linear-Delivery": "delivery-http-001",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }

    # Success POST
    status, resp_headers, body = handle_linear_webhook(
        raw_body=raw_body,
        headers=headers,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=t_ms,
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
        now_ms=t_ms,
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
        now_ms=t_ms,
    )
    assert status_bad == 401
    assert "error" in body_bad

    # P2: Non-issue event (Project) -> 200 IGNORED, no task created
    raw_project = json.dumps(sample_issue_payload("create", "unstarted", webhook_timestamp=t_ms, event_type="Project")).encode("utf-8")
    project_headers = {
        "Linear-Signature": sign(raw_project),
        "Linear-Delivery": "delivery-proj-001",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Project",
    }
    status_proj, _, body_proj = handle_linear_webhook(
        raw_body=raw_project,
        headers=project_headers,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=t_ms,
    )
    assert status_proj == 200
    assert body_proj.get("ignored") is True
    assert "UNSUPPORTED_EVENT_TYPE" in body_proj.get("reason", "")

    # Non-issue event (Project) WITHOUT Linear-Event header -> parsed from body type and safely ignored
    project_no_hdr = {
        "Linear-Signature": sign(raw_project),
        "Linear-Delivery": "delivery-proj-002",
        "Linear-Timestamp": str(t_ms),
    }
    status_proj2, _, body_proj2 = handle_linear_webhook(
        raw_body=raw_project,
        headers=project_no_hdr,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=t_ms,
    )
    assert status_proj2 == 200
    assert body_proj2.get("ignored") is True

    # Non-issue event (Comment) -> 200 IGNORED
    raw_comment = json.dumps(sample_issue_payload("create", "unstarted", webhook_timestamp=t_ms, event_type="Comment")).encode("utf-8")
    comment_headers = {
        "Linear-Signature": sign(raw_comment),
        "Linear-Delivery": "delivery-comm-001",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Comment",
    }
    status_comm, _, body_comm = handle_linear_webhook(
        raw_body=raw_comment,
        headers=comment_headers,
        store=store,
        organization_id=ORG_ID,
        signing_secret=SECRET,
        now_ms=t_ms,
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
    raw_body = json.dumps(sample_issue_payload("create", "unstarted", webhook_timestamp=now_ms)).encode("utf-8")
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


def test_codex_p2_remove_with_same_updated_at_does_not_collide(intake_env):
    """Codex P2 regression: create followed by remove using same data.updatedAt and later payload.createdAt."""
    path, store = intake_env
    from herdr.intake.connectors.linear.connector import LinearConnector

    connector = LinearConnector(
        scope=SCOPE,
        organization_id=ORG_ID,
        signing_secret=SECRET,
    )
    policy = {"user_id": "operator", "auto_assign": True, "project_id": None, "workspace_id": None}
    inbox_config = {"tenant_id": SCOPE.tenant_id, "user_id": "operator", "connections": {SCOPE.connection_id: {"auto_assign": True}}}

    # Step 1: Create issue at T1
    t1_iso = "2026-11-19T08:00:00.000Z"
    t1_ms = int(datetime.fromisoformat(t1_iso.replace("Z", "+00:00")).timestamp() * 1000)
    raw_create = json.dumps(sample_issue_payload(
        "create", "unstarted",
        issue_id="issue-uuid-codex-p2",
        updated_at=t1_iso,
        created_at=t1_iso,
        webhook_timestamp=t1_ms,
    )).encode("utf-8")
    headers_create = {
        "Linear-Signature": sign(raw_create),
        "Linear-Delivery": "delivery-codex-001",
        "Linear-Timestamp": str(t1_ms),
        "Linear-Event": "Issue",
    }
    delivery_create = connector.process_webhook(raw_create, headers_create, now_ms=t1_ms)
    receipt_create = store.accept_event(delivery_create)
    assert receipt_create.outcome == "ACCEPTED"
    assert worker.run_once(store, materialization=policy) is True

    # Step 2: Remove issue retaining SAME data.updatedAt = T1, but later payload.createdAt = T2
    t2_iso = "2026-11-19T08:10:00.000Z"
    t2_ms = int(datetime.fromisoformat(t2_iso.replace("Z", "+00:00")).timestamp() * 1000)
    raw_remove = json.dumps(sample_issue_payload(
        "remove", "unstarted",
        issue_id="issue-uuid-codex-p2",
        updated_at=t1_iso,  # Kept same as create!
        created_at=t2_iso,  # Later event creation time!
        webhook_timestamp=t2_ms,
    )).encode("utf-8")
    headers_remove = {
        "Linear-Signature": sign(raw_remove),
        "Linear-Delivery": "delivery-codex-002",
        "Linear-Timestamp": str(t2_ms),
        "Linear-Event": "Issue",
    }
    delivery_remove = connector.process_webhook(raw_remove, headers_remove, now_ms=t2_ms)
    receipt_remove = store.accept_event(delivery_remove)
    # Must be ACCEPTED, NOT rejected as FACT_IDENTITY_COLLISION!
    assert receipt_remove.outcome == "ACCEPTED"
    assert receipt_remove.event_id != receipt_create.event_id

    # Consume with worker -> successfully marks item CLOSED
    assert worker.run_once(store, materialization=policy) is True
    inbox = IntakeInbox(path, inbox_config)
    items = inbox.list_items()
    assert len(items) == 1
    assert items[0]["status"] == "SOURCE_CLOSED"


def test_codex_p1_content_length_limit_enforced_before_read(monkeypatch):
    """Codex P1 regression: body size limit is enforced before reading from stream."""
    import socket
    import threading
    from herdr.intake.connectors.linear.receiver import LinearWebhookHandler

    client_sock, server_sock = socket.socketpair()
    mock_server = MagicMock()
    mock_server.store = MagicMock()
    mock_server.organization_id = ORG_ID
    mock_server.signing_secret = SECRET

    def handle():
        LinearWebhookHandler(server_sock, ("127.0.0.1", 12345), mock_server)

    t = threading.Thread(target=handle)
    t.start()

    # Send 300,000 Content-Length with only 50 bytes of body
    client_sock.sendall(b"POST / HTTP/1.1\r\nContent-Length: 300000\r\n\r\n" + b"X" * 50)
    response_chunks = []
    while True:
        chunk = client_sock.recv(4096)
        if not chunk:
            break
        response_chunks.append(chunk)
        if b"PAYLOAD_TOO_LARGE" in b"".join(response_chunks):
            break
    response_data = b"".join(response_chunks).decode("utf-8")
    assert "413" in response_data
    assert "PAYLOAD_TOO_LARGE" in response_data

    client_sock.close()
    t.join()


def test_p1_02_authorization_revision_lifecycle_and_stale_rejection(intake_env):
    """P1-02: Connection revision 1 -> 2 (DISABLED) -> 3 (ACTIVE), verifying no silent 200 on rejection."""
    path, store = intake_env
    from herdr.intake.connectors.linear.receiver import handle_linear_webhook

    t_iso = "2026-11-19T08:00:00.000Z"
    t_ms = int(datetime.fromisoformat(t_iso.replace("Z", "+00:00")).timestamp() * 1000)

    # 1. Initial State: Connection registered at config_revision = 1, status = ACTIVE
    raw_1 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-rev-1", webhook_timestamp=t_ms)).encode("utf-8")
    headers_1 = {
        "Linear-Signature": sign(raw_1),
        "Linear-Delivery": "delivery-rev-001",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }
    status_1, _, body_1 = handle_linear_webhook(raw_1, headers_1, store, ORG_ID, SECRET, now_ms=t_ms)
    assert status_1 == 200
    assert body_1.get("outcome") == "ACCEPTED"

    # 2. Revoke authorization: update connection status to DISABLED -> config_revision = 2
    authorizer_store = EventStore(path, SCOPE, authorizer=lambda action, scope: "admin-actor")
    new_rev = authorizer_store.update_connection_authorization(1, status="DISABLED", reason="OAuth revoked by user")
    assert new_rev == 2

    # Webhook sent when revoked -> must be rejected with 403 (NOT 200!)
    raw_2 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-rev-2", webhook_timestamp=t_ms)).encode("utf-8")
    headers_2 = {
        "Linear-Signature": sign(raw_2),
        "Linear-Delivery": "delivery-rev-002",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }
    status_2, _, body_2 = handle_linear_webhook(raw_2, headers_2, store, ORG_ID, SECRET, now_ms=t_ms)
    assert status_2 == 403
    assert "CONNECTION_UNAVAILABLE" in body_2.get("error", "")

    # 3. Re-authorize connection: update connection status to ACTIVE -> config_revision = 3
    new_rev_3 = authorizer_store.update_connection_authorization(2, status="ACTIVE", reason="Re-authenticated via OAuth")
    assert new_rev_3 == 3

    # Webhook sent after re-authorization -> dynamically picks up revision 3 -> 200 ACCEPTED
    raw_3 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-rev-3", webhook_timestamp=t_ms)).encode("utf-8")
    headers_3 = {
        "Linear-Signature": sign(raw_3),
        "Linear-Delivery": "delivery-rev-003",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }
    status_3, _, body_3 = handle_linear_webhook(raw_3, headers_3, store, ORG_ID, SECRET, now_ms=t_ms)
    assert status_3 == 200
    assert body_3.get("outcome") == "ACCEPTED"

    # 4. Stale delivery test: if a delivery is explicitly stamped with stale config_revision = 1 against revision 3:
    # EventStore rejects it as CONNECTION_UNAVAILABLE, and webhook handler MUST return 409 (NOT silent 200!)
    raw_stale = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-rev-stale", webhook_timestamp=t_ms)).encode("utf-8")
    headers_stale = {
        "Linear-Signature": sign(raw_stale),
        "Linear-Delivery": "delivery-rev-stale",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }
    status_stale, _, body_stale = handle_linear_webhook(
        raw_stale, headers_stale, store, ORG_ID, SECRET, config_revision=1, now_ms=t_ms
    )
    assert status_stale == 409
    assert body_stale.get("ok") is False
    assert "CONNECTION_UNAVAILABLE" in body_stale.get("error", "")


def test_p1_03_console_slow_client_read_timeout_and_incomplete_body():
    """P1-03: Console webhook endpoint enforces read timeout and rejects incomplete body."""
    import socket
    import threading
    from console.http_server import Handler

    # Case 1: Incomplete body (Content-Length: 100, only 20 bytes received and client closes stream)
    client_sock1, server_sock1 = socket.socketpair()
    mock_server1 = MagicMock()

    def handle1():
        Handler(server_sock1, ("127.0.0.1", 12345), mock_server1)

    t1 = threading.Thread(target=handle1)
    t1.start()

    client_sock1.sendall(b"POST /api/intake/webhook/linear HTTP/1.1\r\nContent-Length: 100\r\n\r\n" + b"X" * 20)
    client_sock1.shutdown(socket.SHUT_WR)
    res1_chunks = []
    while True:
        c = client_sock1.recv(4096)
        if not c:
            break
        res1_chunks.append(c)
        if b"INCOMPLETE_BODY" in b"".join(res1_chunks):
            break
    res1 = b"".join(res1_chunks).decode("utf-8")
    assert "400" in res1
    assert "INCOMPLETE_BODY" in res1
    client_sock1.close()
    t1.join()

    # Case 2: Slow/hanging client socket read timeout
    client_sock2, server_sock2 = socket.socketpair()
    mock_server2 = MagicMock()

    class FastTimeoutSocket:
        def __init__(self, s):
            self._s = s
        def settimeout(self, _):
            self._s.settimeout(0.1)
        def makefile(self, *args, **kwargs):
            return self._s.makefile(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self._s, name)

    wrapped_sock2 = FastTimeoutSocket(server_sock2)

    def handle2():
        Handler(wrapped_sock2, ("127.0.0.1", 12345), mock_server2)

    t2 = threading.Thread(target=handle2)
    t2.start()

    # Send Content-Length: 100, but only send 20 bytes and stall without closing
    client_sock2.sendall(b"POST /api/intake/webhook/linear HTTP/1.1\r\nContent-Length: 100\r\n\r\n" + b"X" * 20)
    res2_chunks = []
    while True:
        c = client_sock2.recv(4096)
        if not c:
            break
        res2_chunks.append(c)
        if b"REQUEST_TIMEOUT" in b"".join(res2_chunks):
            break
    res2 = b"".join(res2_chunks).decode("utf-8")
    assert "408" in res2
    assert "REQUEST_TIMEOUT" in res2
    client_sock2.close()
    t2.join()


def test_p1_secret_rotation_bound_to_authorization_revision(tmp_path):
    """P1: Webhook signing secret rotation is bound to connection auth_ref and config_revision."""
    from herdr.intake.connectors.linear.credentials import clear_secret_registry, register_secret
    from herdr.intake.connectors.linear.receiver import handle_linear_webhook

    clear_secret_registry()
    path = tmp_path / "state_rot.db"
    initialize(path)
    store = EventStore(
        path, SCOPE,
        authorizer=lambda action, scope: "admin",
        verifier=lambda d: d.verified_via == "SIGNATURE",
    )

    old_secret = "old-secret-key-111"
    new_secret = "new-secret-key-222"
    ref_v1 = "linear-secret-ref-v1"
    ref_v2 = "linear-secret-ref-v2"

    register_secret(ref_v1, old_secret)

    # 1. Initial connection: revision = 1, auth_ref = ref_v1
    store.register_connection(
        provider="linear",
        instance=ORG_ID,
        display_name="Linear",
        connector_version="1.0.0",
        auth_ref=ref_v1,
    )

    t_iso = "2026-11-19T08:00:00.000Z"
    t_ms = int(datetime.fromisoformat(t_iso.replace("Z", "+00:00")).timestamp() * 1000)

    # Event signed with Old Secret at revision 1 -> ACCEPTED
    raw_1 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-rot-1", webhook_timestamp=t_ms)).encode("utf-8")
    headers_1 = {
        "Linear-Signature": sign(raw_1, old_secret),
        "Linear-Delivery": "delivery-rot-001",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }
    status_1, _, body_1 = handle_linear_webhook(raw_1, headers_1, store, ORG_ID, signing_secret=old_secret, auth_ref=ref_v1, now_ms=t_ms)
    assert status_1 == 200
    assert body_1.get("outcome") == "ACCEPTED"

    # 2. Administrator rotates secret in DB: update auth_ref to ref_v2, revision increments to 2
    new_rev = store.update_connection_authorization(1, status="ACTIVE", reason="Key rotation", auth_ref=ref_v2)
    assert new_rev == 2

    # Register the new secret in secret manager / registry
    register_secret(ref_v2, new_secret)

    # 3. Scenario A: Old service has NOT restarted and NOT refreshed credentials (still holding old_secret with ref_v1)
    # Attacker sends event using old_secret to the un-refreshed service -> FAIL CLOSED (401 AUTH_REF_MISMATCH)
    raw_2_att = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-rot-2-att", webhook_timestamp=t_ms)).encode("utf-8")
    headers_2_att = {
        "Linear-Signature": sign(raw_2_att, old_secret),
        "Linear-Delivery": "delivery-rot-002-att",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }
    status_unrefreshed, _, body_unrefreshed = handle_linear_webhook(
        raw_2_att, headers_2_att, store, ORG_ID,
        signing_secret=old_secret, auth_ref=ref_v1, now_ms=t_ms,
    )
    assert status_unrefreshed == 401
    assert "AUTH_REF_MISMATCH" in body_unrefreshed.get("error", "")

    # 4. Scenario B: Service resolves credentials dynamically from registry/env for ref_v2
    # Attacker sends event signed with Old Secret to the service -> HMAC signature failure against new_secret (401 INVALID_SIGNATURE)
    status_old_sig, _, body_old_sig = handle_linear_webhook(
        raw_2_att, headers_2_att, store, ORG_ID,
        signing_secret=None, auth_ref=None, now_ms=t_ms,
    )
    assert status_old_sig == 401
    assert "INVALID_SIGNATURE" in body_old_sig.get("error", "")

    # 5. Legitimate Linear event signed with New Secret sent to the updated/resolving service -> ACCEPTED at revision 2
    raw_2_legit = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-rot-2-legit", webhook_timestamp=t_ms)).encode("utf-8")
    headers_2_legit = {
        "Linear-Signature": sign(raw_2_legit, new_secret),
        "Linear-Delivery": "delivery-rot-002-legit",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }
    status_new, _, body_new = handle_linear_webhook(
        raw_2_legit, headers_2_legit, store, ORG_ID,
        signing_secret=new_secret, auth_ref=ref_v2, now_ms=t_ms,
    )
    assert status_new == 200
    assert body_new.get("outcome") == "ACCEPTED"

    clear_secret_registry()


def _send_http_request(handler_cls, server_obj, path, headers, raw_body):
    """Helper to send an HTTP POST request over socketpair to a BaseHTTPRequestHandler."""
    import socket
    import threading

    client_sock, server_sock = socket.socketpair()

    def handle():
        try:
            handler_cls(server_sock, ("127.0.0.1", 12345), server_obj)
        finally:
            try:
                server_sock.close()
            except Exception:
                pass

    t = threading.Thread(target=handle)
    t.start()

    all_headers = {
        "Host": "127.0.0.1",
        "Content-Length": str(len(raw_body)),
        "Connection": "close",
        **headers,
    }
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in all_headers.items())
    req_bytes = f"POST {path} HTTP/1.1\r\n{header_lines}\r\n".encode("utf-8") + raw_body
    client_sock.sendall(req_bytes)

    chunks = []
    while True:
        c = client_sock.recv(4096)
        if not c:
            break
        chunks.append(c)
    client_sock.close()
    t.join()

    resp_data = b"".join(chunks).decode("utf-8")
    status_line = resp_data.split("\r\n", 1)[0]
    status_code = int(status_line.split()[1])
    parts = resp_data.split("\r\n\r\n", 1)
    body = json.loads(parts[1]) if len(parts) > 1 and parts[1].strip() else {}
    return status_code, body


def test_console_http_handler_secret_rotation(tmp_path, monkeypatch):
    """Integration: Console HTTP Handler dynamically resolves signing secret via auth_ref without global secret."""
    from console.http_server import Handler
    from herdr.intake.connectors.linear.credentials import clear_secret_registry

    clear_secret_registry()
    path = tmp_path / "state_console_rot.db"
    initialize(path)
    store = EventStore(
        path,
        SCOPE,
        authorizer=lambda action, scope: "admin",
        verifier=lambda d: d.verified_via == "SIGNATURE",
    )

    old_secret = "console-old-secret-111"
    new_secret = "console-new-secret-222"
    ref_v1 = "linear-ref-v1"
    ref_v2 = "linear-ref-v2"

    # 1. Connection registered at revision 1 with auth_ref = ref_v1
    store.register_connection(
        provider="linear",
        instance=ORG_ID,
        display_name="Linear",
        connector_version="1.0.0",
        auth_ref=ref_v1,
    )

    # Ingress environment: LINEAR_WEBHOOK_SECRET is NOT set globally, credentials resolved per auth_ref
    monkeypatch.setenv("LINEAR_ORGANIZATION_ID", ORG_ID)
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET_REF", raising=False)
    monkeypatch.setenv("HAFLOW_INTAKE_TENANT", SCOPE.tenant_id)
    monkeypatch.setenv("HAFLOW_LINEAR_CONNECTION", SCOPE.connection_id)
    monkeypatch.setattr("herdr.state_db.get_default_db_path", lambda: str(path))
    monkeypatch.setattr("console.inbox_api.get_inbox_store", lambda: MagicMock(db_path=str(path)))

    # Set secret for ref_v1 in environment
    monkeypatch.setenv("HAFLOW_INTAKE_SECRET_LINEAR_REF_V1", old_secret)

    mock_server = MagicMock()
    now_ms = int(time.time() * 1000)

    # Request 1: Signed with old_secret -> Console Handler returns 200 ACCEPTED
    raw_1 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="console-rot-1", webhook_timestamp=now_ms)).encode("utf-8")
    headers_1 = {
        "Linear-Signature": sign(raw_1, old_secret),
        "Linear-Delivery": "delivery-console-rot-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_1, body_1 = _send_http_request(Handler, mock_server, "/api/intake/webhook/linear", headers_1, raw_1)
    assert status_1 == 200
    assert body_1.get("ok") is True
    assert body_1["data"]["outcome"] == "ACCEPTED"

    # 2. Administrator rotates connection authorization to ref_v2 in DB -> revision = 2
    new_rev = store.update_connection_authorization(1, status="ACTIVE", reason="Key rotation", auth_ref=ref_v2)
    assert new_rev == 2
    monkeypatch.setenv("HAFLOW_INTAKE_SECRET_LINEAR_REF_V2", new_secret)

    # Request 2 (Attacker): Signed with old_secret -> 401 INVALID_SIGNATURE (evaluated against dynamically resolved new_secret)
    raw_2_att = json.dumps(sample_issue_payload("create", "unstarted", issue_id="console-rot-2-att", webhook_timestamp=now_ms)).encode("utf-8")
    headers_2_att = {
        "Linear-Signature": sign(raw_2_att, old_secret),
        "Linear-Delivery": "delivery-console-rot-002-att",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_2_att, body_2_att = _send_http_request(Handler, mock_server, "/api/intake/webhook/linear", headers_2_att, raw_2_att)
    assert status_2_att == 401
    assert body_2_att.get("ok") is False
    assert "INVALID_SIGNATURE" in body_2_att.get("error", "")

    # Request 3 (Legitimate): Signed with new_secret -> 200 ACCEPTED at revision 2
    raw_2_legit = json.dumps(sample_issue_payload("create", "unstarted", issue_id="console-rot-2-legit", webhook_timestamp=now_ms)).encode("utf-8")
    headers_2_legit = {
        "Linear-Signature": sign(raw_2_legit, new_secret),
        "Linear-Delivery": "delivery-console-rot-002-legit",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_2_legit, body_2_legit = _send_http_request(Handler, mock_server, "/api/intake/webhook/linear", headers_2_legit, raw_2_legit)
    assert status_2_legit == 200
    assert body_2_legit.get("ok") is True
    assert body_2_legit["data"]["outcome"] == "ACCEPTED"

    # 3. Connection rotated in DB to unconfigured ref_v3 -> 401 CREDENTIAL_UNRESOLVED (Fail Closed)
    store.update_connection_authorization(2, status="ACTIVE", reason="Key rotation to unconfigured v3", auth_ref="linear-ref-v3")
    raw_3 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="console-rot-3", webhook_timestamp=now_ms)).encode("utf-8")
    headers_3 = {
        "Linear-Signature": sign(raw_3, new_secret),
        "Linear-Delivery": "delivery-console-rot-003",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_3, body_3 = _send_http_request(Handler, mock_server, "/api/intake/webhook/linear", headers_3, raw_3)
    assert status_3 == 401
    assert body_3.get("ok") is False
    assert "CREDENTIAL_UNRESOLVED" in body_3.get("error", "")


def test_standalone_webhook_handler_secret_rotation(tmp_path):
    """Integration: Standalone Webhook Handler dynamically resolves secrets and enforces rotation Fail-Closed."""
    from herdr.intake.connectors.linear.credentials import clear_secret_registry, register_secret
    from herdr.intake.connectors.linear.receiver import LinearWebhookHandler

    clear_secret_registry()
    path = tmp_path / "state_standalone_rot.db"
    initialize(path)
    store = EventStore(
        path,
        SCOPE,
        authorizer=lambda action, scope: "admin",
        verifier=lambda d: d.verified_via == "SIGNATURE",
    )

    old_secret = "standalone-old-111"
    new_secret = "standalone-new-222"
    ref_v1 = "linear-ref-v1"
    ref_v2 = "linear-ref-v2"

    store.register_connection(
        provider="linear",
        instance=ORG_ID,
        display_name="Linear",
        connector_version="1.0.0",
        auth_ref=ref_v1,
    )
    register_secret(ref_v1, old_secret)

    # Dynamic server mock: signing_secret=None, auth_ref=None (fully dynamic resolution)
    dynamic_server = MagicMock()
    dynamic_server.store = store
    dynamic_server.organization_id = ORG_ID
    dynamic_server.signing_secret = None
    dynamic_server.auth_ref = None

    now_ms = int(time.time() * 1000)

    # 1. Request signed with old_secret -> 200 ACCEPTED
    raw_1 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="standalone-rot-1", webhook_timestamp=now_ms)).encode("utf-8")
    headers_1 = {
        "Linear-Signature": sign(raw_1, old_secret),
        "Linear-Delivery": "delivery-standalone-rot-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_1, body_1 = _send_http_request(LinearWebhookHandler, dynamic_server, "/", headers_1, raw_1)
    assert status_1 == 200
    assert body_1.get("received") is True
    assert body_1.get("outcome") == "ACCEPTED"

    # 2. Administrator rotates key in DB to ref_v2
    new_rev = store.update_connection_authorization(1, status="ACTIVE", reason="Key rotation", auth_ref=ref_v2)
    assert new_rev == 2
    register_secret(ref_v2, new_secret)

    # Request signed with old_secret sent to dynamic server -> 401 INVALID_SIGNATURE
    raw_2_att = json.dumps(sample_issue_payload("create", "unstarted", issue_id="standalone-rot-2-att", webhook_timestamp=now_ms)).encode("utf-8")
    headers_2_att = {
        "Linear-Signature": sign(raw_2_att, old_secret),
        "Linear-Delivery": "delivery-standalone-rot-002-att",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_2_att, body_2_att = _send_http_request(LinearWebhookHandler, dynamic_server, "/", headers_2_att, raw_2_att)
    assert status_2_att == 401
    assert "INVALID_SIGNATURE" in body_2_att.get("error", "")

    # Request signed with new_secret sent to dynamic server -> 200 ACCEPTED
    raw_2_legit = json.dumps(sample_issue_payload("create", "unstarted", issue_id="standalone-rot-2-legit", webhook_timestamp=now_ms)).encode("utf-8")
    headers_2_legit = {
        "Linear-Signature": sign(raw_2_legit, new_secret),
        "Linear-Delivery": "delivery-standalone-rot-002-legit",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_2_legit, body_2_legit = _send_http_request(LinearWebhookHandler, dynamic_server, "/", headers_2_legit, raw_2_legit)
    assert status_2_legit == 200
    assert body_2_legit.get("outcome") == "ACCEPTED"

    # 3. Stale daemon instance scenario:
    # Server started with static ref_v1 credentials, DB is at ref_v2 -> FAIL CLOSED (401 AUTH_REF_MISMATCH)
    stale_server = MagicMock()
    stale_server.store = store
    stale_server.organization_id = ORG_ID
    stale_server.signing_secret = old_secret
    stale_server.auth_ref = ref_v1

    status_stale, body_stale = _send_http_request(LinearWebhookHandler, stale_server, "/", headers_2_att, raw_2_att)
    assert status_stale == 401
    assert "AUTH_REF_MISMATCH" in body_stale.get("error", "")

    # 4. Unconfigured secret scenario: rotate in DB to ref_v3 without registering secret -> 401 CREDENTIAL_UNRESOLVED
    store.update_connection_authorization(2, status="ACTIVE", reason="Rotate to v3", auth_ref="linear-ref-v3")
    status_unresolved, body_unresolved = _send_http_request(LinearWebhookHandler, dynamic_server, "/", headers_2_legit, raw_2_legit)
    assert status_unresolved == 401
    assert "CREDENTIAL_UNRESOLVED" in body_unresolved.get("error", "")

    clear_secret_registry()


def test_standalone_cli_parser_allows_omitted_secret(monkeypatch):
    """Verify that standalone CLI allows omitting --secret and defaults to dynamic resolution."""
    import importlib.util

    script_path = Path(__file__).resolve().parents[1] / "services" / "herdr-linear-webhook.py"
    spec = importlib.util.spec_from_file_location("herdr_linear_webhook", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    test_args = [
        "herdr-linear-webhook.py",
        "--tenant", "test-tenant",
        "--connection", "test-conn",
        "--org-id", ORG_ID,
    ]
    monkeypatch.setattr("sys.argv", test_args)
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET_REF", raising=False)

    with patch.object(mod, "LinearWebhookServer") as mock_server, \
         patch.object(mod, "initialize") as mock_init, \
         patch.object(mod, "EventStore") as mock_store, \
         patch.object(mod.signal, "signal"):
        mock_server.return_value.serve_forever.side_effect = SystemExit(0)
        with pytest.raises(SystemExit):
            mod.main()
        mock_server.assert_called_once()
        _, kwargs = mock_server.call_args
        assert kwargs.get("signing_secret") is None
        assert kwargs.get("auth_ref") is None
        assert kwargs.get("organization_id") == ORG_ID


def test_manifest_rejects_none_and_default_auth_ref():
    """Manifest: validate_config rejects 'none' and 'default' for webhook_signing_secret_ref."""
    from herdr.intake.connectors.linear.manifest import validate_config

    for bad_ref in ("none", "default", "NONE", "Default"):
        cfg = {
            "organization_id": ORG_ID,
            "webhook_signing_secret_ref": bad_ref,
        }
        report = validate_config(cfg)
        assert report.valid is False
        assert any("cannot be 'none' or 'default'" in e for e in report.errors)

    # Valid non-default token succeeds
    valid_cfg = {
        "organization_id": ORG_ID,
        "webhook_signing_secret_ref": "linear-webhook-prod-v1",
    }
    report_valid = validate_config(valid_cfg)
    assert report_valid.valid is True


def test_register_linear_connection_rejects_none_and_default(tmp_path):
    """Registration: register_linear_connection rejects 'none' and 'default'."""
    from herdr.intake.connectors.linear.connector import LinearConnector, register_linear_connection

    path = tmp_path / "state_reg.db"
    initialize(path)
    store = EventStore(path, SCOPE, authorizer=lambda a, s: "admin")

    for bad_ref in ("none", "default", "NONE", "Default"):
        with pytest.raises(ValueError, match="DISALLOWED_DEFAULT_AUTH_REF"):
            register_linear_connection(store, instance=ORG_ID, auth_ref=bad_ref)

        with pytest.raises(ValueError, match="DISALLOWED_DEFAULT_AUTH_REF"):
            LinearConnector.register_connection(store, instance=ORG_ID, auth_ref=bad_ref)

    with pytest.raises(ValueError, match="auth_ref is required"):
        register_linear_connection(store, instance=ORG_ID, auth_ref="")

    # Valid explicit reference succeeds
    register_linear_connection(store, instance=ORG_ID, auth_ref="linear-prod-ref-v1")


def test_credentials_resolver_rejects_none_and_default():
    """Credentials: resolve_signing_secret rejects 'none' and 'default' even with global secret."""
    from herdr.intake.connectors.linear.credentials import SecretResolutionError, resolve_signing_secret

    for bad_ref in ("none", "default", "NONE", "Default"):
        # Without loaded secret
        with pytest.raises(SecretResolutionError, match="DISALLOWED_DEFAULT_AUTH_REF"):
            resolve_signing_secret(bad_ref)

        # With loaded secret (proves global secret cannot bypass immutable auth_ref contract)
        with pytest.raises(SecretResolutionError, match="DISALLOWED_DEFAULT_AUTH_REF"):
            resolve_signing_secret(bad_ref, loaded_secret="some-global-secret")


def test_webhook_receiver_and_http_handlers_reject_default_auth_ref(tmp_path, monkeypatch):
    """Receiver & HTTP Handlers: strictly reject webhooks when connection has auth_ref 'none' or 'default'."""
    from console.http_server import Handler
    from herdr.intake.connectors.linear.receiver import LinearWebhookHandler, handle_linear_webhook

    path = tmp_path / "state_def_reject.db"
    initialize(path)
    store = EventStore(path, SCOPE, authorizer=lambda a, s: "admin", verifier=lambda d: d.verified_via == "SIGNATURE")

    # 1. Connection registered with default auth_ref = 'none' (e.g. legacy/unconfigured)
    store.register_connection(
        provider="linear",
        instance=ORG_ID,
        display_name="Linear",
        connector_version="1.0.0",
        auth_ref="none",
    )

    t_ms = int(time.time() * 1000)
    raw = json.dumps(sample_issue_payload("create", "unstarted", issue_id="def-1", webhook_timestamp=t_ms)).encode("utf-8")
    headers = {
        "Linear-Signature": sign(raw, SECRET),
        "Linear-Delivery": "delivery-def-001",
        "Linear-Timestamp": str(t_ms),
        "Linear-Event": "Issue",
    }

    # Case A: handle_linear_webhook directly -> 401 DISALLOWED_DEFAULT_AUTH_REF
    status, _, body = handle_linear_webhook(raw, headers, store, ORG_ID, signing_secret=SECRET, now_ms=t_ms)
    assert status == 401
    assert "DISALLOWED_DEFAULT_AUTH_REF" in body.get("error", "")

    # Case B: Console HTTP Handler -> 401 DISALLOWED_DEFAULT_AUTH_REF
    monkeypatch.setenv("LINEAR_ORGANIZATION_ID", ORG_ID)
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("HAFLOW_INTAKE_TENANT", SCOPE.tenant_id)
    monkeypatch.setenv("HAFLOW_LINEAR_CONNECTION", SCOPE.connection_id)
    monkeypatch.setattr("herdr.state_db.get_default_db_path", lambda: str(path))
    monkeypatch.setattr("console.inbox_api.get_inbox_store", lambda: MagicMock(db_path=str(path)))

    mock_server = MagicMock()
    status_console, body_console = _send_http_request(Handler, mock_server, "/api/intake/webhook/linear", headers, raw)
    assert status_console == 401
    assert "DISALLOWED_DEFAULT_AUTH_REF" in body_console.get("error", "")

    # Case C: Standalone Webhook Handler with DB auth_ref = 'default' -> 401 DISALLOWED_DEFAULT_AUTH_REF
    store.update_connection_authorization(1, status="ACTIVE", reason="Update to default", auth_ref="default")
    standalone_server = MagicMock()
    standalone_server.store = store
    standalone_server.organization_id = ORG_ID
    standalone_server.signing_secret = SECRET
    standalone_server.auth_ref = None

    status_standalone, body_standalone = _send_http_request(LinearWebhookHandler, standalone_server, "/", headers, raw)
    assert status_standalone == 401
    assert "DISALLOWED_DEFAULT_AUTH_REF" in body_standalone.get("error", "")


def test_canonical_auth_ref_prevents_environment_lookup_collision(tmp_path, monkeypatch):
    """Codex P2 regression: distinct auth_ref identities must be canonical kebab-case to prevent env lookup collision."""
    from herdr.intake.connectors.linear.connector import register_linear_connection
    from herdr.intake.connectors.linear.credentials import SecretResolutionError, resolve_signing_secret
    from herdr.intake.connectors.linear.manifest import validate_config
    from herdr.intake.connectors.linear.receiver import handle_linear_webhook

    # 1. Manifest layer rejects non-canonical names (underscores, dots, mixed case)
    for bad_name in ("linear_key", "Linear-Key", "linear.key", "linear--key"):
        rep = validate_config({"organization_id": ORG_ID, "webhook_signing_secret_ref": bad_name})
        assert rep.valid is False
        assert any("must be canonical lowercase kebab-case" in e for e in rep.errors)

    # 2. Registration layer rejects non-canonical names
    path = tmp_path / "state_canon.db"
    initialize(path)
    store = EventStore(path, SCOPE, authorizer=lambda a, s: "admin", verifier=lambda d: d.verified_via == "SIGNATURE")

    with pytest.raises(ValueError, match="NON_CANONICAL_AUTH_REF"):
        register_linear_connection(store, instance=ORG_ID, auth_ref="linear_key")

    # 3. Credentials resolution layer rejects non-canonical references
    with pytest.raises(SecretResolutionError, match="NON_CANONICAL_AUTH_REF"):
        resolve_signing_secret("linear_key")

    # 4. Codex reproduction scenario:
    # Register connection with canonical 'linear-key' at revision 1
    register_linear_connection(store, instance=ORG_ID, auth_ref="linear-key")
    old_secret = "secret-for-linear-key-111"
    monkeypatch.setenv("HAFLOW_INTAKE_SECRET_LINEAR_KEY", old_secret)

    now_ms = int(time.time() * 1000)
    raw_1 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="canon-1", webhook_timestamp=now_ms)).encode("utf-8")
    headers_1 = {
        "Linear-Signature": sign(raw_1, old_secret),
        "Linear-Delivery": "delivery-canon-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_1, _, body_1 = handle_linear_webhook(raw_1, headers_1, store, ORG_ID, now_ms=now_ms)
    assert status_1 == 200
    assert body_1.get("outcome") == "ACCEPTED"

    # Administrator updates authorization to non-canonical 'linear_key' at revision 2
    # In the vulnerability, 'linear_key' collapsed to LINEAR_KEY and silently reused old_secret at revision 2.
    # Now, the receiver strictly rejects non-canonical 'linear_key' with 401 NON_CANONICAL_AUTH_REF!
    store.update_connection_authorization(1, status="ACTIVE", reason="Rotate to underscore name", auth_ref="linear_key")
    raw_2 = json.dumps(sample_issue_payload("create", "unstarted", issue_id="canon-2", webhook_timestamp=now_ms)).encode("utf-8")
    headers_2 = {
        "Linear-Signature": sign(raw_2, old_secret),
        "Linear-Delivery": "delivery-canon-002",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_2, _, body_2 = handle_linear_webhook(raw_2, headers_2, store, ORG_ID, now_ms=now_ms)
    assert status_2 == 401
    assert "NON_CANONICAL_AUTH_REF" in body_2.get("error", "")


# =====================================================================
# V2 Tests: Bidirectional Synchronization, Echo Loop Suppression & Egress
# =====================================================================


def test_linear_client_states_and_mutations():
    """Verify LinearClient GraphQL state queries, caching, and mutation requests."""
    from herdr.intake.connectors.linear.client import LinearClient

    client = LinearClient("lin_api_test_secret_token")

    # 1. Mock get_team_workflow_states
    mock_states_data = {
        "data": {
            "team": {
                "id": "team-eng",
                "states": {
                    "nodes": [
                        {"id": "st-backlog", "name": "Backlog", "type": "backlog", "position": 1},
                        {"id": "st-started", "name": "In Progress", "type": "started", "position": 2},
                        {"id": "st-done", "name": "Done", "type": "completed", "position": 3},
                    ]
                }
            }
        }
    }
    with patch.object(client, "_post_graphql", return_value=mock_states_data) as mock_post:
        states = client.get_team_workflow_states("team-eng")
        assert len(states) == 3
        assert mock_post.call_count == 1

        # Verify caching (second call uses cache, no additional HTTP request)
        states_cached = client.get_team_workflow_states("team-eng")
        assert states_cached == states
        assert mock_post.call_count == 1

        # Verify state resolution by type
        started_id = client.resolve_state_id_by_type("team-eng", "started")
        assert started_id == "st-started"
        done_id = client.resolve_state_id_by_type("team-eng", "completed")
        assert done_id == "st-done"
        missing_id = client.resolve_state_id_by_type("team-eng", "nonexistent")
        assert missing_id is None

    # 2. Mock update_issue_state
    mock_update_data = {
        "data": {
            "issueUpdate": {
                "success": True,
                "issue": {"id": "issue-123", "state": {"id": "st-started", "name": "In Progress", "type": "started"}}
            }
        }
    }
    with patch.object(client, "_post_graphql", return_value=mock_update_data) as mock_post:
        res = client.update_issue_state("issue-123", "st-started")
        assert res["success"] is True
        assert res["issue"]["id"] == "issue-123"
        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        assert "mutation IssueUpdate" in args[0]
        assert args[1] == {"id": "issue-123", "input": {"stateId": "st-started"}}

    # 3. Mock create_comment
    mock_comment_data = {
        "data": {
            "commentCreate": {
                "success": True,
                "comment": {"id": "cmt-999", "body": "Hello Linear", "createdAt": "2026-11-20T08:00:00Z"}
            }
        }
    }
    with patch.object(client, "_post_graphql", return_value=mock_comment_data) as mock_post:
        res = client.create_comment("issue-123", "Hello Linear")
        assert res["success"] is True
        assert res["comment"]["id"] == "cmt-999"
        mock_post.assert_called_once()
        args = mock_post.call_args[0]
        assert "mutation CommentCreate" in args[0]
        assert args[1] == {"input": {"issueId": "issue-123", "body": "Hello Linear"}}


def test_credentials_resolve_api_token(monkeypatch):
    """Verify Linear API token resolution rules, memory registry, and fail-closed security."""
    from herdr.intake.connectors.linear.credentials import (
        SecretResolutionError,
        clear_token_registry,
        register_api_token,
        resolve_api_token,
        unregister_api_token,
    )

    clear_token_registry()

    # 1. Rejects invalid auth_ref
    with pytest.raises(SecretResolutionError, match="DISALLOWED_DEFAULT_AUTH_REF"):
        resolve_api_token("default")

    # 2. In-memory registry resolution
    register_api_token("linear-prod-v2", "lin_api_mem_12345")
    assert resolve_api_token("linear-prod-v2") == "lin_api_mem_12345"

    unregister_api_token("linear-prod-v2")
    with pytest.raises(SecretResolutionError, match="CREDENTIAL_UNRESOLVED"):
        resolve_api_token("linear-prod-v2")

    # 3. Environment variable lookup
    monkeypatch.setenv("HAFLOW_INTAKE_TOKEN_LINEAR_PROD_V2", "lin_api_env_token_456")
    assert resolve_api_token("linear-prod-v2") == "lin_api_env_token_456"

    # 4. Explicit loaded token with ref check
    assert resolve_api_token("linear-prod-v2", loaded_token="lin_api_direct", loaded_auth_ref="linear-prod-v2") == "lin_api_direct"
    with pytest.raises(SecretResolutionError, match="AUTH_REF_MISMATCH"):
        resolve_api_token("linear-prod-v2", loaded_token="lin_api_direct", loaded_auth_ref="linear-other-ref")


def test_echo_loop_detection_and_drop(tmp_path):
    """Verify Webhooks generated by HAFlow's bot actor are safely dropped with 200 ignored."""
    from herdr.intake.connectors.linear.connector import register_linear_connection
    from herdr.intake.connectors.linear.receiver import handle_linear_webhook

    path = tmp_path / "state_echo.db"
    initialize(path)
    store = EventStore(path, SCOPE, authorizer=lambda a, s: "admin", verifier=lambda d: d.verified_via == "SIGNATURE")
    register_linear_connection(store, instance=ORG_ID, auth_ref="linear-prod-v1")

    now_ms = int(time.time() * 1000)
    bot_id = "user-bot-haflow-uuid"

    # Normal user payload -> Accepted
    normal_payload = sample_issue_payload("update", "started", issue_id="echo-test-1", webhook_timestamp=now_ms)
    normal_payload["actor"] = {"id": "user-human-alice", "type": "user", "name": "Alice"}
    raw_norm = json.dumps(normal_payload).encode("utf-8")
    headers_norm = {
        "Linear-Signature": sign(raw_norm, SECRET),
        "Linear-Delivery": "delivery-echo-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_norm, _, body_norm = handle_linear_webhook(
        raw_norm, headers_norm, store, ORG_ID, signing_secret=SECRET, auth_ref="linear-prod-v1",
        bot_user_ids=(bot_id,), now_ms=now_ms,
    )
    assert status_norm == 200
    assert body_norm.get("outcome") == "ACCEPTED"

    # Bot actor payload -> Safely ignored (echo loop suppressed)
    bot_payload = sample_issue_payload("update", "started", issue_id="echo-test-1", webhook_timestamp=now_ms)
    bot_payload["actor"] = {"id": bot_id, "type": "user", "name": "HAFlow Bot"}
    raw_bot = json.dumps(bot_payload).encode("utf-8")
    headers_bot = {
        "Linear-Signature": sign(raw_bot, SECRET),
        "Linear-Delivery": "delivery-echo-002",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    status_bot, _, body_bot = handle_linear_webhook(
        raw_bot, headers_bot, store, ORG_ID, signing_secret=SECRET, auth_ref="linear-prod-v1",
        bot_user_ids=(bot_id,), now_ms=now_ms,
    )
    assert status_bot == 200
    assert body_bot.get("ignored") is True
    assert "BOT_ECHO_IGNORED" in body_bot.get("reason", "")


def test_sync_dispatcher_confirm_complete_and_fail(tmp_path):
    """Verify LinearSyncDispatcher updates state and creates comments on confirm, complete, and fail."""
    from herdr.intake.connectors.linear.client import LinearClient
    from herdr.intake.connectors.linear.sync import LinearSyncDispatcher

    db_path = tmp_path / "sync_test.db"
    initialize(db_path)

    from herdr.intake.schema import initialize_materialization, open_db
    from contextlib import closing
    initialize_materialization(db_path)
    with closing(open_db(db_path)) as db:
        db.execute(
            """INSERT INTO intake_connections (tenant_id, connection_id, provider, provider_instance_key, display_name, connector_version, status, config_revision, auth_ref, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("tenant-test", "linear", "linear", ORG_ID, "Linear", "1.0.0", "ACTIVE", 1, "linear-prod-v1", 1000, 1000)
        )
        db.execute(
            """INSERT INTO intake_objects (object_id, tenant_id, provider, provider_instance_key, object_type, external_id, observed_connection_id, version, snapshot_digest, snapshot_json, external_status, last_event_id, first_seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("obj-123", "tenant-test", "linear", ORG_ID, "issue", "issue-linear-uuid-456", "linear", "1", "sha256-mock",
             json.dumps({"title": "Test Issue", "description": "Test Desc", "team": {"id": "team-linear-1"}}), "OPEN", "evt-1", 1000, 1000)
        )
        db.execute(
            """INSERT INTO user_inbox_tasks (id, user_id, title, instruction, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("inbox_task_abc", "operator", "Test Issue", "Test Desc", "DRAFT", 1000, 1000)
        )
        db.execute(
            """INSERT INTO intake_task_links (link_id, tenant_id, object_id, purpose_key, disposition, task_id, origin_connection_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("link-123", "tenant-test", "obj-123", "review", "LINKED", "inbox_task_abc", "linear", 1000, 1000)
        )

    mock_client = MagicMock(spec=LinearClient)
    mock_client.resolve_state_id_by_type.side_effect = lambda team_id, st_type: f"state-{st_type}"
    mock_client.update_issue_state.return_value = {"success": True}
    mock_client.create_comment.return_value = {"success": True, "comment": {"id": "cmt-111"}}

    dispatcher = LinearSyncDispatcher(db_path, client=mock_client)

    # 1. Sync confirm
    res_confirm = dispatcher.sync_task_confirmed("link-123", user_id="operator", project_id="proj-haflow")
    assert res_confirm.ok is True
    assert res_confirm.action == "confirm"
    assert res_confirm.state_id == "state-started"
    mock_client.update_issue_state.assert_called_with("issue-linear-uuid-456", "state-started")
    mock_client.create_comment.assert_called()

    # 2. Sync completed
    res_complete = dispatcher.sync_task_completed("inbox_task_abc", summary="All PRs passed")
    assert res_complete.ok is True
    assert res_complete.action == "complete"
    assert res_complete.state_id == "state-completed"
    mock_client.update_issue_state.assert_called_with("issue-linear-uuid-456", "state-completed")

    # 3. Sync failed
    res_fail = dispatcher.sync_task_failed("inbox_task_abc", error="Build failed")
    assert res_fail.ok is True
    assert res_fail.action == "failed"
    mock_client.create_comment.assert_called()


def test_sync_dispatcher_resilient_error_handling(tmp_path):
    """Verify outbound sync failure never raises or disrupts calling context."""
    from herdr.intake.connectors.linear.client import LinearClient
    from herdr.intake.connectors.linear.sync import (
        dispatch_linear_sync_on_confirm,
        dispatch_linear_sync_on_task_status,
    )

    db_path = tmp_path / "sync_fail.db"
    initialize(db_path)

    # Client that fails with network error
    failing_client = MagicMock(spec=LinearClient)
    failing_client.resolve_state_id_by_type.side_effect = RuntimeError("Network timeout to api.linear.app")

    # None of these should raise
    res1 = dispatch_linear_sync_on_confirm(db_path, "nonexistent-link", client=failing_client)
    assert res1.ok is False

    res2 = dispatch_linear_sync_on_task_status(db_path, "nonexistent-task", "COMPLETED", client=failing_client)
    assert res2.ok is False


def test_lifecycle_hooks_trigger_linear_sync(tmp_path, monkeypatch):
    """Verify IntakeInbox.confirm and InboxStore status updates hook into Linear sync."""
    from herdr.intake.connectors.linear.connector import LinearConnector, register_linear_connection
    from herdr.intake.inbox import IntakeInbox
    from herdr.inbox.store import InboxStore
    from herdr.intake.worker import run_once

    db_path = tmp_path / "hooks_test.db"
    initialize(db_path)

    registry = {"a": {"project_id": "proj-a", "workspace_id": "ws-a"}}
    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": registry})

    store = EventStore(db_path, Scope("tenant-test", "linear"), authorizer=lambda a, s: "admin", verifier=lambda d: d.verified_via == "SIGNATURE")
    register_linear_connection(store, instance=ORG_ID, auth_ref="linear-prod-v1")
    conn = LinearConnector(store.scope, ORG_ID, SECRET)

    now_ms = int(time.time() * 1000)
    raw = json.dumps(sample_issue_payload("create", "unstarted", issue_id="hook-issue-1", webhook_timestamp=now_ms)).encode("utf-8")
    headers = {
        "Linear-Signature": sign(raw, SECRET),
        "Linear-Delivery": "delivery-hook-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    delivery = conn.process_webhook(raw, headers, now_ms=now_ms)
    store.accept_event(delivery)
    run_once(store, materialization={"user_id": "operator", "auto_assign": True, "project_id": "proj-a", "workspace_id": "ws-a"})

    config = {"tenant_id": "tenant-test", "user_id": "operator", "connections": {"linear": {}}}
    inbox = IntakeInbox(db_path, config)
    item, = inbox.list_items()
    assert item["status"] == "DRAFT"

    # 1. Verify confirm triggers dispatch_linear_sync_on_confirm
    with patch("herdr.intake.connectors.linear.sync.dispatch_linear_sync_on_confirm") as mock_confirm_sync:
        confirmed_item = inbox.change(item["link_id"], "confirm", item["revision"])
        assert confirmed_item["status"] == "CONFIRMED"
        mock_confirm_sync.assert_called_once_with(
            db_path, item["link_id"], user_id="operator", project_id="proj-a"
        )

    # 2. Verify InboxStore COMPLETED triggers dispatch_linear_sync_on_task_status
    with patch("herdr.intake.connectors.linear.sync.dispatch_linear_sync_on_task_status") as mock_status_sync:
        inbox_store = InboxStore(db_path=Path(db_path))
        # Insert a standalone active_intake task that can transition through normal workflow lifecycle
        active_task = inbox_store.create_task(
            title="Active Task",
            instruction="Execute task",
            project_id="proj-a",
            workspace_id="ws-a",
            source_type="active_intake",
            source_id="link-standalone",
        )
        tid = active_task["id"]
        inbox_store.update_task_status(tid, "PENDING")
        inbox_store.update_task_status(tid, "CLAIMED", worker_id="w-1")
        inbox_store.update_task_status(tid, "RUNNING", workflow_id="wf-1")
        inbox_store.update_task_status(tid, "COMPLETED")
        mock_status_sync.assert_called_once_with(
            Path(db_path), tid, "COMPLETED", error=None
        )


def test_confirmed_intake_task_dispatch_and_execution_bridge(tmp_path, monkeypatch):
    """Phase 3: Confirmed intake tasks can be dispatched to PENDING and claimed by InboxDispatcher."""
    import sqlite3
    from herdr.inbox.dispatcher import InboxDispatcher
    from herdr.inbox.store import InboxStore
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.worker import run_once
    from herdr.intake.schema import initialize, initialize_materialization
    from herdr.intake.store import EventStore, Scope
    from herdr.intake.connectors.linear.connector import LinearConnector, register_linear_connection

    db_path = tmp_path / "dispatch_bridge.db"
    initialize(db_path)
    initialize_materialization(db_path)

    scope = Scope("tenant-test", "linear")
    store = EventStore(db_path, scope, clock=lambda: 1000, verifier=lambda d: d.verified_via == "SIGNATURE", authorizer=lambda a, s: "admin")
    register_linear_connection(store, instance=ORG_ID, auth_ref="linear-prod-v1")
    conn_adapter = LinearConnector(store.scope, ORG_ID, SECRET)

    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": {
        "p": {"project_id": "proj-a", "workspace_id": "ws-a"}
    }})

    now_ms = int(time.time() * 1000)
    raw = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-bridge-001", webhook_timestamp=now_ms)).encode("utf-8")
    headers = {
        "Linear-Signature": sign(raw, SECRET),
        "Linear-Delivery": "del-bridge-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    delivery = conn_adapter.process_webhook(raw, headers, now_ms=now_ms)
    store.accept_event(delivery)

    policy = {"user_id": "operator", "project_id": "proj-a", "workspace_id": "ws-a", "auto_assign": True}
    run_once(store, materialization=policy)

    config = {"tenant_id": "tenant-test", "user_id": "operator", "connections": {"linear": {}}}
    inbox = IntakeInbox(db_path, config)
    item, = inbox.list_items()
    task_id = item["task_id"]

    # 1. Unconfirmed task cannot be manually forced to PENDING (Trigger blocks it)
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="active intake task may not be dispatched"):
        conn.execute("UPDATE user_inbox_tasks SET status = 'PENDING' WHERE id = ?", (task_id,))
    conn.close()

    # 2. Confirming with dispatch=True moves task to PENDING in user_inbox_tasks
    item = inbox.change(item["link_id"], "confirm", item["revision"], dispatch=True)
    assert item["status"] == "CONFIRMED"

    inbox_store = InboxStore(db_path=Path(db_path))
    task = inbox_store.get_task(task_id)
    assert task is not None
    assert task["status"] == "PENDING"
    assert task["project_id"] == "proj-a"
    assert task["workspace_id"] == "ws-a"

    # 3. Task is visible in manual_only task queries since it is now PENDING
    pending_tasks, total = inbox_store.list_tasks(status="PENDING", manual_only=True)
    assert any(t["id"] == task_id for t in pending_tasks)

    # 4. InboxDispatcher can claim the task
    dispatcher = InboxDispatcher(store=inbox_store, db_path=db_path, worker_id="test-worker")
    claimed = dispatcher.claim_next_task("ws-a")
    assert claimed is not None
    assert claimed["id"] == task_id
    assert claimed["status"] == "CLAIMED"
    assert claimed["worker_id"] is not None

    # 5. Transition to RUNNING and COMPLETED
    inbox_store.update_task_status(task_id, "RUNNING", workflow_id="wf-bridge-001")
    running_task = inbox_store.get_task(task_id)
    assert running_task["status"] == "RUNNING"
    assert running_task["workflow_id"] == "wf-bridge-001"

    inbox_store.update_task_status(task_id, "COMPLETED")
    completed_task = inbox_store.get_task(task_id)
    assert completed_task["status"] == "COMPLETED"


def test_unassign_pending_intake_task_reverts_to_draft(tmp_path, monkeypatch):
    """Unassigning a pending intake task resets status to DRAFT."""
    from herdr.inbox.store import InboxStore
    from herdr.intake.inbox import IntakeInbox
    from herdr.intake.worker import run_once
    from herdr.intake.schema import initialize, initialize_materialization
    from herdr.intake.store import EventStore, Scope
    from herdr.intake.connectors.linear.connector import LinearConnector, register_linear_connection

    db_path = tmp_path / "unassign_revert.db"
    initialize(db_path)
    initialize_materialization(db_path)

    scope = Scope("tenant-test", "linear")
    store = EventStore(db_path, scope, clock=lambda: 1000, verifier=lambda d: d.verified_via == "SIGNATURE", authorizer=lambda a, s: "admin")
    register_linear_connection(store, instance=ORG_ID, auth_ref="linear-prod-v1")
    conn_adapter = LinearConnector(store.scope, ORG_ID, SECRET)

    monkeypatch.setattr("herdr.intake.materializer.load_projects", lambda: {"projects": {
        "p": {"project_id": "proj-a", "workspace_id": "ws-a"}
    }})

    now_ms = int(time.time() * 1000)
    raw = json.dumps(sample_issue_payload("create", "unstarted", issue_id="issue-unassign-001", webhook_timestamp=now_ms)).encode("utf-8")
    headers = {
        "Linear-Signature": sign(raw, SECRET),
        "Linear-Delivery": "del-unassign-001",
        "Linear-Timestamp": str(now_ms),
        "Linear-Event": "Issue",
    }
    delivery = conn_adapter.process_webhook(raw, headers, now_ms=now_ms)
    store.accept_event(delivery)

    policy = {"user_id": "operator", "project_id": "proj-a", "workspace_id": "ws-a", "auto_assign": True}
    run_once(store, materialization=policy)

    config = {"tenant_id": "tenant-test", "user_id": "operator", "connections": {"linear": {}}}
    inbox = IntakeInbox(db_path, config)
    item, = inbox.list_items()
    task_id = item["task_id"]

    # Confirm with dispatch
    item = inbox.change(item["link_id"], "confirm", item["revision"], dispatch=True)
    inbox_store = InboxStore(db_path=Path(db_path))
    assert inbox_store.get_task(task_id)["status"] == "PENDING"

    # Unassign reverts task to DRAFT
    item = inbox.change(item["link_id"], "unassign", item["revision"])
    assert item["status"] == "PENDING_ASSIGNMENT"
    assert inbox_store.get_task(task_id)["status"] == "DRAFT"


def test_linear_sync_resolves_team_dynamically(tmp_path):
    """LinearSyncDispatcher resolves team_id dynamically if missing from snapshot."""
    import sqlite3
    from herdr.intake.connectors.linear.sync import LinearSyncDispatcher
    from herdr.intake.schema import initialize, initialize_materialization
    from unittest.mock import MagicMock

    db_path = tmp_path / "linear_dyn_team.db"
    initialize(db_path)
    initialize_materialization(db_path)

    # Insert a link and object with NO team_id in snapshot_json
    conn = sqlite3.connect(db_path)
    now = 1000
    conn.execute(
        """
        INSERT INTO intake_connections (tenant_id, connection_id, provider, provider_instance_key, display_name, config_revision, connector_version, status, auth_ref, created_at, updated_at)
        VALUES ('tenant-test', 'linear', 'linear', 'linear-inst', 'Linear', 1, '2.0.0', 'ACTIVE', 'linear-prod-v1', 1000, 1000)
        """
    )
    conn.execute(
        """
        INSERT INTO intake_objects (tenant_id, object_id, provider, provider_instance_key, external_id, object_type, observed_connection_id, external_status, version, snapshot_digest, snapshot_json, first_seen_at, updated_at, last_event_id)
        VALUES ('tenant-test', 'obj-1', 'linear', 'linear-inst', 'linear-issue-999', 'issue', 'linear', 'OPEN', 1, 'digest', '{"title": "Test Issue", "description": "Test Desc"}', 1000, 1000, 'ev-1')
        """
    )
    conn.execute(
        """
        INSERT INTO intake_task_links (link_id, tenant_id, object_id, purpose_key, disposition, created_at, updated_at, origin_connection_id, authorization_revision, last_event_id, body_connection_id, body_revision, project_id, workspace_id)
        VALUES ('link-1', 'tenant-test', 'obj-1', 'primary', 'LINKED', 1000, 1000, 'linear', 1, 'ev-1', 'linear', 1, 'proj-1', 'ws-1')
        """
    )
    conn.commit()
    conn.close()

    dispatcher = LinearSyncDispatcher(db_path=db_path)
    mock_client = MagicMock()
    mock_client.get_issue_team_id.return_value = "dynamic-team-id"
    mock_client.resolve_state_id_by_type.return_value = "state-started-123"
    mock_client.update_issue_state.return_value = {"success": True}
    mock_client.create_comment.return_value = {"comment": {"id": "c-123"}}
    dispatcher._resolve_client = MagicMock(return_value=mock_client)

    res = dispatcher.sync_task_confirmed(
        "link-1",
        user_id="operator",
        project_id="proj-1",
    )
    assert res.ok is True
    assert res.issue_id == "linear-issue-999"
    assert res.state_id == "state-started-123"
    assert res.comment_id == "c-123"
    mock_client.get_issue_team_id.assert_called_once_with("linear-issue-999")
    mock_client.resolve_state_id_by_type.assert_called_once_with("dynamic-team-id", "started")
    mock_client.update_issue_state.assert_called_once_with("linear-issue-999", "state-started-123")
