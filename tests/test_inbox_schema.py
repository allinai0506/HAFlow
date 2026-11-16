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


def test_legacy_database_migration_atomic_rollback_on_failure_and_recovery(tmp_path: Path):
    """Verify that if a crash or exception occurs midway through legacy schema migration:
    1. The transaction rolls back cleanly without data loss.
    2. The original table and records remain completely intact.
    3. A subsequent migration run recovers cleanly and finishes the migration.
    """
    db_file = tmp_path / "legacy_inbox_fault.db"
    conn = sqlite3.connect(str(db_file), isolation_level=None)
    conn.row_factory = sqlite3.Row

    # 1. Setup legacy schema
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
            context_json TEXT DEFAULT '{"execution_history": [{"run": 1}]}',
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

    conn.execute("""
        INSERT INTO user_inbox_tasks (
            id, user_id, title, instruction, idempotency_key, created_at, updated_at
        ) VALUES ('task-1', 'alice', 'Task 1', 'Do 1', 'k1', 100.0, 100.0)
    """)
    conn.execute("""
        INSERT INTO user_inbox_tasks (
            id, user_id, title, instruction, idempotency_key, created_at, updated_at
        ) VALUES ('task-2', 'alice', 'Task 2', 'Do 2', 'k2', 101.0, 101.0)
    """)

    # 2. Inject fault during migration: proxy conn to fail right before rename
    class FaultyConnectionProxy:
        def __init__(self, target):
            self._target = target

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and "ALTER TABLE user_inbox_tasks_migrating RENAME TO user_inbox_tasks" in sql:
                raise sqlite3.OperationalError("Simulated crash right before table rename")
            return self._target.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._target, name)

    proxy = FaultyConnectionProxy(conn)

    with pytest.raises(sqlite3.OperationalError, match="Simulated crash right before table rename"):
        ensure_inbox_schema(proxy)

    # Verify rollback: original table and rows are 100% intact!
    rows = conn.execute("SELECT id, title, user_id FROM user_inbox_tasks ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == "task-1" and rows[0]["title"] == "Task 1"
    assert rows[1]["id"] == "task-2" and rows[1]["title"] == "Task 2"

    # Temporary table was either rolled back or dropped
    migrating_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_inbox_tasks_migrating'"
    ).fetchone()
    assert migrating_table is None

    # 3. Subsequent recovery: re-run migration without faults
    ensure_inbox_schema(conn)

    # Verify successfully migrated
    rows_after = conn.execute("SELECT id, title, execution_history_json FROM user_inbox_tasks ORDER BY id").fetchall()
    assert len(rows_after) == 2
    assert rows_after[0]["id"] == "task-1"
    assert "execution_history_json" in rows_after[0].keys()
    assert '[{"run": 1}]' in rows_after[0]["execution_history_json"]

    # Verify composite user unique constraint now in place (another user can use k1)
    conn.execute("""
        INSERT INTO user_inbox_tasks (
            id, user_id, title, instruction, idempotency_key, created_at, updated_at
        ) VALUES ('task-3', 'bob', 'Task 3', 'Do 3', 'k1', 102.0, 102.0)
    """)

    conn.close()


def test_legacy_database_migration_atomic_rollback_on_copy_failure(tmp_path: Path):
    """Verify that failure during data copying rolls back completely without corrupting original data."""
    db_file = tmp_path / "legacy_inbox_copy_fault.db"
    conn = sqlite3.connect(str(db_file), isolation_level=None)
    conn.row_factory = sqlite3.Row

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

    conn.execute("""
        INSERT INTO user_inbox_tasks (
            id, user_id, title, instruction, idempotency_key, created_at, updated_at
        ) VALUES ('t-1', 'alice', 'Task 1', 'Do 1', 'key1', 100.0, 100.0)
    """)
    conn.execute("""
        INSERT INTO user_inbox_tasks (
            id, user_id, title, instruction, idempotency_key, created_at, updated_at
        ) VALUES ('t-2', 'alice', 'Task 2', 'Do 2', 'key2', 101.0, 101.0)
    """)

    # Inject fault on 2nd insert into migrating table
    class FaultOnCopyProxy:
        def __init__(self, target):
            self._target = target
            self._copied_count = 0

        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and "INSERT INTO user_inbox_tasks_migrating" in sql:
                self._copied_count += 1
                if self._copied_count >= 2:
                    raise sqlite3.OperationalError("Simulated disk error during data copy")
            return self._target.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._target, name)

    proxy = FaultOnCopyProxy(conn)

    with pytest.raises(sqlite3.OperationalError, match="Simulated disk error during data copy"):
        ensure_inbox_schema(proxy)

    # Verify rollback: both original records intact
    rows = conn.execute("SELECT id, title FROM user_inbox_tasks ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == "t-1"
    assert rows[1]["id"] == "t-2"

    # Temporary table is gone
    migrating_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_inbox_tasks_migrating'"
    ).fetchone()
    assert migrating_table is None

    # Normal recovery
    ensure_inbox_schema(conn)
    rows_recovered = conn.execute("SELECT id FROM user_inbox_tasks ORDER BY id").fetchall()
    assert len(rows_recovered) == 2

    conn.close()
