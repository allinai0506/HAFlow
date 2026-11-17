"""SQLite schema DDL and migrations for User Task Inbox."""

import json
import re
import sqlite3


def _has_global_idempotency_unique(conn: sqlite3.Connection) -> bool:
    """Check if user_inbox_tasks has the legacy global UNIQUE constraint on idempotency_key."""
    cur = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_inbox_tasks'")
    row = cur.fetchone()
    if not row or not row[0]:
        return False
    table_sql = row[0]
    if re.search(r"idempotency_key\s+TEXT\s+UNIQUE", table_sql, re.IGNORECASE):
        return True

    # Also inspect PRAGMA index_list for any unique index only on idempotency_key
    try:
        cur_idx = conn.execute("PRAGMA index_list('user_inbox_tasks')")
        for idx in cur_idx.fetchall():
            idx_name = idx[1] if isinstance(idx, (list, tuple)) else idx["name"]
            is_unique = bool(idx[2] if isinstance(idx, (list, tuple)) else idx["unique"])
            if is_unique and idx_name != "uq_inbox_user_idempotency":
                info_cur = conn.execute(f"PRAGMA index_info('{idx_name}')")
                cols = [
                    (col[2] if isinstance(col, (list, tuple)) else col["name"])
                    for col in info_cur.fetchall()
                ]
                if cols == ["idempotency_key"]:
                    return True
    except Exception:
        pass
    return False


def _create_inbox_indices(conn: sqlite3.Connection) -> None:
    """Ensure all indexes for user_inbox_tasks exist."""
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_inbox_dispatch_queue
            ON user_inbox_tasks(status, workspace_id, priority_weight DESC, sort_order ASC, created_at ASC);
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_inbox_user_status
            ON user_inbox_tasks(user_id, status, created_at DESC);
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_inbox_workspace_active
            ON user_inbox_tasks(workspace_id, status);
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_inbox_workflow_id
            ON user_inbox_tasks(workflow_id);
    """)

    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_inbox_user_idempotency
            ON user_inbox_tasks(user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
    """)


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """Migrate user_inbox_tasks to drop global idempotency_key UNIQUE and ensure execution_history_json atomically."""
    in_tx = getattr(conn, "in_transaction", False)
    if not in_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # Clear any leftover temporary table from an earlier crashed attempt
        conn.execute("DROP TABLE IF EXISTS user_inbox_tasks_migrating")

        # 1. Create table user_inbox_tasks_migrating with new structure
        conn.execute("""
            CREATE TABLE user_inbox_tasks_migrating (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                project_id TEXT,
                workspace_id TEXT,
                title TEXT NOT NULL,
                instruction TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_id TEXT,
                idempotency_key TEXT,
                priority TEXT NOT NULL DEFAULT 'NORMAL',
                priority_weight INTEGER NOT NULL DEFAULT 2,
                sort_order INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                workflow_id TEXT,
                template_name TEXT DEFAULT 'software-development-v1',
                agent_override TEXT DEFAULT 'auto',
                context_json TEXT DEFAULT '{}',
                execution_history_json TEXT DEFAULT '[]',
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

        # 2. Copy existing data; extract any execution_history from context_json into execution_history_json
        cur = conn.execute("SELECT * FROM user_inbox_tasks")
        col_names = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchall()
        for row in rows:
            if isinstance(row, sqlite3.Row):
                d = dict(row)
            elif isinstance(row, dict):
                d = row
            else:
                d = dict(zip(col_names, row))
            ctx = {}
            if d.get("context_json"):
                try:
                    ctx = json.loads(d["context_json"])
                except Exception:
                    ctx = {}
            exec_hist = ctx.pop("execution_history", []) if isinstance(ctx, dict) else []
            clean_ctx_json = json.dumps(ctx, ensure_ascii=False) if isinstance(ctx, dict) else (d.get("context_json") or "{}")
            exec_hist_json = json.dumps(exec_hist, ensure_ascii=False) if exec_hist else (d.get("execution_history_json") or "[]")

            conn.execute("""
                INSERT INTO user_inbox_tasks_migrating (
                    id, user_id, project_id, workspace_id, title, instruction,
                    source_type, source_id, idempotency_key, priority, priority_weight,
                    sort_order, status, workflow_id, template_name, agent_override,
                    context_json, execution_history_json, worker_id, lease_expires_at,
                    heartbeat_at, attempt_count, max_attempts, error,
                    created_at, updated_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                d.get("id"), d.get("user_id", "default"), d.get("project_id"), d.get("workspace_id"),
                d.get("title", ""), d.get("instruction", ""), d.get("source_type", "manual"),
                d.get("source_id"), d.get("idempotency_key"), d.get("priority", "NORMAL"),
                d.get("priority_weight", 2), d.get("sort_order", 0), d.get("status", "DRAFT"),
                d.get("workflow_id"), d.get("template_name", "software-development-v1"),
                d.get("agent_override", "auto"), clean_ctx_json, exec_hist_json,
                d.get("worker_id"), d.get("lease_expires_at"), d.get("heartbeat_at"),
                d.get("attempt_count", 0), d.get("max_attempts", 3), d.get("error"),
                d.get("created_at"), d.get("updated_at"), d.get("started_at"), d.get("completed_at")
            ))

        # 3. Replace table atomically
        conn.execute("DROP TABLE user_inbox_tasks")
        conn.execute("ALTER TABLE user_inbox_tasks_migrating RENAME TO user_inbox_tasks")

        # 4. Create all indexes within the same transaction
        _create_inbox_indices(conn)

        if not in_tx:
            conn.execute("COMMIT")
    except Exception:
        if not in_tx:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        raise


def ensure_inbox_schema(conn: sqlite3.Connection) -> None:
    """Ensure user_inbox_tasks table and indices exist, performing migrations if needed."""
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_inbox_tasks'")
    table_exists = bool(cur.fetchone())

    # Check for orphaned migration temp table if main table was dropped before rename in a crashed legacy migration
    cur_migrating = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_inbox_tasks_migrating'")
    migrating_exists = bool(cur_migrating.fetchone())

    if not table_exists and migrating_exists:
        # Recover orphaned table from interrupted rename
        conn.execute("ALTER TABLE user_inbox_tasks_migrating RENAME TO user_inbox_tasks")
        table_exists = True
    elif table_exists and migrating_exists:
        # Clean up stale temp table from uncommitted aborted migration
        conn.execute("DROP TABLE IF EXISTS user_inbox_tasks_migrating")

    if table_exists:
        if _has_global_idempotency_unique(conn):
            _migrate_legacy_schema(conn)
            return
        else:
            # Check if execution_history_json column exists
            info_cur = conn.execute("PRAGMA table_info('user_inbox_tasks')")
            cols = {
                (c[1] if isinstance(c, (list, tuple)) else c["name"])
                for c in info_cur.fetchall()
            }
            if "execution_history_json" not in cols:
                conn.execute("ALTER TABLE user_inbox_tasks ADD COLUMN execution_history_json TEXT DEFAULT '[]'")
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_inbox_tasks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                project_id TEXT,
                workspace_id TEXT,
                title TEXT NOT NULL,
                instruction TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'manual',
                source_id TEXT,
                idempotency_key TEXT,
                priority TEXT NOT NULL DEFAULT 'NORMAL',
                priority_weight INTEGER NOT NULL DEFAULT 2,
                sort_order INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                workflow_id TEXT,
                template_name TEXT DEFAULT 'software-development-v1',
                agent_override TEXT DEFAULT 'auto',
                context_json TEXT DEFAULT '{}',
                execution_history_json TEXT DEFAULT '[]',
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

    _create_inbox_indices(conn)
