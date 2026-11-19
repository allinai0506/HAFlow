"""Webhook Ingress HTTP handler and server for Linear Connector."""

import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Mapping

from herdr.intake.protocol import MAX_BODY_BYTES, Scope
from herdr.intake.store import EventStore, ScopeDenied
from .connector import LinearConnector
from .normalizer import OrganizationMismatchError
from .verifier import SignatureVerificationError, TimestampSkewError

LOG = logging.getLogger(__name__)


def handle_linear_webhook(
    raw_body: bytes,
    headers: Mapping[str, str],
    store: EventStore,
    organization_id: str,
    signing_secret: str,
    *,
    now_ms: int | None = None,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    """Process an incoming Linear Webhook HTTP request payload and commit to EventStore.

    Returns:
        (status_code, response_headers, response_body_dict)
    """
    json_headers = {"Content-Type": "application/json"}

    if len(raw_body) > MAX_BODY_BYTES:
        return 413, json_headers, {"ok": False, "error": "PAYLOAD_TOO_LARGE"}

    lower_headers = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    event_type = lower_headers.get("linear-event")

    # Linear V1 only consumes Issue events
    if event_type and event_type != "Issue":
        return 200, json_headers, {
            "received": True,
            "ignored": True,
            "reason": f"UNSUPPORTED_EVENT_TYPE: {event_type}",
        }

    connector = LinearConnector(
        scope=store.scope,
        organization_id=organization_id,
        signing_secret=signing_secret,
    )

    try:
        delivery = connector.process_webhook(raw_body, headers, now_ms=now_ms)
    except SignatureVerificationError as exc:
        LOG.warning("Linear webhook signature verification failed: %s", exc)
        return 401, json_headers, {"ok": False, "error": str(exc)}
    except TimestampSkewError as exc:
        LOG.warning("Linear webhook clock-skew rejected: %s", exc)
        return 400, json_headers, {"ok": False, "error": str(exc)}
    except OrganizationMismatchError as exc:
        LOG.warning("Linear webhook organization mismatch: %s", exc)
        return 403, json_headers, {"ok": False, "error": str(exc)}
    except Exception as exc:
        LOG.error("Linear webhook processing error: %s", exc)
        return 400, json_headers, {"ok": False, "error": f"INVALID_PAYLOAD: {exc}"}

    try:
        receipt = store.accept_event(delivery)
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

        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

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
