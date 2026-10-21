"""schema ddl helpers persistence; extracted without changing behavior."""

import sqlite3


def _ensure_working_context_source_heads_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate source heads to task-specific identity."""
    table_info = conn.execute(
        "PRAGMA table_info(working_context_source_heads)"
    ).fetchall()
    columns = {str(row["name"]) for row in table_info}
    primary_key = {
        str(row["name"]) for row in table_info if int(row["pk"] or 0) > 0
    }
    migrated = False
    if columns and (
        primary_key != {"run_scope", "workflow_id", "task_id"}
        or "task_id" not in columns
    ):
        migrated = True
        legacy_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_context_source_heads_legacy'"
        ).fetchone() is not None
        if legacy_exists:
            conn.execute("DROP TABLE IF EXISTS working_context_source_heads_legacy_main")
            conn.execute(
                "ALTER TABLE working_context_source_heads RENAME TO working_context_source_heads_legacy_main"
            )
        else:
            conn.execute(
                "ALTER TABLE working_context_source_heads RENAME TO working_context_source_heads_legacy"
            )
        columns = set()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS working_context_source_heads (
            run_scope TEXT NOT NULL,
            workflow_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            source_version TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (run_scope, workflow_id, task_id)
        );
    """)
    # NOTE: legacy execution-shared rows (run_scope, workflow_id) lack task_id
    # and their source_version covers a different (pre-relevant-projection)
    # identity. They are retained in *_legacy for audit but never promoted
    # into the task-specific heads: each task registers its own projection
    # version independently so parallel tasks cannot mark each other stale.
    if not migrated:
        # Fresh database or already task-specific: no history to seed and
        # live heads must never be overwritten.
        return
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_contexts'"
        ).fetchone()
        is None
    ):
        return
    # P1-1 baseline: historical working_contexts rows carry watermarks from the
    # execution-shared era. Seed each task-specific head at its task's maximum
    # historical watermark (with that row's source_version) so the next
    # registration continues monotonically (7 -> 8) instead of dropping to 1,
    # which would mark fresh candidates stale against retained history.
    conn.execute("""
        INSERT OR IGNORE INTO working_context_source_heads
            (run_scope, workflow_id, task_id, source_version, revision, updated_at)
        SELECT run_scope, COALESCE(workflow_id, ''), task_id,
               COALESCE(source_version, ''),
               COALESCE(source_watermark, 0),
               COALESCE(compiled_at, 0)
          FROM (
                SELECT run_scope, workflow_id, task_id, source_version,
                       source_watermark, compiled_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY run_scope, COALESCE(workflow_id, ''), task_id
                           ORDER BY COALESCE(source_watermark, 0) DESC,
                                    COALESCE(compiled_at, 0) DESC,
                                    rowid DESC
                       ) AS rn
                  FROM working_contexts
               )
         WHERE rn = 1
    """)

