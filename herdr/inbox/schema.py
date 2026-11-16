"""SQLite schema DDL and migrations for User Task Inbox."""

import sqlite3


def ensure_inbox_schema(conn: sqlite3.Connection) -> None:
    """Ensure user_inbox_tasks table and indices exist."""
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
