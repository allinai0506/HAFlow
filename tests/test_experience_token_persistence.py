"""Regression tests for persistent database storage of Experience Hub reviewer tokens."""

import json
import os
import tempfile
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from console.herdr_factory_console import Handler
from herdr import state_db


def test_user_token_db_crud(tmp_path):
    """Test saving, retrieving, and deleting tokens in state.db."""
    db_file = tmp_path / "test_state.db"
    state_db.init_db(db_file)

    # 1. Initially non-existent
    assert state_db.get_user_token("user_1", "experience_hub", db_path=db_file) is None

    # 2. Save token
    state_db.save_user_token("user_1", "experience_hub", "secret_token_123", db_path=db_file)
    assert state_db.get_user_token("user_1", "experience_hub", db_path=db_file) == "secret_token_123"

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
    # user_2 remains intact
    assert state_db.get_user_token("user_2", "experience_hub", db_path=db_file) == "user2_token_789"


def test_user_token_seeding_fallback(tmp_path, monkeypatch):
    """Verify that unseeded default reviewer token automatically seeds from credential file."""
    db_file = tmp_path / "test_state.db"
    state_db.init_db(db_file)

    fake_home = tmp_path / "fake_home"
    cred_dir = fake_home / ".herdr-controller" / "credentials"
    cred_dir.mkdir(parents=True)
    cred_file = cred_dir / "experience-console-reviewer.token"
    cred_file.write_text("seeded_reviewer_token_abc\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # First access should read from credential file and save to DB
    token = state_db.get_user_token("local-console-reviewer", "experience_hub", db_path=db_file)
    assert token == "seeded_reviewer_token_abc"

    # Verify it is now persisted in SQLite
    with state_db.get_db_connection(db_file) as conn:
        row = conn.execute(
            "SELECT token_value FROM user_tokens WHERE user_id = 'local-console-reviewer' AND token_type = 'experience_hub'"
        ).fetchone()
        assert row is not None
        assert row[0] == "seeded_reviewer_token_abc"

    # Remove the credential file; token should still be retrieved directly from DB
    cred_file.unlink()
    token_reloaded = state_db.get_user_token("local-console-reviewer", "experience_hub", db_path=db_file)
    assert token_reloaded == "seeded_reviewer_token_abc"


def test_console_experience_token_api(tmp_path, monkeypatch):
    """Verify console GET and POST /api/experience/token endpoints."""
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
        # 1. GET token initially empty
        req = urllib.request.Request(f"{base_url}/api/experience/token")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True
            # Might be empty string if no credentials file
            assert data.get("data", {}).get("token") in ("", None) or data.get("token") in ("", None)

        # 2. POST token to persist in DB
        body = json.dumps({"token": "my_new_hub_token_xyz"}).encode("utf-8")
        post_req = urllib.request.Request(
            f"{base_url}/api/experience/token",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(post_req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True

        # 3. GET token returns the persisted token
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            res_token = (data.get("data") or {}).get("token") or data.get("token")
            assert res_token == "my_new_hub_token_xyz"

        # 4. Verify directly in state.db
        token_in_db = state_db.get_user_token("local-console-reviewer", "experience_hub", db_path=db_file)
        assert token_in_db == "my_new_hub_token_xyz"

        # 5. Clear token via POST with empty token
        clear_body = json.dumps({"token": ""}).encode("utf-8")
        clear_req = urllib.request.Request(
            f"{base_url}/api/experience/token",
            data=clear_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(clear_req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["ok"] is True

        token_after_clear = state_db.get_user_token("local-console-reviewer", "experience_hub", db_path=db_file)
        assert token_after_clear in ("", None)
    finally:
        server.shutdown()