def _ensure_working_context_source_clock_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the source clock to execution-scope granularity."""
    table_info = conn.execute(
        "PRAGMA table_info(working_context_source_clock)"
    ).fetchall()
    columns = {str(row["name"]) for row in table_info}
    primary_key = {
        str(row["name"]) for row in table_info if int(row["pk"] or 0) > 0
    }
    legacy_revision = 0
    legacy_clock_table = False
    if columns and (
        not {"run_scope", "workflow_id"}.issubset(columns)
        or primary_key != {"run_scope", "workflow_id"}
    ):
        row = (
            conn.execute(
                "SELECT revision FROM working_context_source_clock WHERE id = 1"
            ).fetchone()
            if "id" in columns else None
        )
        legacy_revision = int(row["revision"] or 0) if row is not None else 0
        for source_table in (
            "workflows", "tasks", "events", "trajectory_findings", "observations",
            "observation_receipts", "eval_results", "collaboration_events",
        ):
            for operation in ("insert", "update", "delete"):
                conn.execute(
                    f"DROP TRIGGER IF EXISTS trg_working_context_source_clock_{source_table}_{operation}"
                )
        conn.execute(
            "ALTER TABLE working_context_source_clock RENAME TO working_context_source_clock_legacy"
        )
        legacy_clock_table = True
    conn.execute("""
        CREATE TABLE IF NOT EXISTS working_context_source_clock (
            run_scope TEXT NOT NULL,
            workflow_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL,
            PRIMARY KEY (run_scope, workflow_id)
        );
    """)
    legacy_clock_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_context_source_clock_legacy'"
    ).fetchone() is not None
    if legacy_clock_table or legacy_clock_exists:
        legacy_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(working_context_source_clock_legacy)"
            ).fetchall()
        }
        if {"run_scope", "workflow_id", "revision"}.issubset(legacy_columns):
            conn.execute(
                """
                INSERT INTO working_context_source_clock
                    (run_scope, workflow_id, revision)
                SELECT COALESCE(run_scope, ''), COALESCE(workflow_id, ''),
                       COALESCE(MAX(revision), 0)
                FROM working_context_source_clock_legacy
                WHERE 1
                GROUP BY run_scope, workflow_id
                ON CONFLICT(run_scope, workflow_id) DO UPDATE SET
                    revision = MAX(revision, excluded.revision)
                """
            )
        elif legacy_revision:
            conn.execute(
                """
                INSERT OR IGNORE INTO working_context_source_clock
                    (run_scope, workflow_id, revision)
                SELECT run_scope, COALESCE(workflow_id, ''), ?
                FROM working_context_source_heads
                """,
                (legacy_revision,),
            )

def _ensure_rollout_schema(conn: sqlite3.Connection) -> None:
    """Create Controlled Rollout tables (idempotent, migrates the key).

    Current State + Immutable Audit Events: ``rollout_state`` holds one
    row per bucket (the present percentage); ``rollout_audit`` appends
    exactly one row per state change and is never updated or deleted.
    Called from ``_ensure_schema`` for fresh DBs and directly by the
    rollout helpers so pre-existing DBs gain the tables lazily.

    ``rollout_state`` is keyed by the composite identity
    ``(agent, node, task_type)``, not by a delimited string: a
    ``"/"``-joined key cannot distinguish ``("a/b", "c", "d")`` from
    ``("a", "b/c", "d")``, so two different buckets could read and write
    one row. The pre-composite table is migrated in place (rows copied by
    their real columns) rather than left to collide.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rollout_state (
            agent TEXT NOT NULL,
            node TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT '',
            percentage INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            updated_by TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (agent, node, task_type)
        );
    """)
    _migrate_rollout_state_key(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rollout_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_key TEXT NOT NULL,
            agent TEXT NOT NULL,
            node TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT '',
            previous_percentage INTEGER NOT NULL,
            new_percentage INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            algorithm_version TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rollout_audit_bucket "
        "ON rollout_audit(agent, node, task_type, id DESC);")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rollout_audit_created "
        "ON rollout_audit(created_at DESC, id DESC);")

def _migrate_rollout_state_key(conn: sqlite3.Connection) -> None:
    """Rebuild a pre-composite ``rollout_state`` onto the real key.

    The old table used a delimited ``bucket_key`` PRIMARY KEY. Rows are
    copied by their own ``agent``/``node``/``task_type`` columns, so the
    only thing lost is the ambiguity that let two buckets share a row.
    """
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(rollout_state)").fetchall()
    }
    primary_key = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(rollout_state)").fetchall()
        if int(row["pk"] or 0) > 0
    }
    if primary_key == {"agent", "node", "task_type"}:
        return
    if not columns or "bucket_key" not in columns:
        return
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.execute("ALTER TABLE rollout_state RENAME TO rollout_state_legacy")
        conn.execute("""
            CREATE TABLE rollout_state (
                agent TEXT NOT NULL,
                node TEXT NOT NULL,
                task_type TEXT NOT NULL DEFAULT '',
                percentage INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (agent, node, task_type)
            );
        """)
        conn.execute(
            "INSERT OR REPLACE INTO rollout_state "
            "(agent, node, task_type, percentage, updated_at, updated_by, "
            "reason) SELECT agent, node, COALESCE(task_type, ''), percentage, "
            "updated_at, updated_by, reason FROM rollout_state_legacy"
        )
        conn.execute("DROP TABLE rollout_state_legacy")
        conn.execute("COMMIT;")
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise

def _ensure_task_columns(conn: sqlite3.Connection) -> None:
    """Add the monotonic task version used by compare-and-set callers."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks);")}
    if "version" in columns:
        return
    try:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN version INTEGER NOT NULL DEFAULT 0;"
        )
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks);")}
        if "version" not in columns:
            raise

