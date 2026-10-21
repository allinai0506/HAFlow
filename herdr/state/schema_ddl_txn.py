"""schema ddl txn persistence; extracted without changing behavior."""

import json
import time
from .constants import _route_decision_bucket_exprs
from .schema_ddl_helpers import _ensure_completion_columns, _ensure_event_columns, _ensure_intervention_columns, _ensure_task_columns, _ensure_working_context_columns, _ensure_working_context_source_clock_schema, _ensure_working_context_source_heads_schema


def _ensure_schema_ddl_transaction(conn):
    conn.execute("BEGIN IMMEDIATE;")
    _ensure_working_context_source_heads_schema(conn)
    _ensure_working_context_source_clock_schema(conn)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS working_context_metric_events (
            metric_id TEXT PRIMARY KEY,
            context_id TEXT NOT NULL,
            run_scope TEXT NOT NULL,
            run_id TEXT,
            workflow_id TEXT,
            task_id TEXT NOT NULL,
            agent_role TEXT NOT NULL,
            reused INTEGER NOT NULL DEFAULT 0,
            changed INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        );
    """);

    conn.execute("""
        CREATE TABLE IF NOT EXISTS steering_items (
            steer_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            workflow_id TEXT,
            instruction TEXT NOT NULL,
            operator TEXT,
            urgent INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            dispatched_at REAL,
            created_at REAL,
            payload_json TEXT
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS steering_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            task_id TEXT,
            steer_id TEXT,
            instruction TEXT,
            operator TEXT,
            urgent INTEGER DEFAULT 0,
            reason TEXT,
            timestamp REAL,
            payload_json TEXT
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS interventions (
            intervention_id TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            workflow_id TEXT,
            task_id TEXT NOT NULL,
            evaluation_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            finding_refs_json TEXT NOT NULL DEFAULT '[]',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 0,
            requested_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL,
            execution_owner TEXT,
            lease_until REAL,
            result_json TEXT,
            error_json TEXT
        );
    """)

    # Collaboration events: minimal cross-agent handoff facts (V1).
    # Identity is (run_id, from_task_id, to_task_id, type, source_fact_id).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collaboration_events (
            event_id TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            workflow_id TEXT,
            from_task_id TEXT NOT NULL,
            to_task_id TEXT NOT NULL,
            from_agent TEXT NOT NULL DEFAULT '',
            to_agent TEXT NOT NULL DEFAULT '',
            from_pane_id TEXT,
            to_pane_id TEXT,
            type TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            artifact_refs_json TEXT NOT NULL DEFAULT '[]',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            context_refs_json TEXT NOT NULL DEFAULT '[]',
            requires_response INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            source_fact_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            dispatched_at REAL,
            acknowledged_at REAL,
            completed_at REAL,
            handoff_created_at REAL NOT NULL,
            handoff_dispatched_at REAL,
            handoff_acknowledged_at REAL,
            handoff_completed_at REAL
        );
    """)

    # Indexes for fast lookup and DAG queries
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_tokens (
            user_id TEXT NOT NULL,
            token_type TEXT NOT NULL,
            has_token INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL,
            PRIMARY KEY (user_id, token_type)
        );
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_wf_created ON tasks(workflow_id, created_at, task_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workflows_project ON workflows (CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$.project_id') END);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cp_wf_created ON checkpoints(workflow_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_completion_observations_task ON completion_observations(task_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cp_parent ON checkpoints(parent_checkpoint_id);")
    _ensure_event_columns(conn)
    _ensure_task_columns(conn)
    _ensure_completion_columns(conn)
    _ensure_intervention_columns(conn)
    _ensure_working_context_columns(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_wf ON events(workflow_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence, id);")
    # Exact canary-bucket reads (the Controlled Rollout guard) must be
    # served by an index, not by scanning route_decision history while
    # evaluating JSON per row. The WHERE fragments built by
    # ``_exact_bucket_sql`` reuse ``_route_decision_bucket_exprs``
    # verbatim so SQLite resolves them against this partial expression
    # index. The CASE/json_valid wrapper keeps malformed payloads from
    # ever failing index maintenance on INSERT: they index as NULL and
    # simply never match an exact-bucket read.
    _bucket_exprs = _route_decision_bucket_exprs()
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_route_decision_bucket "
        f"ON events({_bucket_exprs[0]}, {_bucket_exprs[1]}, "
        f"{_bucket_exprs[2]}, {_bucket_exprs[3]}, timestamp, id) "
        "WHERE event_type = 'route_decision';")
    for row in conn.execute(
        "SELECT run_id, task_id, workflow_id, timestamp, payload_json "
        "FROM events WHERE source = 'trajectory' AND event_type = 'observation_created'"
    ).fetchall():
        try:
            observation_id = (json.loads(row["payload_json"] or "{}").get("observation") or {}).get("observation_id")
        except (TypeError, json.JSONDecodeError):
            observation_id = None
        if observation_id:
            conn.execute(
                "INSERT OR IGNORE INTO observation_receipts "
                "(observation_id, run_id, task_id, workflow_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (observation_id, row["run_id"], row["task_id"], row["workflow_id"], row["timestamp"] or time.time()),
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_run ON trajectory_findings(run_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_type ON trajectory_findings(finding_type, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id, created_at, observation_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_task ON observations(task_id, created_at, observation_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_type ON observations(source_type, created_at, observation_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_sha256 ON observations(sha256);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_packs_run_created ON context_packs(run_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_packs_task_created ON context_packs(task_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_working_contexts_task_role ON working_contexts(task_id, agent_role, compiled_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_working_contexts_task_created ON working_contexts(task_id, compiled_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_working_contexts_scope_version ON working_contexts(run_scope, source_watermark DESC, compiled_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_working_context_metrics_scope ON working_context_metric_events(run_scope, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_working_context_metrics_run ON working_context_metric_events(run_id, created_at DESC);")
    # Deduplication is fingerprint-based, not sequence-only: a Finding or task
    # state can change without a new Trajectory sequence.
    conn.execute("DROP INDEX IF EXISTS ux_context_packs_run_sequence;")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_context_packs_run_sequence ON context_packs(run_id, source_event_sequence);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steering_task ON steering_items(task_id, status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_steering_hist_task ON steering_history(task_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interventions_run ON interventions(run_id, requested_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interventions_task ON interventions(task_id, status, requested_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_run ON collaboration_events(run_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_tasks ON collaboration_events(from_task_id, to_task_id, status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collab_identity ON collaboration_events(identity_key);")

    # Eval results: point-in-time evaluation facts for one run revision.
    # Intentionally no FOREIGN KEY clauses: deleting a workflow must retain
    # eval rows for auditability. Eval is distinct from Metrics aggregation.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            eval_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            verdict TEXT,
            scores_json TEXT,
            evidence_json TEXT,
            requirements_satisfied INTEGER,
            verification_passed INTEGER,
            human_intervention_count INTEGER,
            final_status TEXT,
            warnings_json TEXT,
            task_status TEXT,
            task_id TEXT,
            workflow_id TEXT,
            created_at REAL NOT NULL,
            UNIQUE(run_id, revision)
        );
    """)
    source_clock_tables = (
        "workflows", "tasks", "events", "trajectory_findings", "observations",
        "observation_receipts", "eval_results", "collaboration_events",
    )
    task_scoped_tables = {
        "events", "trajectory_findings", "observations", "observation_receipts", "eval_results",
    }

    def source_clock_workflow(alias: str, source_table: str) -> str:
        if source_table in {"workflows", "tasks", "collaboration_events"}:
            return f"COALESCE(NULLIF({alias}.workflow_id, ''), '')"
        task_workflow = (
            f"(SELECT t.workflow_id FROM tasks t WHERE t.task_id = {alias}.task_id LIMIT 1)"
        )
        return f"COALESCE(NULLIF({alias}.workflow_id, ''), NULLIF({task_workflow}, ''), '')"

    def _safe_task_json(column: str, path: str) -> str:
        """Null-safe JSON extraction for trigger bodies.

        A malformed historical payload must never break source writes:
        without the json_valid guard, repairing a damaged task row via
        save_task would crash inside the AFTER UPDATE trigger while it
        reads OLD.payload_json.
        """
        return (
            f"CASE WHEN json_valid({column}) "
            f"THEN json_extract({column}, '{path}') END"
        )

    def source_clock_scope(alias: str, source_table: str) -> str:
        if source_table == "workflows":
            return f"COALESCE(NULLIF({alias}.workflow_id, ''), '')"
        if source_table == "tasks":
            return (
                f"COALESCE(NULLIF({_safe_task_json(f'{alias}.payload_json', '$.workflow_run_id')}, ''), "
                f"NULLIF({_safe_task_json(f'{alias}.payload_json', '$.execution_id')}, ''), "
                f"NULLIF({alias}.workflow_id, ''), '')"
            )
        if source_table == "collaboration_events":
            return f"COALESCE(NULLIF({alias}.run_id, ''), NULLIF({alias}.workflow_id, ''), '')"
        task_scope = (
            f"(SELECT COALESCE(NULLIF({_safe_task_json('t.payload_json', '$.workflow_run_id')}, ''), "
            f"NULLIF({_safe_task_json('t.payload_json', '$.execution_id')}, ''), NULLIF(t.workflow_id, ''), '') "
            f"FROM tasks t WHERE t.task_id = {alias}.task_id LIMIT 1)"
        )
        run_scope = (
            f"(SELECT COALESCE(NULLIF({_safe_task_json('t.payload_json', '$.workflow_run_id')}, ''), "
            f"NULLIF({_safe_task_json('t.payload_json', '$.execution_id')}, ''), NULLIF(t.workflow_id, ''), '') "
            f"FROM tasks t WHERE {alias}.task_id IS NOT NULL "
            f"AND {_safe_task_json('t.payload_json', '$.run_id')} = {alias}.run_id LIMIT 1)"
        )
        return (
            f"COALESCE({task_scope}, {run_scope}, NULLIF({alias}.workflow_id, ''), "
            f"NULLIF({alias}.run_id, ''), '')"
        )

    def source_clock_has_task(alias: str, source_table: str) -> str:
        if source_table not in task_scoped_tables:
            return "0"
        return f"EXISTS(SELECT 1 FROM tasks t WHERE t.task_id = {alias}.task_id)"

    for source_table in source_clock_tables:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger_name = f"trg_working_context_source_clock_{source_table}_{operation.lower()}"
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            alias = "OLD" if operation == "DELETE" else "NEW"
            scope_expr = source_clock_scope(alias, source_table)
            workflow_expr = source_clock_workflow(alias, source_table)
            statements = [
                f"""
                INSERT INTO working_context_source_clock (run_scope, workflow_id, revision)
                SELECT {scope_expr}, {workflow_expr}, 1
                WHERE {scope_expr} <> ''
                ON CONFLICT(run_scope, workflow_id) DO UPDATE SET revision = revision + 1;
                """
            ]
            if source_table == "workflows":
                statements.append(
                    f"""
                    INSERT INTO working_context_source_clock (run_scope, workflow_id, revision)
                    SELECT DISTINCT h.run_scope, h.workflow_id, 1
                    FROM working_context_source_heads h
                    WHERE h.workflow_id = {workflow_expr}
                      AND h.run_scope <> {scope_expr}
                    ON CONFLICT(run_scope, workflow_id) DO UPDATE SET revision = revision + 1;
                    """
                )
            if source_table in task_scoped_tables:
                statements.append(
                    f"""
                    INSERT INTO working_context_source_clock (run_scope, workflow_id, revision)
                    SELECT DISTINCT h.run_scope, h.workflow_id, 1
                    FROM working_context_source_heads h
                    WHERE h.workflow_id = {workflow_expr}
                      AND NOT {source_clock_has_task(alias, source_table)}
                    ON CONFLICT(run_scope, workflow_id) DO UPDATE SET revision = revision + 1;
                    """
                )
            if operation == "UPDATE":
                old_scope_expr = source_clock_scope("OLD", source_table)
                old_workflow_expr = source_clock_workflow("OLD", source_table)
                old_head_filter = (
                    "" if source_table == "workflows"
                    else f" AND NOT {source_clock_has_task('OLD', source_table)}"
                )
                statements.append(
                    f"""
                    INSERT INTO working_context_source_clock (run_scope, workflow_id, revision)
                    SELECT {old_scope_expr}, {old_workflow_expr}, 1
                    WHERE {old_scope_expr} <> '' AND (
                        {old_scope_expr} <> {scope_expr}
                        OR {old_workflow_expr} <> {workflow_expr}
                    )
                    ON CONFLICT(run_scope, workflow_id) DO UPDATE SET revision = revision + 1;
                    """
                )
                if source_table in {"workflows"} | task_scoped_tables:
                    statements.append(
                        f"""
                        INSERT INTO working_context_source_clock (run_scope, workflow_id, revision)
                        SELECT DISTINCT h.run_scope, h.workflow_id, 1
                        FROM working_context_source_heads h
                        WHERE h.workflow_id = {old_workflow_expr}{old_head_filter}
                        ON CONFLICT(run_scope, workflow_id) DO UPDATE SET revision = revision + 1;
                        """
                    )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger_name}
                AFTER {operation} ON {source_table}
                BEGIN
                    {''.join(statements)}
                END;
                """
            )
    conn.execute("COMMIT;")
