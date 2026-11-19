"""Webhook Ingress HTTP handler and server for Linear Connector."""

from contextlib import closing
import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Mapping

from herdr.intake.protocol import MAX_BODY_BYTES, Scope
from herdr.intake.schema import open_db
from herdr.intake.store import EventStore, ScopeDenied
from .connector import LinearConnector
from .normalizer import OrganizationMismatchError, UnsupportedEventTypeError
from .verifier import SignatureVerificationError, TimestampSkewError

LOG = logging.getLogger(__name__)


def get_connection_config(store: EventStore) -> dict[str, Any]:
    """Fetch trusted connection status and config revision from the State DB."""
    with closing(open_db(store.path)) as db:
        row = db.execute(
            "SELECT config_revision, status, auth_ref, connector_version FROM intake_connections WHERE tenant_id = ? AND connection_id = ?",
            store.scope.keys,
        ).fetchone()
        if not row:
            raise ScopeDenied("unknown intake scope")
        return dict(row)


def handle_linear_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    store: EventStore,
    organization_id: str,
    signing_secret: str,
    *,
    config_revision: int | None = None,
    now_ms: int | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Process an incoming Linear Webhook HTTP request payload and commit to EventStore.

    Returns:
        (status_code, response_headers, response_body_dict)
    """
    json_headers = {"Content-Type": "application/json"}

    if len(raw_body) > MAX_BODY_BYTES:
        return 413, json_headers, {"ok": False, "error": "PAYLOAD_TOO_LARGE"}

    # Resolve config_revision and check connection availability from authoritative State DB
    if config_revision is None:
        try:
            conn_info = get_connection_config(store)
        except ScopeDenied as exc:
            LOG.warning("Linear webhook scope denied: %s", exc)
            return 403, json_headers, {"ok": False, "error": str(exc)}
        except Exception as exc:
            LOG.error("Failed to load connection config: %s", exc)
            return 500, json_headers, {"ok": False, "error": "INTERNAL_STORAGE_ERROR"}

        if conn_info["status"] not in ("ACTIVE", "DEGRADED"):
            LOG.warning("Linear webhook rejected: connection status is %s", conn_info["status"])
            return 403, json_headers, {
                "ok": False,
                "error": f"CONNECTION_UNAVAILABLE: status is {conn_info['status']}",
            }
        config_revision = int(conn_info["config_revision"])

    connector = LinearConnector(
        scope=store.scope,
        organization_id=organization_id,
        signing_secret=signing_secret,
        config_revision=config_revision,
    )

    try:
        delivery = connector.process_webhook(raw_body, headers, now_ms=now_ms)
        if delivery is None:
            # Valid webhook signature & timestamp, but non-Issue event (e.g. Project, Comment) safely ignored
            return 200, json_headers, {
                "received": True,
                "ignored": True,
                "reason": "UNSUPPORTED_EVENT_TYPE: only Issue events are processed",
            }
    except SignatureVerificationError as exc:
        LOG.warning("Linear webhook signature verification failed: %s", exc)
        return 401, json_headers, {"ok": False, "error": str(exc)}
    except TimestampSkewError as exc:
        LOG.warning("Linear webhook clock-skew rejected: %s", exc)
        return 400, json_headers, {"ok": False, "error": str(exc)}
    except OrganizationMismatchError as exc:
        LOG.warning("Linear webhook organization mismatch: %s", exc)
        return 403, json_headers, {"ok": False, "error": str(exc)}
    except UnsupportedEventTypeError as exc:
        return 200, json_headers, {
            "received": True,
            "ignored": True,
            "reason": str(exc),
        }
    except Exception as exc:
        LOG.error("Linear webhook processing error: %s", exc)
        return 400, json_headers, {"ok": False, "error": f"INVALID_PAYLOAD: {exc}"}

    try:
        receipt = store.accept_event(delivery)
        if receipt.outcome == "REJECTED":
            status_code = 409 if receipt.reason == "CONNECTION_UNAVAILABLE" else 400
            return status_code, json_headers, {
                "ok": False,
                "error": f"DELIVERY_REJECTED: {receipt.reason}",
                "receipt_id": receipt.receipt_id,
            }
        return 200, json_headers, {
            "received": True,
            "outcome": receipt.outcome,
            "event_id": receipt.event_id,
            "receipt_id": receipt.receipt_id,
        }
    except ScopeDenied as exc:
        LOG.warning("Linear webhook scope denied: %s", exc)
        return 403, json_headers, {"ok": False, "error": str(exc)}
    except Exception as exc:
        LOG.error("Linear webhook store commit failure: %s", exc)
        return 500, json_headers, {"ok": False, "error": "INTERNAL_STORAGE_ERROR"}


class LinearWebhookHandler(BaseHTTPRequestHandler):
    """Minimal standalone HTTP request handler for Linear webhooks."""

    store: EventStore
    organization_id: str
    signing_secret: str

    def setup(self):
        super().setup()
        self.request.settimeout(5.0)

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self.close_connection = True
            self.send_error(400, "Invalid Content-Length")
            return

        if content_length > MAX_BODY_BYTES:
            self.close_connection = True
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            body = b'{"ok": false, "error": "PAYLOAD_TOO_LARGE"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return

        try:
            self.request.settimeout(5.0)
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            if len(raw_body) != content_length:
                self.close_connection = True
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                body = b'{"ok": false, "error": "INCOMPLETE_BODY"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                return
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            self.send_response(408)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            body = b'{"ok": false, "error": "REQUEST_TIMEOUT"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            return

        status, resp_headers, body = handle_linear_webhook(
            raw_body=raw_body,
            headers=dict(self.headers),
            store=self.server.store,
            organization_id=self.server.organization_id,
            signing_secret=self.server.signing_secret,
        )

        resp_bytes = json.dumps(body).encode("utf-8")
        self.send_response(status)
        for k, v in resp_headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)
        self.wfile.flush()

    def log_message(self, format, *args):
        LOG.info(format, *args)
