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
    from herdr.inbox.store import InboxStore
    db_file = tmp_path / "app_state.db"
    store = InboxStore(db_file)
    conn = get_db_connection(db_file)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_inbox_tasks'"
        ).fetchone()
        assert row is not None
        assert row["name"] == "user_inbox_tasks"
    finally:
        conn.close()


def test_legacy_database_migration_allows_duplicate_idempotency_keys_across_users(tmp_path: Path):
    """Verify that a database created with legacy global UNIQUE idempotency_key is migrated
    so different users can use the same idempotency key without collision.
    """
    db_file = tmp_path / "legacy_inbox.db"
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row

    # 1. Setup legacy schema with inline UNIQUE on idempotency_key
    conn.execute("""
        CREATE TABLE user_inbox_tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default',
            project_id TEXT,
            workspace_id TEXT,
            title TEXT NOT NULL,
            instruction TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_id TEXT,
            idempotency_key TEXT UNIQUE,
            priority TEXT NOT NULL DEFAULT 'NORMAL',
            priority_weight INTEGER NOT NULL DEFAULT 2,
            sort_order INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            workflow_id TEXT,
            template_name TEXT DEFAULT 'software-development-v1',
            agent_override TEXT DEFAULT 'auto',
            context_json TEXT DEFAULT '{}',
            worker_id TEXT,
            lease_expires_at REAL,
            heartbeat_at REAL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL
        );
    """)

    # 2. Insert Alice's task with idempotency_key='shared-key-1'
    conn.execute("""
        INSERT INTO user_inbox_tasks (
            id, user_id, title, instruction, idempotency_key, created_at, updated_at
        ) VALUES ('t1', 'alice', 'Alice Task', 'Do something', 'shared-key-1', 100.0, 100.0)
    """)
    conn.commit()

    # 3. Before migration: Bob inserting with the same key fails due to legacy global UNIQUE constraint
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO user_inbox_tasks (
                id, user_id, title, instruction, idempotency_key, created_at, updated_at
            ) VALUES ('t2', 'bob', 'Bob Task', 'Do something else', 'shared-key-1', 101.0, 101.0)
        """)

    # 4. Perform migration
    ensure_inbox_schema(conn)

    # 5. After migration: Bob can now insert with the same idempotency_key successfully
    conn.execute("""
        INSERT INTO user_inbox_tasks (
            id, user_id, title, instruction, idempotency_key, created_at, updated_at
        ) VALUES ('t2', 'bob', 'Bob Task', 'Do something else', 'shared-key-1', 101.0, 101.0)
    """)
    conn.commit()

    # Verify both records exist
    rows = conn.execute("SELECT user_id, idempotency_key FROM user_inbox_tasks ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["user_id"] == "alice" and rows[0]["idempotency_key"] == "shared-key-1"
    assert rows[1]["user_id"] == "bob" and rows[1]["idempotency_key"] == "shared-key-1"

    # Alice trying to insert a duplicate key still fails under composite user unique index
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""
            INSERT INTO user_inbox_tasks (
                id, user_id, title, instruction, idempotency_key, created_at, updated_at
            ) VALUES ('t3', 'alice', 'Alice Duplicate', 'Dup', 'shared-key-1', 102.0, 102.0)
        """)

    conn.close()