def _ensure_completion_columns(conn: sqlite3.Connection) -> None:
    """Add ordering metadata to completion observations from older databases."""
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(completion_observations);")
    }
    if "last_observed_at" not in columns:
        try:
            conn.execute(
                "ALTER TABLE completion_observations "
                "ADD COLUMN last_observed_at REAL;"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    if "blocker_present" in columns:
        return
    # C03c: durable BLOCKER marker presence, so screen residue that outlives
    # a reopen is never re-sampled as a fresh blocker.
    try:
        conn.execute(
            "ALTER TABLE completion_observations "
            "ADD COLUMN blocker_present INTEGER;"
        )
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise

def _ensure_event_columns(conn: sqlite3.Connection) -> None:
    """Upgrade pre-WorkflowEvent event tables in place."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events);")}
    column_types = {
        "node_id": "TEXT",
        "agent_id": "TEXT",
        "source": "TEXT",
        "run_id": "TEXT",
        "sequence": "INTEGER",
    }
    for name in ("node_id", "agent_id", "source", "run_id", "sequence"):
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {name} {column_types[name]};")
            except sqlite3.OperationalError as exc:
                # Another process may have won the schema-upgrade race after
                # this connection's PRAGMA snapshot. Only accept that exact
                # race after confirming the target column now exists.
                if "duplicate column name" not in str(exc).lower():
                    raise
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(events);")}
                if name not in columns:
                    raise
            columns.add(name)

def _ensure_intervention_columns(conn: sqlite3.Connection) -> None:
    """Upgrade Action Protocol V1 rows with recovery ownership fields."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(interventions);")}
    column_types = {"execution_owner": "TEXT", "lease_until": "REAL"}
    for name, column_type in column_types.items():
        if name in columns:
            continue
        try:
            conn.execute(f"ALTER TABLE interventions ADD COLUMN {name} {column_type};")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(interventions);")}
            if name not in columns:
                raise
        columns.add(name)

def _ensure_working_context_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(working_contexts);")}
    if "source_watermark" not in columns:
        try:
            conn.execute("ALTER TABLE working_contexts ADD COLUMN source_watermark INTEGER NOT NULL DEFAULT 0;")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

def _ensure_eval_replay_columns(conn: sqlite3.Connection) -> None:
    """Upgrade eval/replay tables with B3/B6 fact columns in place."""
    eval_columns = {row["name"] for row in conn.execute("PRAGMA table_info(eval_results);")}
    eval_types = {
        "scores_json": "TEXT",
        "requirements_satisfied": "INTEGER",
        "verification_passed": "INTEGER",
        "human_intervention_count": "INTEGER",
        "final_status": "TEXT",
        "warnings_json": "TEXT",
        "task_status": "TEXT",
        "task_id": "TEXT",
        "workflow_id": "TEXT",
    }
    for name, column_type in eval_types.items():
        if name in eval_columns:
            continue
        try:
            conn.execute(f"ALTER TABLE eval_results ADD COLUMN {name} {column_type};")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            eval_columns = {row["name"] for row in conn.execute("PRAGMA table_info(eval_results);")}
            if name not in eval_columns:
                raise
        eval_columns.add(name)
    replay_columns = {row["name"] for row in conn.execute("PRAGMA table_info(replay_specs);")}
    replay_types = {"snapshot_path": "TEXT", "policy_json": "TEXT"}
    for name, column_type in replay_types.items():
        if name in replay_columns:
            continue
        try:
            conn.execute(f"ALTER TABLE replay_specs ADD COLUMN {name} {column_type};")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
            replay_columns = {row["name"] for row in conn.execute("PRAGMA table_info(replay_specs);")}
            if name not in replay_columns:
                raise
        replay_columns.add(name)
