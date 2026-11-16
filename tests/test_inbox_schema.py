"""Unit tests for User Task Inbox SQLite schema."""

import sqlite3
import pytest
from pathlib import Path
from herdr.inbox.schema import ensure_inbox_schema
from herdr.state.connection import get_db_connection


def test_inbox_schema_creation_and_idempotence(tmp_path: Path):
    db_file = tmp_path / "test_state.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row

    # First call
    ensure_inbox_schema(conn)

    # Verify table exists
    table_info = conn.execute("PRAGMA table_info(user_inbox_tasks)").fetchall()
    cols = {row["name"]: row["type"].upper() for row in table_info}
    assert "id" in cols
    assert "user_id" in cols
    assert "project_id" in cols
    assert "workspace_id" in cols
    assert "title" in cols
    assert "instruction" in cols
    assert "priority" in cols
    assert "priority_weight" in cols
    assert "sort_order" in cols
    assert "status" in cols
    assert "workflow_id" in cols
    assert "idempotency_key" in cols
    assert "created_at" in cols
    assert "updated_at" in cols

    # Verify indices
    indices = conn.execute("PRAGMA index_list(user_inbox_tasks)").fetchall()
    idx_names = {row["name"] for row in indices}
    assert "idx_inbox_dispatch_queue" in idx_names
    assert "idx_inbox_user_status" in idx_names
    assert "idx_inbox_workspace_active" in idx_names
    assert "idx_inbox_workflow_id" in idx_names

    # Second call (idempotence)
    ensure_inbox_schema(conn)

    conn.close()


def test_inbox_schema_integration_with_state_db(tmp_path: Path):
    db_file = tmp_path / "app_state.db"
    conn = get_db_connection(db_file)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_inbox_tasks'"
        ).fetchone()
        assert row is not None
        assert row["name"] == "user_inbox_tasks"
    finally:
        conn.close()
