#!/usr/bin/env python3
"""Standalone HTTP Ingress service for Linear Webhooks."""

import argparse
import logging
import os
import signal
import sys
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from herdr.intake.connectors.linear.receiver import LinearWebhookHandler
from herdr.intake.schema import initialize
from herdr.intake.store import EventStore, Scope

LOG = logging.getLogger("herdr-linear-webhook")


class LinearWebhookServer(HTTPServer):
    def __init__(self, server_address, RequestHandlerClass, store, organization_id, signing_secret):
        super().__init__(server_address, RequestHandlerClass)
        self.store = store
        self.organization_id = organization_id
        self.signing_secret = signing_secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Instance tenant ID")
    parser.add_argument("--connection", required=True, help="Registered Intake connection ID")
    parser.add_argument("--org-id", default=os.environ.get("LINEAR_ORGANIZATION_ID"), help="Linear Organization ID")
    parser.add_argument("--secret", default=os.environ.get("LINEAR_WEBHOOK_SECRET"), help="Linear Webhook Signing Secret")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HAFLOW_LINEAR_WEBHOOK_PORT", "8089")), help="Port to listen on (default: 8089)")
    parser.add_argument("--host", default=os.environ.get("HAFLOW_LINEAR_WEBHOOK_HOST", "0.0.0.0"), help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--db", help="Path to state.db")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if not args.org_id:
        parser.error("Linear Organization ID is required (via --org-id or LINEAR_ORGANIZATION_ID)")
    if not args.secret:
        parser.error("Linear Webhook Signing Secret is required (via --secret or LINEAR_WEBHOOK_SECRET)")

    db_path = Path(args.db).resolve() if args.db else initialize()
    scope = Scope(args.tenant, args.connection)
    store = EventStore(
        db_path,
        scope,
        verifier=lambda d: d.verified_via == "SIGNATURE",
    )

    server = LinearWebhookServer(
        (args.host, args.port),
        LinearWebhookHandler,
        store=store,
        organization_id=args.org_id,
        signing_secret=args.secret,
    )

    def shutdown(*_):
        LOG.info("Shutting down Linear webhook server...")
        Thread(target=server.shutdown).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    LOG.info("Linear webhook ingress listening on http://%s:%d/ (tenant: %s, connection: %s)",
             args.host, args.port, args.tenant, args.connection)
    server.serve_forever()
    server.server_close()
    LOG.info("Linear webhook server stopped.")


if __name__ == "__main__":
    main()
