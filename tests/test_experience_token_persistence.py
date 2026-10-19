"""Regression tests for persistent storage of Experience Hub reviewer tokens."""

import json
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from console.herdr_factory_console import Handler
from herdr import state_db


def test_user_token_db_crud(tmp_path, monkeypatch):
    """Test saving, retrieving, and deleting tokens in protected credentials store with DB metadata."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    db_file = tmp_path / "test_state.db"
    state_db.init_db(db_file)

    # 1. Initially non-existent
    assert state_db.get_user_token("user_1", "experience_hub", db_path=db_file) is None

    # 2. Save token
    state_db.save_user_token("user_1", "experience_hub", "secret_token_123", db_path=db_file)
    assert state_db.get_user_token("user_1", "experience_hub", db_path=db_file) == "secret_token_123"

    # Verify token at rest is in 0700 dir and 0600 file, NOT plaintext in state.db
    token_file = fake_home / ".herdr-controller" / "credentials" / "user_1_experience_hub.token"
    assert token_file.is_file()
    assert (token_file.stat().st_mode & 0o777) == 0o600
    assert (token_file.parent.stat().st_mode & 0o777) == 0o700

    # 3. Update token
    state_db.save_user_token("user_1", "experience_hub", "updated_token_456", db_path=db_file)
    assert state_db.get_user_token("user_1", "experience_hub", db_path=db_file) == "updated_token_456"

    # 4. Multi-user isolation
    state_db.save_user_token("user_2", "experience_hub", "user2_token_789", db_path=db_file)
    assert state_db.get_user_token("user_1", "experience_hub", db_path=db_file) == "updated_token_456"
    assert state_db.get_user_token("user_2", "experience_hub", db_path=db_file) == "user2_token_789"

    # 5. Delete token
    assert state_db.delete_user_token("user_1", "experience_hub", db_path=db_file) is True
    assert state_db.get_user_token("user_1", "experience_hub", db_path=db_file) is None
    assert not token_file.exists()
    # user_2 remains intact
    assert state_db.get_user_token("user_2", "experience_hub", db_path=db_file) == "user2_token_789"


def test_user_token_seeding_fallback(tmp_path, monkeypatch):
    """Verify that unseeded default reviewer token automatically defaults from credential file."""
    fake_home = tmp_path / "fake_home"
    cred_dir = fake_home / ".herdr-controller" / "credentials"
    cred_dir.mkdir(parents=True)
    cred_file = cred_dir / "experience-console-reviewer.token"
    cred_file.write_text("seeded_reviewer_token_abc\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    db_file = tmp_path / "test_state.db"
    state_db.init_db(db_file)

    # Access without prior save should read from credential file
    token = state_db.get_user_token("local-console-reviewer", "experience_hub", db_path=db_file)
    assert token == "seeded_reviewer_token_abc"


def test_console_experience_token_api_security(tmp_path, monkeypatch):
    """Verify console GET does not leak secret token, and POST persists securely."""
    fake_home = tmp_path / "fake_home_api"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    db_file = tmp_path / "console_state.db"
    state_db.init_db(db_file)
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        # 1. GET token initially reports has_token: False
        req = urllib.request.Request(f"{base_url}/api/experience/token")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True
            # Never leaks raw secret token
            assert "token" not in (data.get("data") or data)
            assert (data.get("data") or data).get("has_token") is False

        # 2. POST token to persist in DB & credentials store
        body = json.dumps({"token": "my_new_hub_token_xyz"}).encode("utf-8")
        post_req = urllib.request.Request(
            f"{base_url}/api/experience/token",
            data=body,
            headers={"Content-Type": "application/json", "X-HAFlow-Console-Request": "1"},
            method="POST",
        )
        with urllib.request.urlopen(post_req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True
            assert (data.get("data") or data).get("saved") is True

        # 3. GET token now reports has_token: True, still without leaking secret string
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert (data.get("data") or data).get("has_token") is True
            assert "token" not in (data.get("data") or data)

        # 4. Verify token is stored server-side and retrieved securely
        token_stored = state_db.get_user_token("local-console-reviewer", "experience_hub", db_path=db_file)
        assert token_stored == "my_new_hub_token_xyz"

        # 5. Clear token via POST with empty token
        clear_body = json.dumps({"token": ""}).encode("utf-8")
        clear_req = urllib.request.Request(
            f"{base_url}/api/experience/token",
            data=clear_body,
            headers={"Content-Type": "application/json", "X-HAFlow-Console-Request": "1"},
            method="POST",
        )
        with urllib.request.urlopen(clear_req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True

        assert state_db.get_user_token("local-console-reviewer", "experience_hub", db_path=db_file) is None
    finally:
        server.shutdown()
