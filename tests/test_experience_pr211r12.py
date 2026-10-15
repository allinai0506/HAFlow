"""PR #211 R12: respond route, redirect credential guard, valid error status."""

import json
import tempfile
import time
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch


def test_interaction_respond_route_exists():
    from console.herdr_factory_console import Handler
    console = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=console.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{console.server_port}/api/task/interaction/respond",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status != 404
    finally:
        console.shutdown()


def test_redirect_strips_credentials_cross_origin():
    from herdr.experience.client import _NoCredentialRedirect
    handler = _NoCredentialRedirect()
    same = urllib.request.Request("http://hub.internal/a", headers={"Authorization": "Bearer s"})
    kept = handler.redirect_request(same, None, 302, "m", {}, "http://hub.internal/b")
    assert kept.get_header("Authorization") == "Bearer s"
    cross = urllib.request.Request("http://hub.internal/a", headers={"Authorization": "Bearer s"})
    moved = handler.redirect_request(cross, None, 302, "m", {}, "http://evil.example:9/b")
    assert moved.get_header("Authorization") is None


def test_console_error_status_is_valid_http():
    from console.herdr_factory_console import Handler
    from herdr.experience.hub_server import ExperienceHubServer
    with tempfile.TemporaryDirectory() as td:
        srv = ExperienceHubServer(host="127.0.0.1", port=0, db_path=Path(td) / "h.db")
        srv.storage.register_token("tok", user_id="u", role="contributor", allowed_customers=["acme"])
        srv.start(block=False)
        time.sleep(0.05)
        try:
            with patch.dict("os.environ", {"HERDR_HUB_URL": "http://127.0.0.1:1"}):
                import os
                os.environ.pop("HERDR_HUB_TOKEN", None)
                console = HTTPServer(("127.0.0.1", 0), Handler)
                Thread(target=console.serve_forever, daemon=True).start()
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{console.server_port}/api/experience/candidates",
                        headers={"Authorization": "Bearer tok"},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            status = resp.status
                    except urllib.error.HTTPError as e:
                        status = e.code
                    assert 100 <= status <= 599 and status != 0
                finally:
                    console.shutdown()
        finally:
            srv.stop()
