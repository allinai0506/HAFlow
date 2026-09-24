#!/usr/bin/env python3
"""Herdr Checkpoint Store V2: SQLite Embedded State Engine.

Provides durable, transactional state management and time-travel snapshots
using the Python standard library `sqlite3` (Zero external dependencies).

Core Capabilities:
- WAL mode (Write-Ahead Logging) for safe concurrent reads & single-writer transactions
- Single-transaction atomic snapshot capture across workflows and tasks
- Fast indexed snapshot listing without directory scanning
- Time-travel state forking (branching a new workflow from any historical checkpoint)
- Parent-child checkpoint DAG lineage tracking
- Lossless bi-directional migration between V1 JSON files and V2 SQLite DB
"""

import json
import hashlib
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

import fcntl
from typing import Any, Dict, List, Optional, Tuple

from herdr.transitions import (
    ACTIVE_TASK_STATUSES,
    COMPLETED_TASK_STATUSES,
    validate_task_transition,
    validate_workflow_transition,
)


HOME = Path.home()
CONTROLLER_DIR = HOME / ".herdr-controller"


def get_default_db_path() -> Path:
    """Resolve active SQLite DB path from environment or default location."""
    env_path = os.environ.get("HERDR_STATE_DB")
    if env_path:
        return Path(env_path)
    if os.environ.get("CHECKPOINTS_DIR"):
        return Path(os.environ["CHECKPOINTS_DIR"]).parent / "state.db"
    if os.environ.get("WORKFLOWS_FILE"):
        return Path(os.environ["WORKFLOWS_FILE"]).parent / "state.db"
    if os.environ.get("TASKS_FILE"):
        p = Path(os.environ["TASKS_FILE"])
        return p.parent / "state.db" if p.name == "tasks.json" else p.with_suffix(".db")
    return CONTROLLER_DIR / "state.db"



_INITIALIZED_DBS: set = set()


@contextmanager
def _schema_initialization_lock(path: Path):
    lock_path = Path(f"{path}.schema.lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _ensure_working_context_source_heads_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate source heads to scope-plus-workflow identity."""
    table_info = conn.execute(
        "PRAGMA table_info(working_context_source_heads)"
    ).fetchall()
    columns = {str(row["name"]) for row in table_info}
    primary_key = {
        str(row["name"]) for row in table_info if int(row["pk"] or 0) > 0
    }
    if columns and primary_key != {"run_scope", "workflow_id"}:
        conn.execute(
            "ALTER TABLE working_context_source_heads RENAME TO working_context_source_heads_legacy"
        )
        columns = set()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS working_context_source_heads (
            run_scope TEXT NOT NULL,
            workflow_id TEXT NOT NULL DEFAULT '',
            source_version TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (run_scope, workflow_id)
        );
    """)
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_context_source_heads_legacy'"
    ).fetchone():
        conn.execute(
            """
            INSERT OR IGNORE INTO working_context_source_heads
                (run_scope, workflow_id, source_version, revision, updated_at)
            SELECT run_scope, COALESCE(workflow_id, ''), source_version, revision, updated_at
            FROM working_context_source_heads_legacy
            """
        )


def _ensure_working_context_source_clock_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the source clock to execution-scope granularity."""
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(working_context_source_clock)")
    }
    legacy_revision = 0
    if columns and not {"run_scope", "workflow_id"}.issubset(columns):
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS working_context_source_clock (
            run_scope TEXT NOT NULL,
            workflow_id TEXT NOT NULL DEFAULT '',
            revision INTEGER NOT NULL,
            PRIMARY KEY (run_scope, workflow_id)
        );
    """)
    if legacy_revision:
        conn.execute(
            """
            INSERT OR IGNORE INTO working_context_source_clock
                (run_scope, workflow_id, revision)
            SELECT run_scope, COALESCE(workflow_id, ''), ?
            FROM working_context_source_heads
            """,
            (legacy_revision,),
        )


def _ensure_schema(conn: sqlite3.Connection, path_key: str) -> None:
    """Execute table and index creation DDL once per database path."""
    if path_key in _INITIALIZED_DBS:
        return

    conn.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            workflow_id TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            template_name TEXT,
            current_stage TEXT,
            config_json TEXT,
            metadata_json TEXT,
            created_at REAL,
            updated_at REAL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            node TEXT,
            stage TEXT,
            agent TEXT,
            status TEXT,
            stage_verdict TEXT,
            stage_verdict_note TEXT,
            pane_id TEXT,
            goal TEXT,
            blocker TEXT,
            payload_json TEXT,
            created_at REAL,
            updated_at REAL,
            FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            tag TEXT,
            parent_checkpoint_id TEXT,
            created_at REAL,
            workflow_status TEXT,
            task_count INTEGER,
            snapshot_json TEXT,
            metadata_json TEXT,
            FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT,
            node_id TEXT,
            task_id TEXT,
            agent_id TEXT,
            event_type TEXT,
            payload_json TEXT,
            timestamp REAL,
            source TEXT,
            run_id TEXT,
            sequence INTEGER
        );
    """)

    # Observer analysis (NOT facts): findings are judgments derived from the
    # trajectory/events ledger and must stay queryable apart from it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trajectory_findings (
            finding_id TEXT PRIMARY KEY,
            finding_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            node TEXT,
            agent TEXT,
            agent_session_id TEXT,
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            summary TEXT,
            suspected_cause TEXT,
            recommended_action TEXT,
            confidence REAL,
            evidence_json TEXT,
            metadata_json TEXT,
            created_at REAL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            observation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            content_ref TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            excerpt TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            UNIQUE(run_id, source_type, source_ref, sha256)
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS observation_receipts (
            observation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            created_at REAL NOT NULL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_packs (
            context_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT,
            workflow_id TEXT,
            goal TEXT,
            current_state_json TEXT NOT NULL DEFAULT '{}',
            completed_json TEXT NOT NULL DEFAULT '[]',
            verified_facts_json TEXT NOT NULL DEFAULT '[]',
            important_findings_json TEXT NOT NULL DEFAULT '[]',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            artifact_refs_json TEXT NOT NULL DEFAULT '[]',
            open_issues_json TEXT NOT NULL DEFAULT '[]',
            next_focus_json TEXT NOT NULL DEFAULT '[]',
            source_event_sequence INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS working_contexts (
            context_id TEXT PRIMARY KEY,
            run_scope TEXT NOT NULL,
            run_id TEXT,
            workflow_id TEXT,
            task_id TEXT NOT NULL,
            node_id TEXT,
            agent_role TEXT NOT NULL,
            context_fingerprint TEXT NOT NULL,
            source_version TEXT,
            source_watermark INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            compiled_at REAL NOT NULL
        );
    """);

    conn.execute("BEGIN IMMEDIATE;")
    _ensure_working_context_source_heads_schema(conn)
    _ensure_working_context_source_clock_schema(conn)
    conn.execute("COMMIT;")

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

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_wf ON tasks(workflow_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_wf_created ON tasks(workflow_id, created_at, task_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cp_wf_created ON checkpoints(workflow_id, created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cp_parent ON checkpoints(parent_checkpoint_id);")
    _ensure_event_columns(conn)
    _ensure_intervention_columns(conn)
    _ensure_working_context_columns(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_wf ON events(workflow_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, timestamp);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run_sequence ON events(run_id, sequence, id);")
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

    def source_clock_scope(alias: str, source_table: str) -> str:
        if source_table == "workflows":
            return f"COALESCE(NULLIF({alias}.workflow_id, ''), '')"
        if source_table == "tasks":
            return (
                f"COALESCE(NULLIF(json_extract({alias}.payload_json, '$.workflow_run_id'), ''), "
                f"NULLIF(json_extract({alias}.payload_json, '$.execution_id'), ''), "
                f"NULLIF({alias}.workflow_id, ''), '')"
            )
        if source_table == "collaboration_events":
            return f"COALESCE(NULLIF({alias}.run_id, ''), NULLIF({alias}.workflow_id, ''), '')"
        task_scope = (
            "(SELECT COALESCE(NULLIF(json_extract(t.payload_json, '$.workflow_run_id'), ''), "
            "NULLIF(json_extract(t.payload_json, '$.execution_id'), ''), NULLIF(t.workflow_id, ''), '') "
            f"FROM tasks t WHERE t.task_id = {alias}.task_id LIMIT 1)"
        )
        run_scope = (
            "(SELECT COALESCE(NULLIF(json_extract(t.payload_json, '$.workflow_run_id'), ''), "
            "NULLIF(json_extract(t.payload_json, '$.execution_id'), ''), NULLIF(t.workflow_id, ''), '') "
            f"FROM tasks t WHERE json_extract(t.payload_json, '$.run_id') = {alias}.run_id LIMIT 1)"
        )
        return (
            f"COALESCE({task_scope}, {run_scope}, NULLIF({alias}.workflow_id, ''), "
            f"NULLIF({alias}.run_id, ''), '')"
        )

    def source_clock_has_task(alias: str, source_table: str) -> str:
        if source_table not in task_scoped_tables:
            return "0"
        return (
            f"(EXISTS(SELECT 1 FROM tasks t WHERE t.task_id = {alias}.task_id) "
            f"OR EXISTS(SELECT 1 FROM tasks t "
            f"WHERE json_extract(t.payload_json, '$.run_id') = {alias}.run_id))"
        )

    conn.execute("BEGIN IMMEDIATE;")
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
                    SELECT h.run_scope, h.workflow_id, 1
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
                    SELECT h.run_scope, h.workflow_id, 1
                    FROM working_context_source_heads h
                    WHERE h.workflow_id = {workflow_expr}
                      AND NOT {source_clock_has_task(alias, source_table)}
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

    # Replay specs: lineage edges from a source run to a replay run.
    # Intentionally no FOREIGN KEY clauses: specs survive workflow deletion
    # and never imply liveness of either run.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS replay_specs (
            spec_id TEXT PRIMARY KEY,
            source_run_id TEXT NOT NULL,
            replay_run_id TEXT NOT NULL UNIQUE,
            workflow_id TEXT,
            definition_json TEXT,
            lineage_json TEXT,
            snapshot_path TEXT,
            policy_json TEXT,
            created_at REAL NOT NULL
        );
    """)
    _ensure_eval_replay_columns(conn)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_results_run_revision ON eval_results(run_id, revision DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_results_created ON eval_results(created_at DESC);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_specs_source ON replay_specs(source_run_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_specs_replay ON replay_specs(replay_run_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_specs_workflow ON replay_specs(workflow_id);")

    # One-time atomic bootstrap migration if initializing a DB where legacy JSON exists and not yet completed
    cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'v1_migration_done';")
    if not cur.fetchone():
        # Check if the database already contains existing runtime state (e.g. upgraded from PR #21
        # where SQLite was in active use before schema_meta was introduced).
        # If runtime state already exists, SQLite is authoritative and we MUST NOT import legacy JSON,
        # otherwise stale JSON could overwrite newer SQLite state on upgrade.
        has_existing_state = bool(conn.execute("""
            SELECT (
                EXISTS(SELECT 1 FROM workflows LIMIT 1) OR
                EXISTS(SELECT 1 FROM tasks LIMIT 1) OR
                EXISTS(SELECT 1 FROM checkpoints LIMIT 1) OR
                EXISTS(SELECT 1 FROM steering_items LIMIT 1) OR
                EXISTS(SELECT 1 FROM events LIMIT 1)
            );
        """).fetchone()[0])

        if has_existing_state:
            conn.execute("""
                INSERT INTO schema_meta (key, value) VALUES ('v1_migration_done', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1';
            """)
            _INITIALIZED_DBS.add(path_key)
        else:
            target_path = Path(path_key)
            target_dir = target_path.parent
            wf_file = target_dir / "workflows.json"
            st_file = target_dir / "steering.json"
            if os.environ.get("CHECKPOINTS_DIR"):
                cp_dir = Path(os.environ["CHECKPOINTS_DIR"])
            else:
                cp_dir = target_dir / "checkpoints"

            # If a dedicated companion json exists for this DB file (e.g. /tmp/xyz.db -> /tmp/xyz.json)
            if target_path.name != "state.db" and target_path.with_suffix(".json").exists():
                tasks_file = target_path.with_suffix(".json")
            else:
                tasks_file = target_dir / "tasks.json"

            has_legacy = (
                wf_file.exists()
                or tasks_file.exists()
                or st_file.exists()
                or (cp_dir.exists() and any(cp_dir.rglob("*.json")))
            )
            if not has_legacy:
                conn.execute("""
                    INSERT INTO schema_meta (key, value) VALUES ('v1_migration_done', '1')
                    ON CONFLICT(key) DO UPDATE SET value = '1';
                """)
                _INITIALIZED_DBS.add(path_key)
            else:
                try:
                    conn.execute("BEGIN TRANSACTION;")

                    if wf_file.exists():
                        data = json.loads(wf_file.read_text(encoding="utf-8"))
                        wfs_data = data.get("workflows", {})
                        if isinstance(wfs_data, dict):
                            for wid, wf_obj in wfs_data.items():
                                if isinstance(wf_obj, dict):
                                    wf_obj.setdefault("workflow_id", wid)
                                    save_workflow(wf_obj, db_path=None, conn=conn)
                        elif isinstance(wfs_data, list):
                            for wf_obj in wfs_data:
                                if isinstance(wf_obj, dict) and wf_obj.get("workflow_id"):
                                    save_workflow(wf_obj, db_path=None, conn=conn)

                    if tasks_file.exists():
                        data = json.loads(tasks_file.read_text(encoding="utf-8"))
                        t_list = data.get("tasks", [])
                        if isinstance(t_list, list):
                            for t_obj in t_list:
                                if isinstance(t_obj, dict) and t_obj.get("task_id"):
                                    save_task(t_obj, db_path=None, conn=conn)

                    if st_file.exists():
                        s_data = json.loads(st_file.read_text(encoding="utf-8"))
                        q_dict = s_data.get("steering_queues", {})
                        if isinstance(q_dict, dict):
                            for tid, q in q_dict.items():
                                if isinstance(q, list):
                                    for s_item in q:
                                        s_item.setdefault("task_id", tid)
                                        save_steer(s_item, conn=conn)
                        h_list = s_data.get("history", [])
                        if isinstance(h_list, list):
                            for h in h_list:
                                record_steering_history(h, conn=conn)

                    if cp_dir.exists():
                        for f in sorted(cp_dir.rglob("*.json")):
                            if f.is_file():
                                snap = json.loads(f.read_text(encoding="utf-8"))
                                cpid = snap.get("checkpoint_id")
                                wid = snap.get("workflow_id")
                                if not cpid or not wid:
                                    continue
                                cur_wf = conn.execute("SELECT 1 FROM workflows WHERE workflow_id = ?", (wid,))
                                if not cur_wf.fetchone():
                                    save_workflow(
                                        snap.get("workflow") or {"workflow_id": wid, "title": wid, "status": "pending"},
                                        db_path=None,
                                        conn=conn,
                                    )
                                conn.execute("""
                                    INSERT INTO checkpoints (
                                        checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                                        created_at, workflow_status, task_count, snapshot_json, metadata_json
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(checkpoint_id) DO NOTHING;
                                """, (
                                    cpid,
                                    wid,
                                    snap.get("tag", ""),
                                    snap.get("parent_checkpoint_id"),
                                    float(snap.get("created_at") or time.time()),
                                    snap.get("workflow", {}).get("status", "pending"),
                                    len(snap.get("tasks", [])),
                                    json.dumps(snap, ensure_ascii=False),
                                    json.dumps(snap.get("metadata", {}), ensure_ascii=False),
                                ))

                    conn.execute("""
                        INSERT INTO schema_meta (key, value) VALUES ('v1_migration_done', '1')
                        ON CONFLICT(key) DO UPDATE SET value = '1';
                    """)
                    conn.execute("COMMIT;")
                    _INITIALIZED_DBS.add(path_key)
                except Exception:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
    else:
        _INITIALIZED_DBS.add(path_key)


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


def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Create a thread-safe connection to the SQLite state database with WAL mode."""
    path = db_path or get_default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(
        str(path),
        timeout=10.0,
        isolation_level=None,  # autocommit mode; we manage transactions explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    
    path_key = str(path.resolve())
    schema_lock = (
        nullcontext()
        if path_key in _INITIALIZED_DBS
        else _schema_initialization_lock(Path(path_key))
    )
    try:
        with schema_lock:
            # Configure high-concurrency PRAGMAs while first-time schema
            # initialization is serialized across processes.
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            _ensure_schema(conn, path_key)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def init_db(db_path: Optional[Path] = None) -> Path:
    """Initialize database tables and indexes if they do not exist."""
    path = db_path or get_default_db_path()
    # Force initialization even if cached
    _INITIALIZED_DBS.discard(str(path.resolve()))
    conn = get_db_connection(path)
    conn.close()
    return path



def save_workflow(
    wf_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert a workflow record."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    wid = wf_dict.get("workflow_id")
    if not wid:
        raise ValueError("workflow_id is required")

    now = time.time()
    title = wf_dict.get("title", "")
    status = wf_dict.get("status", "pending")
    template_name = wf_dict.get("template_name", "")
    current_stage = wf_dict.get("current_stage") or wf_dict.get("stage", "")
    config_json = json.dumps(wf_dict.get("config", {}), ensure_ascii=False)
    created_at = float(wf_dict.get("created_at") or now)

    # Exclude special keys from metadata
    meta = {k: v for k, v in wf_dict.items() if k not in {
        "workflow_id", "title", "status", "template_name", "current_stage", "stage", "config", "created_at"
    }}
    metadata_json = json.dumps(meta, ensure_ascii=False)

    try:
        conn.execute("""
            INSERT INTO workflows (workflow_id, title, status, template_name, current_stage, config_json, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workflow_id) DO UPDATE SET
                title=excluded.title,
                status=excluded.status,
                template_name=excluded.template_name,
                current_stage=excluded.current_stage,
                config_json=excluded.config_json,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at;
        """, (wid, title, status, template_name, current_stage, config_json, metadata_json, created_at, now))
    finally:
        if should_close:
            conn.close()


def get_workflow(workflow_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a workflow by its workflow_id."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            return None

        meta = json.loads(row["metadata_json"] or "{}")
        cfg = json.loads(row["config_json"] or "{}")

        wf = dict(meta)
        wf.update({
            "workflow_id": row["workflow_id"],
            "title": row["title"],
            "status": row["status"],
            "template_name": row["template_name"],
            "current_stage": row["current_stage"],
            "config": cfg,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
        return wf
    finally:
        conn.close()


def list_workflows(
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Fetch all workflows, optionally filtered by status."""
    conn = get_db_connection(db_path)
    try:
        if status:
            cur = conn.execute("SELECT * FROM workflows WHERE status = ? ORDER BY created_at DESC", (status,))
        else:
            cur = conn.execute("SELECT * FROM workflows ORDER BY created_at DESC")
        results = []
        for row in cur.fetchall():
            meta = json.loads(row["metadata_json"] or "{}")
            cfg = json.loads(row["config_json"] or "{}")
            wf = dict(meta)
            wf.update({
                "workflow_id": row["workflow_id"],
                "title": row["title"],
                "status": row["status"],
                "template_name": row["template_name"],
                "current_stage": row["current_stage"],
                "config": cfg,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
            results.append(wf)
        return results
    finally:
        conn.close()


def delete_workflow(workflow_id: str, db_path: Optional[Path] = None) -> bool:
    """Delete a workflow and cascade its tasks/checkpoints."""
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        cur = conn.execute("DELETE FROM workflows WHERE workflow_id = ?", (workflow_id,))
        deleted = cur.rowcount > 0
        conn.execute("COMMIT;")
        return deleted
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def save_task(
    task_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert a task record."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    tid = task_dict.get("task_id")
    if not tid:
        raise ValueError("task_id is required")
    wid = task_dict.get("workflow_id") or "default"

    # Auto-ensure parent workflow exists to prevent foreign key violation
    cur_wf = conn.execute("SELECT 1 FROM workflows WHERE workflow_id = ?", (wid,))
    if not cur_wf.fetchone():
        save_workflow({"workflow_id": wid, "title": wid, "status": "pending"}, db_path, conn=conn)

    now = time.time()
    node = task_dict.get("node") or task_dict.get("stage", "")
    stage = task_dict.get("stage") or node
    agent = task_dict.get("agent", "auto")
    status = task_dict.get("status", "pending")
    verdict = task_dict.get("stage_verdict", "")
    verdict_note = task_dict.get("stage_verdict_note", "")
    pane_id = task_dict.get("pane_id", "")
    goal = task_dict.get("goal", "")
    blocker = task_dict.get("blocker", "")
    created_at = float(task_dict.get("created_at") or task_dict.get("started_at") or now)

    payload = {k: v for k, v in task_dict.items() if k not in {
        "task_id", "workflow_id", "node", "stage", "agent", "status",
        "stage_verdict", "stage_verdict_note", "pane_id", "goal", "blocker", "created_at"
    }}
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        conn.execute("""
            INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, stage_verdict, stage_verdict_note, pane_id, goal, blocker, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                workflow_id=excluded.workflow_id,
                node=excluded.node,
                stage=excluded.stage,
                agent=excluded.agent,
                status=excluded.status,
                stage_verdict=excluded.stage_verdict,
                stage_verdict_note=excluded.stage_verdict_note,
                pane_id=excluded.pane_id,
                goal=excluded.goal,
                blocker=excluded.blocker,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at;
        """, (tid, wid, node, stage, agent, status, verdict, verdict_note, pane_id, goal, blocker, payload_json, created_at, now))
    finally:
        if should_close:
            conn.close()


def _decode_task_row(row: sqlite3.Row) -> Dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    task = dict(payload)
    task.update({
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "node": row["node"],
        "stage": row["stage"],
        "agent": row["agent"],
        "status": row["status"],
        "stage_verdict": row["stage_verdict"],
        "stage_verdict_note": row["stage_verdict_note"],
        "pane_id": row["pane_id"],
        "goal": row["goal"],
        "blocker": row["blocker"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    })
    return task


def get_task(task_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a single task by its task_id."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        return _decode_task_row(row) if row else None
    finally:
        conn.close()


def get_tasks(workflow_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Fetch all tasks for a workflow."""
    return list_tasks(workflow_id=workflow_id, db_path=db_path)


def list_tasks(
    workflow_id: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Fetch tasks optionally filtered by workflow_id and/or status."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM tasks WHERE 1=1"
        params: List[Any] = []
        if workflow_id:
            query += " AND workflow_id = ?"
            params.append(workflow_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        cur = conn.execute(query, tuple(params))
        tasks = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            t = dict(payload)
            t.update({
                "task_id": row["task_id"],
                "workflow_id": row["workflow_id"],
                "node": row["node"],
                "stage": row["stage"],
                "agent": row["agent"],
                "status": row["status"],
                "stage_verdict": row["stage_verdict"],
                "stage_verdict_note": row["stage_verdict_note"],
                "pane_id": row["pane_id"],
                "goal": row["goal"],
                "blocker": row["blocker"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
            tasks.append(t)
        return tasks
    finally:
        conn.close()


def delete_task(task_id: str, db_path: Optional[Path] = None) -> bool:
    """Delete a task by its task_id."""
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        cur = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        deleted = cur.rowcount > 0
        conn.execute("COMMIT;")
        return deleted
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def save_steer(
    steer_dict: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert a steering item."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    sid = steer_dict.get("steer_id")
    tid = steer_dict.get("task_id")
    if not sid or not tid:
        raise ValueError("steer_id and task_id are required")

    wid = steer_dict.get("workflow_id")
    instruction = steer_dict.get("instruction", "")
    operator = steer_dict.get("operator", "human")
    urgent = 1 if steer_dict.get("urgent") else 0
    status = steer_dict.get("status", "pending")
    dispatched_at = steer_dict.get("dispatched_at")
    created_at = float(steer_dict.get("created_at") or time.time())

    payload = {k: v for k, v in steer_dict.items() if k not in {
        "steer_id", "task_id", "workflow_id", "instruction", "operator",
        "urgent", "status", "dispatched_at", "created_at"
    }}
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        conn.execute("""
            INSERT INTO steering_items (
                steer_id, task_id, workflow_id, instruction, operator,
                urgent, status, dispatched_at, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(steer_id) DO UPDATE SET
                task_id=excluded.task_id,
                workflow_id=excluded.workflow_id,
                instruction=excluded.instruction,
                operator=excluded.operator,
                urgent=excluded.urgent,
                status=excluded.status,
                dispatched_at=excluded.dispatched_at,
                payload_json=excluded.payload_json;
        """, (sid, tid, wid, instruction, operator, urgent, status, dispatched_at, created_at, payload_json))
    finally:
        if should_close:
            conn.close()


def get_steer(steer_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch a steering item by its steer_id."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM steering_items WHERE steer_id = ?", (steer_id,))
        row = cur.fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"] or "{}")
        item = dict(payload)
        item.update({
            "steer_id": row["steer_id"],
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "instruction": row["instruction"],
            "operator": row["operator"],
            "urgent": bool(row["urgent"]),
            "status": row["status"],
            "dispatched_at": row["dispatched_at"],
            "created_at": row["created_at"],
        })
        return item
    finally:
        conn.close()


def list_steers(
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List steering items optionally filtered by task_id and/or status."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM steering_items WHERE 1=1"
        params: List[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        cur = conn.execute(query, tuple(params))
        items = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            item = dict(payload)
            item.update({
                "steer_id": row["steer_id"],
                "task_id": row["task_id"],
                "workflow_id": row["workflow_id"],
                "instruction": row["instruction"],
                "operator": row["operator"],
                "urgent": bool(row["urgent"]),
                "status": row["status"],
                "dispatched_at": row["dispatched_at"],
                "created_at": row["created_at"],
            })
            items.append(item)
        return items
    finally:
        conn.close()


def update_steer_status(
    steer_id: str,
    status: str,
    dispatched_at: Optional[float] = None,
    db_path: Optional[Path] = None,
) -> bool:
    """Update status and dispatched_at for a steering item."""
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        if dispatched_at is not None:
            cur = conn.execute(
                "UPDATE steering_items SET status = ?, dispatched_at = ? WHERE steer_id = ?",
                (status, dispatched_at, steer_id),
            )
        else:
            cur = conn.execute(
                "UPDATE steering_items SET status = ? WHERE steer_id = ?",
                (status, steer_id),
            )
        updated = cur.rowcount > 0
        conn.execute("COMMIT;")
        return updated
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def record_steering_history(
    record: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record a steering audit/action entry."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True
    try:
        if should_close:
            conn.execute("BEGIN TRANSACTION;")
        now = float(record.get("timestamp") or time.time())
        action = record.get("action", "unknown")
        task_id = record.get("task_id")
        steer_id = record.get("steer_id")
        instruction = record.get("instruction")
        operator = record.get("operator")
        urgent = 1 if record.get("urgent") else 0
        reason = record.get("reason")
        payload = {k: v for k, v in record.items() if k not in {
            "action", "task_id", "steer_id", "instruction", "operator", "urgent", "reason", "timestamp"
        }}
        payload_json = json.dumps(payload, ensure_ascii=False)

        conn.execute("""
            INSERT INTO steering_history (
                action, task_id, steer_id, instruction, operator, urgent, reason, timestamp, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (action, task_id, steer_id, instruction, operator, urgent, reason, now, payload_json))
        record_event({
            "workflow_id": record.get("workflow_id"),
            "node_id": record.get("node_id") or record.get("node"),
            "task_id": task_id,
            "agent_id": record.get("agent_id") or record.get("agent"),
            "event_type": f"steering.{action}",
            "timestamp": now,
            "payload": {
                "steer_id": steer_id,
                "instruction": instruction,
                "operator": operator,
                "urgent": bool(urgent),
                "reason": reason,
                **payload,
            },
            "source": record.get("source") or "steering",
        }, conn=conn)
        if should_close:
            conn.execute("COMMIT;")
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()


def list_steering_history(
    task_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List steering history entries ordered chronologically."""
    conn = get_db_connection(db_path)
    try:
        if task_id:
            cur = conn.execute("SELECT * FROM steering_history WHERE task_id = ? ORDER BY timestamp ASC", (task_id,))
        else:
            cur = conn.execute("SELECT * FROM steering_history ORDER BY timestamp ASC")
        results = []
        for row in cur.fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            item = dict(payload)
            item.update({
                "action": row["action"],
                "task_id": row["task_id"],
                "steer_id": row["steer_id"],
                "instruction": row["instruction"],
                "operator": row["operator"],
                "urgent": bool(row["urgent"]),
                "reason": row["reason"],
                "timestamp": row["timestamp"],
            })
            results.append(item)
        return results
    finally:
        conn.close()


def _decode_intervention_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "intervention_id": row["intervention_id"],
        "identity_key": row["identity_key"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "task_id": row["task_id"],
        "evaluation_id": row["evaluation_id"],
        "decision_id": row["decision_id"],
        "action": row["action"],
        "reason": row["reason"],
        "finding_refs": json.loads(row["finding_refs_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "status": row["status"],
        "attempt": row["attempt"],
        "max_attempts": row["max_attempts"],
        "requested_at": row["requested_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "execution_owner": row["execution_owner"],
        "lease_until": row["lease_until"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
    }


def _record_intervention_event(conn: sqlite3.Connection, item: Dict[str, Any], event_type: str) -> None:
    record_event(
        {
            "workflow_id": item.get("workflow_id"),
            "task_id": item["task_id"],
            "event_type": event_type,
            "payload": item,
            "source": "intervention",
            "run_id": item["run_id"],
        },
        conn=conn,
    )


def create_intervention(
    intervention: Dict[str, Any], db_path: Optional[Path] = None,
    *, verification_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Create or return the canonical Intervention under one SQLite write lock.

    When ``verification_limit`` is supplied, the VERIFY budget check and the
    identity insert happen under this same SQLite write transaction.
    """
    from .intervention import ACTION_VERIFY, Intervention, STATUS_REQUESTED

    item = Intervention.from_mapping(intervention).to_mapping()
    if item["status"] != STATUS_REQUESTED:
        raise ValueError("new intervention must be requested")
    if not item["intervention_id"]:
        item["intervention_id"] = str(uuid.uuid4())
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        existing = conn.execute(
            "SELECT * FROM interventions WHERE identity_key = ?", (item["identity_key"],)
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT;")
            return _decode_intervention_row(existing)
        budget_exhausted = False
        if item["action"] == ACTION_VERIFY and verification_limit is not None:
            statuses = ("requested", "running", "completed", "failed")
            placeholders = ",".join("?" for _ in statuses)
            count = conn.execute(
                f"""SELECT COUNT(*) FROM interventions
                    WHERE run_id = ? AND task_id = ? AND action = ?
                      AND status IN ({placeholders})""",
                (item["run_id"], item["task_id"], ACTION_VERIFY, *statuses),
            ).fetchone()[0]
            budget_exhausted = int(count) >= int(verification_limit)
        inserted = conn.execute(
            """
            INSERT INTO interventions (
                intervention_id, identity_key, run_id, workflow_id, task_id,
                evaluation_id, decision_id, action, reason, finding_refs_json,
                evidence_refs_json, status, attempt, max_attempts, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO NOTHING
            """,
            (
                item["intervention_id"], item["identity_key"], item["run_id"],
                item["workflow_id"], item["task_id"], item["evaluation_id"],
                item["decision_id"], item["action"], item["reason"],
                json.dumps(item["finding_refs"], ensure_ascii=False),
                json.dumps(item["evidence_refs"], ensure_ascii=False),
                item["status"], item["attempt"], item["max_attempts"],
                item["requested_at"] or time.time(),
            ),
        ).rowcount
        row = conn.execute(
            "SELECT * FROM interventions WHERE identity_key = ?", (item["identity_key"],)
        ).fetchone()
        if row is None:
            raise RuntimeError("intervention insert did not produce a canonical row")
        canonical = _decode_intervention_row(row)
        if inserted:
            _record_intervention_event(conn, canonical, "intervention_requested")
        if budget_exhausted:
            error = {
                "code": "verification_budget_exhausted",
                "verification_count": int(count),
                "max_verifications": int(verification_limit),
            }
            finished_at = time.time()
            conn.execute(
                """UPDATE interventions SET status = ?, finished_at = ?, error_json = ?
                   WHERE intervention_id = ? AND status = ?""",
                (
                    "failed", finished_at, json.dumps(error, ensure_ascii=False),
                    canonical["intervention_id"], STATUS_REQUESTED,
                ),
            )
            canonical = _decode_intervention_row(conn.execute(
                "SELECT * FROM interventions WHERE intervention_id = ?",
                (canonical["intervention_id"],),
            ).fetchone())
            _record_intervention_event(conn, canonical, "intervention_failed")
        conn.execute("COMMIT;")
        return canonical
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_intervention(intervention_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        return _decode_intervention_row(row) if row else None
    finally:
        conn.close()


def list_interventions(
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    statuses: Optional[List[str]] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM interventions WHERE 1=1"
        params: List[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        if statuses:
            query += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        query += " ORDER BY requested_at DESC, intervention_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_intervention_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def claim_intervention(
    intervention_id: str,
    db_path: Optional[Path] = None,
    *,
    execution_owner: Optional[str] = None,
    lease_seconds: float = 300.0,
    recover_running: bool = False,
) -> Optional[Dict[str, Any]]:
    from .intervention import STATUS_REQUESTED, STATUS_RUNNING

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK;")
            return None
        now = time.time()
        if row["status"] == STATUS_REQUESTED:
            owner = execution_owner or str(uuid.uuid4())
            conn.execute(
                """UPDATE interventions
                   SET status = ?, started_at = ?, execution_owner = ?, lease_until = ?
                   WHERE intervention_id = ? AND status = ?""",
                (STATUS_RUNNING, now, owner, now + float(lease_seconds),
                 intervention_id, STATUS_REQUESTED),
            )
        elif (
            row["status"] == STATUS_RUNNING
            and recover_running
            and float(row["lease_until"] or 0.0) <= now
        ):
            owner = execution_owner or str(uuid.uuid4())
            conn.execute(
                """UPDATE interventions
                   SET execution_owner = ?, lease_until = ?
                   WHERE intervention_id = ? AND status = ?
                     AND COALESCE(lease_until, 0) <= ?""",
                (owner, now + float(lease_seconds), intervention_id,
                 STATUS_RUNNING, now),
            )
        else:
            conn.execute("ROLLBACK;")
            return None
        updated = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        item = _decode_intervention_row(updated)
        _record_intervention_event(conn, item, "intervention_started")
        conn.execute("COMMIT;")
        return item
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def complete_intervention(
    intervention_id: str, result: Dict[str, Any], db_path: Optional[Path] = None,
    *, execution_owner: Optional[str] = None,
) -> Dict[str, Any]:
    return _finish_intervention(intervention_id, "completed", result=result, db_path=db_path,
                                allowed=("running",), execution_owner=execution_owner)


def fail_intervention(
    intervention_id: str, error: Dict[str, Any], db_path: Optional[Path] = None,
    *, execution_owner: Optional[str] = None,
) -> Dict[str, Any]:
    return _finish_intervention(
        intervention_id, "failed", error=error, db_path=db_path,
        allowed=("requested", "running"), execution_owner=execution_owner,
    )


def _finish_intervention(
    intervention_id: str, status: str, *, result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None, db_path: Optional[Path], allowed: tuple,
    execution_owner: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"intervention '{intervention_id}' not found")
        if row["status"] not in allowed:
            raise ValueError(f"cannot finish intervention from {row['status']}")
        finished_at = time.time()
        if execution_owner is None:
            owner_clause = ""
            owner_params = ()
        else:
            owner_clause = " AND execution_owner = ?"
            owner_params = (execution_owner,)
        updated = conn.execute(
            f"""UPDATE interventions SET status = ?, finished_at = ?,
                   result_json = ?, error_json = ?, execution_owner = NULL,
                   lease_until = NULL
               WHERE intervention_id = ? AND status IN ({','.join('?' for _ in allowed)})
               {owner_clause}""",
            (
                status, finished_at,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                json.dumps(error, ensure_ascii=False) if error is not None else None,
                intervention_id, *allowed, *owner_params,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError(f"intervention '{intervention_id}' execution ownership changed")
        updated = conn.execute(
            "SELECT * FROM interventions WHERE intervention_id = ?", (intervention_id,)
        ).fetchone()
        item = _decode_intervention_row(updated)
        _record_intervention_event(
            conn, item, "intervention_completed" if status == "completed" else "intervention_failed"
        )
        conn.execute("COMMIT;")
        return item
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _decode_collaboration_row(row) -> Dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "identity_key": row["identity_key"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "from_task_id": row["from_task_id"],
        "to_task_id": row["to_task_id"],
        "from_agent": row["from_agent"] or "",
        "to_agent": row["to_agent"] or "",
        "from_pane_id": row["from_pane_id"],
        "to_pane_id": row["to_pane_id"],
        "type": row["type"],
        "summary": row["summary"] or "",
        "artifact_refs": json.loads(row["artifact_refs_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "context_refs": json.loads(row["context_refs_json"] or "[]"),
        "requires_response": bool(row["requires_response"]),
        "status": row["status"],
        "source_fact_id": row["source_fact_id"],
        "created_at": row["created_at"],
        "dispatched_at": row["dispatched_at"],
        "acknowledged_at": row["acknowledged_at"],
        "completed_at": row["completed_at"],
        "handoff_created_at": row["handoff_created_at"],
        "handoff_dispatched_at": row["handoff_dispatched_at"],
        "handoff_acknowledged_at": row["handoff_acknowledged_at"],
        "handoff_completed_at": row["handoff_completed_at"],
    }


def create_collaboration_event(
    event: Dict[str, Any], db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create or return the canonical CollaborationEvent (idempotent).

    Dedupe key is (run_id, from_task_id, to_task_id, type, source_fact_id).
    The losing concurrent writer returns the stored canonical row.
    """
    from .collaboration import create_handoff

    candidate = create_handoff(
        run_id=str(event.get("run_id") or ""),
        workflow_id=str(event.get("workflow_id") or ""),
        from_task_id=str(event.get("from_task_id") or ""),
        from_agent=str(event.get("from_agent") or ""),
        to_task_id=str(event.get("to_task_id") or ""),
        to_agent=str(event.get("to_agent") or ""),
        summary=str(event.get("summary") or ""),
        artifact_refs=list(event.get("artifact_refs") or []),
        evidence_refs=list(event.get("evidence_refs") or []),
        context_refs=list(event.get("context_refs") or []),
        source_fact_id=str(event.get("source_fact_id") or ""),
        event_type=str(event.get("type") or "HANDOFF"),
        requires_response=bool(event.get("requires_response", True)),
    )
    if event.get("from_pane_id"):
        candidate["from_pane_id"] = event.get("from_pane_id")
    if event.get("to_pane_id"):
        candidate["to_pane_id"] = event.get("to_pane_id")
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        existing = conn.execute(
            "SELECT * FROM collaboration_events WHERE identity_key = ?",
            (candidate["identity_key"],),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT;")
            return _decode_collaboration_row(existing)
        conn.execute(
            """
            INSERT INTO collaboration_events (
                event_id, identity_key, run_id, workflow_id,
                from_task_id, to_task_id, from_agent, to_agent,
                from_pane_id, to_pane_id, type, summary,
                artifact_refs_json, evidence_refs_json, context_refs_json,
                requires_response, status, source_fact_id,
                created_at, handoff_created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_key) DO NOTHING
            """,
            (
                candidate["event_id"], candidate["identity_key"],
                candidate["run_id"], candidate["workflow_id"],
                candidate["from_task_id"], candidate["to_task_id"],
                candidate["from_agent"], candidate["to_agent"],
                candidate["from_pane_id"], candidate["to_pane_id"],
                candidate["type"], candidate["summary"],
                json.dumps(candidate["artifact_refs"], ensure_ascii=False),
                json.dumps(candidate["evidence_refs"], ensure_ascii=False),
                json.dumps(candidate["context_refs"], ensure_ascii=False),
                1 if candidate["requires_response"] else 0,
                "created", candidate["source_fact_id"],
                candidate["created_at"], candidate["handoff_created_at"],
            ),
        )
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE identity_key = ?",
            (candidate["identity_key"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("collaboration insert did not produce a canonical row")
        conn.execute("COMMIT;")
        return _decode_collaboration_row(row)
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def get_collaboration_event(
    event_id: str, db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return _decode_collaboration_row(row) if row else None
    finally:
        conn.close()


def attach_working_context_ref(
    event_id: str, context_id: str, db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Attach a compiled target snapshot to a not-yet-dispatched handoff."""
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        event = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (str(event_id),),
        ).fetchone()
        context = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?", (str(context_id),),
        ).fetchone()
        if event is None or context is None:
            raise ValueError("handoff or working context does not exist")
        if str(context["task_id"]) != str(event["to_task_id"]):
            raise ValueError("working context task does not match handoff target")
        if str(context["workflow_id"] or "") != str(event["workflow_id"] or ""):
            raise ValueError("working context workflow does not match handoff")
        if str(context["run_scope"]) != str(event["run_id"]):
            raise ValueError("working context scope does not match handoff")
        context_payload = json.loads(context["payload_json"] or "{}")
        event_source_ref = f"collaboration:{event_id}"
        if event_source_ref not in set(context_payload.get("source_refs") or []):
            raise ValueError("working context does not contain the handoff fact")
        if event["status"] != "created":
            raise ValueError("cannot attach context to a dispatched handoff")
        refs = list(dict.fromkeys(
            [str(ref) for ref in json.loads(event["context_refs_json"] or "[]") if ref]
            + [str(context_id)]
        ))[:3]
        conn.execute(
            "UPDATE collaboration_events SET context_refs_json = ? WHERE event_id = ?",
            (json.dumps(refs, ensure_ascii=False), str(event_id)),
        )
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (str(event_id),),
        ).fetchone()
        conn.commit()
        return _decode_collaboration_row(row)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def list_collaboration_events(
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM collaboration_events WHERE 1=1"
        params: List[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if task_id is not None:
            query += " AND (from_task_id = ? OR to_task_id = ?)"
            params.extend([task_id, task_id])
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC, event_id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(0, int(limit)))
        return [_decode_collaboration_row(r) for r in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _transition_collaboration_event(
    event_id: str, to_status: str, time_field: Optional[str],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    from .collaboration import is_valid_transition

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        row = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"collaboration event '{event_id}' not found")
        old = row["status"]
        if old == to_status:
            conn.execute("COMMIT;")
            return _decode_collaboration_row(row)
        if not is_valid_transition(old, to_status):
            raise ValueError(f"cannot transition collaboration event from {old} to {to_status}")
        now = time.time()
        handoff_field = {
            "dispatched": "handoff_dispatched_at",
            "acknowledged": "handoff_acknowledged_at",
            "completed": "handoff_completed_at",
        }.get(to_status)
        if time_field and handoff_field:
            conn.execute(
                f"""UPDATE collaboration_events SET status = ?,
                    {time_field} = ?, {handoff_field} = ?
                    WHERE event_id = ?""",
                (to_status, now, now, event_id),
            )
        else:
            conn.execute(
                "UPDATE collaboration_events SET status = ? WHERE event_id = ?",
                (to_status, event_id),
            )
        updated = conn.execute(
            "SELECT * FROM collaboration_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        conn.execute("COMMIT;")
        return _decode_collaboration_row(updated)
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def mark_collaboration_dispatched(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "dispatched", "dispatched_at", db_path)


def mark_collaboration_acknowledged(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "acknowledged", "acknowledged_at", db_path)


def mark_collaboration_completed(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "completed", "completed_at", db_path)


def mark_collaboration_failed(event_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    return _transition_collaboration_event(event_id, "failed", None, db_path)


def record_event(
    event: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Append a canonical WorkflowEvent and return the normalized event."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        event_type = event.get("event_type")
        if not event_type:
            raise ValueError("event_type is required")

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")

        normalized = {
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": event_type,
            "timestamp": float(event["timestamp"]) if event.get("timestamp") is not None else time.time(),
            "payload": payload,
            "source": event.get("source") or "system",
            "run_id": event.get("run_id"),
        }

        cur = conn.execute("""
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            normalized["workflow_id"],
            normalized["node_id"],
            normalized["task_id"],
            normalized["agent_id"],
            normalized["event_type"],
            json.dumps(normalized["payload"], ensure_ascii=False),
            normalized["timestamp"],
            normalized["source"],
            normalized["run_id"],
        ))
        normalized["id"] = cur.lastrowid
        return normalized
    finally:
        if should_close:
            conn.close()


def list_events(
    workflow_id: Optional[str] = None,
    node_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    event_type: Optional[str] = None,
    source: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
    desc: bool = False,
) -> List[Dict[str, Any]]:
    """List WorkflowEvents in chronological order with optional filters.

    ``desc=True`` returns newest-first and makes ``limit`` select the newest
    N events (the default ASC + LIMIT would return the oldest N).
    """
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM events WHERE 1=1"
        params: List[Any] = []
        # Keep the legacy WorkflowEvent API behavior stable: trajectory rows
        # are queried through list_trajectory_events, unless explicitly asked
        # for source="trajectory".
        if source == "trajectory":
            query += " AND source = 'trajectory'"
        elif source is None:
            query += " AND (source IS NULL OR source != 'trajectory')"
        for column, value in (
            ("workflow_id", workflow_id),
            ("node_id", node_id),
            ("task_id", task_id),
            ("agent_id", agent_id),
            ("event_type", event_type),
            ("source", source),
        ):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY timestamp {direction}, id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))

        results = []
        for row in conn.execute(query, params).fetchall():
            results.append({
                "id": row["id"],
                "workflow_id": row["workflow_id"],
                "node_id": row["node_id"],
                "task_id": row["task_id"],
                "agent_id": row["agent_id"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "source": row["source"] or "unknown",
                "run_id": row["run_id"],
            })
        return results
    finally:
        conn.close()


def record_trajectory_event(
    event: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one trajectory event with a transaction-local run sequence.

    This is deliberately separate from the legacy WorkflowEvent API. Both use
    the same SQLite event store, but trajectory rows carry the run/sequence
    identity needed for deterministic replay.
    """
    run_id = event.get("run_id")
    event_type = event.get("event_type")
    if not run_id:
        raise ValueError("run_id is required")
    if not event_type:
        raise ValueError("event_type is required")

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        sequence = conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM events
            WHERE run_id = ? AND source = 'trajectory'
            """,
            (run_id,),
        ).fetchone()[0]
        timestamp = float(event["timestamp"]) if event.get("timestamp") is not None else time.time()
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source,
                run_id, sequence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'trajectory', ?, ?)
            """,
            (
                event.get("workflow_id"),
                event.get("node_id") or event.get("node"),
                event.get("task_id"),
                event.get("agent_id") or event.get("agent"),
                event_type,
                json.dumps(payload, ensure_ascii=False),
                timestamp,
                run_id,
                sequence,
            ),
        )
        conn.execute("COMMIT;")
        return {
            "id": cur.lastrowid,
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": event_type,
            "timestamp": timestamp,
            "source": "trajectory",
            "run_id": run_id,
            "sequence": sequence,
            "payload": payload,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def record_observation_receipt(
    event: Dict[str, Any],
    observation_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically append at most one trajectory receipt for an Observation."""
    if not observation_id:
        raise ValueError("observation_id is required")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    run_id = event.get("run_id")
    if not run_id:
        raise ValueError("run_id is required")

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        timestamp = float(event["timestamp"]) if event.get("timestamp") is not None else time.time()
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO observation_receipts
                (observation_id, run_id, task_id, workflow_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (observation_id, run_id, event.get("task_id"), event.get("workflow_id"), timestamp),
        ).rowcount
        if not inserted:
            conn.execute("COMMIT;")
            return None
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO events (
                workflow_id, node_id, task_id, agent_id,
                event_type, payload_json, timestamp, source, run_id, sequence
            ) VALUES (?, ?, ?, ?, 'observation_created', ?, ?, 'trajectory', ?, ?)
            """,
            (
                event.get("workflow_id"), event.get("node_id") or event.get("node"),
                event.get("task_id"), event.get("agent_id") or event.get("agent"),
                json.dumps(payload, ensure_ascii=False), timestamp, run_id, sequence,
            ),
        )
        conn.execute("COMMIT;")
        return {
            "id": cur.lastrowid,
            "workflow_id": event.get("workflow_id"),
            "node_id": event.get("node_id") or event.get("node"),
            "task_id": event.get("task_id"),
            "agent_id": event.get("agent_id") or event.get("agent"),
            "event_type": "observation_created",
            "timestamp": timestamp,
            "source": "trajectory",
            "run_id": run_id,
            "sequence": sequence,
            "payload": payload,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def list_trajectory_events(
    run_id: str,
    event_type: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: Optional[int] = None,
    desc: bool = False,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read trajectory rows with optional newest-first bounded selection."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM events WHERE run_id = ? AND source = 'trajectory'"
        params: List[Any] = [run_id]
        if event_type is not None:
            query += " AND event_type = ?"
            params.append(event_type)
        if task_id is not None:
            query += " AND task_id = ?"
            params.append(task_id)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY sequence {direction}, id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        results = []
        for row in conn.execute(query, params).fetchall():
            results.append({
                "id": row["id"],
                "workflow_id": row["workflow_id"],
                "node_id": row["node_id"],
                "task_id": row["task_id"],
                "agent_id": row["agent_id"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "source": row["source"],
                "run_id": row["run_id"],
                "sequence": row["sequence"],
                "payload": json.loads(row["payload_json"] or "{}"),
            })
        return results
    finally:
        conn.close()


def aggregate_run_metric_rows(run_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Return scalar, run-scoped metric facts without loading evidence content."""
    conn = get_db_connection(db_path)
    try:
        event_row = conn.execute(
            """
            SELECT COUNT(*) AS trajectory_events,
                   MIN(timestamp) AS started_at,
                   MAX(CASE WHEN event_type IN ('run_completed', 'run_failed') THEN timestamp END)
                       AS finished_at,
                   SUM(CASE WHEN event_type = 'run_completed' THEN 1 ELSE 0 END) AS run_completed,
                   SUM(CASE WHEN event_type = 'run_failed' THEN 1 ELSE 0 END) AS run_failed,
                   SUM(CASE WHEN event_type = 'verification_completed' THEN 1 ELSE 0 END)
                       AS verification_total,
                   SUM(CASE WHEN event_type = 'verification_completed'
                            THEN CASE WHEN json_valid(payload_json)
                                      THEN CASE WHEN json_extract(payload_json, '$.verification.passed') = 1
                                                THEN 1 ELSE 0 END
                                      ELSE 0 END
                            ELSE 0 END) AS verification_passed,
                   SUM(CASE WHEN event_type = 'verification_completed'
                            THEN CASE WHEN json_valid(payload_json)
                                      THEN CASE WHEN json_extract(payload_json, '$.verification.passed') = 0
                                                THEN 1 ELSE 0 END
                                      ELSE 0 END
                            ELSE 0 END) AS verification_failed
                   ,SUM(CASE WHEN event_type = 'task_started' THEN 1 ELSE 0 END) AS task_started
                   ,SUM(CASE WHEN event_type = 'artifact_created' THEN 1 ELSE 0 END) AS artifact_created
                   ,SUM(CASE WHEN event_type = 'agent_done' THEN 1 ELSE 0 END) AS agent_done
              FROM events
             WHERE run_id = ? AND source = 'trajectory'
            """,
            (run_id,),
        ).fetchone()
        identity = conn.execute(
            """
            SELECT
              (SELECT task_id FROM events
                WHERE run_id = ? AND source = 'trajectory' AND task_id IS NOT NULL
                ORDER BY sequence ASC, id ASC LIMIT 1) AS task_id,
              (SELECT workflow_id FROM events
                WHERE run_id = ? AND source = 'trajectory' AND workflow_id IS NOT NULL
                ORDER BY sequence ASC, id ASC LIMIT 1) AS workflow_id
            """,
            (run_id, run_id),
        ).fetchone()
        scope_row = conn.execute(
            """
            SELECT workflow_id, payload_json
              FROM tasks
             WHERE json_extract(payload_json, '$.run_id') = ?
             ORDER BY updated_at DESC, task_id ASC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        metric_scope = run_id
        if scope_row is not None:
            payload = json.loads(scope_row["payload_json"] or "{}")
            metric_scope = (
                payload.get("workflow_run_id")
                or payload.get("execution_id")
                or run_id
            )
        metric_workflow_id = (
            scope_row["workflow_id"] if scope_row is not None
            else (identity["workflow_id"] if identity is not None else None)
        )
        observations = conn.execute(
            """
            SELECT COUNT(*) AS observations_created,
                   COALESCE(SUM(size_bytes), 0) AS observation_bytes
              FROM observations
             WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        findings = conn.execute(
            "SELECT COUNT(*) AS findings_created FROM trajectory_findings WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        context_packs = conn.execute(
            "SELECT COUNT(*) AS context_packs_created FROM context_packs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        latest_context = conn.execute(
            """
            SELECT * FROM context_packs
             WHERE run_id = ?
             ORDER BY created_at DESC, rowid DESC
             LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        working_context_metrics = conn.execute(
            """
            SELECT COUNT(*) AS metric_count,
                   COALESCE(SUM(reused), 0) AS reused_count,
                   COALESCE(SUM(changed), 0) AS changed_count
              FROM working_context_metric_events
             WHERE (run_id = ? OR run_scope = ?)
               AND workflow_id IS ?
            """,
            (run_id, metric_scope, metric_workflow_id),
        ).fetchone()
        working_context_snapshots = conn.execute(
            """
            SELECT COUNT(*) AS snapshot_count
              FROM working_contexts
             WHERE (run_id = ? OR run_scope = ?)
               AND workflow_id IS ?
            """,
            (run_id, metric_scope, metric_workflow_id),
        ).fetchone()
        latest_working_context = conn.execute(
            """
            SELECT * FROM working_contexts
             WHERE (run_id = ? OR run_scope = ?)
               AND workflow_id IS ?
             ORDER BY source_watermark DESC, compiled_at DESC, rowid DESC
             LIMIT 1
            """,
            (run_id, metric_scope, metric_workflow_id),
        ).fetchone()
        working_context_compile_count = max(
            int(working_context_metrics["metric_count"] or 0),
            int(working_context_snapshots["snapshot_count"] or 0),
        )
        working_context_reuse_count = int(working_context_metrics["reused_count"] or 0)
        working_context_change_count = int(working_context_metrics["changed_count"] or 0)
        return {
            "trajectory_events": int(event_row["trajectory_events"] or 0),
            "started_at": event_row["started_at"],
            "finished_at": event_row["finished_at"],
            "run_completed": int(event_row["run_completed"] or 0),
            "run_failed": int(event_row["run_failed"] or 0),
            "verification_total": int(event_row["verification_total"] or 0),
            "verification_passed": int(event_row["verification_passed"] or 0),
            "verification_failed": int(event_row["verification_failed"] or 0),
            "task_started": int(event_row["task_started"] or 0),
            "artifact_created": int(event_row["artifact_created"] or 0),
            "agent_done": int(event_row["agent_done"] or 0),
            "task_id": identity["task_id"] if identity else None,
            "workflow_id": metric_workflow_id,
            "observations_created": int(observations["observations_created"] or 0),
            "observation_bytes": int(observations["observation_bytes"] or 0),
            "findings_created": int(findings["findings_created"] or 0),
            "context_packs_created": int(context_packs["context_packs_created"] or 0),
            "working_context_compiles": working_context_compile_count,
            "working_context_reused": working_context_reuse_count,
            "working_context_changed": working_context_change_count,
            "latest_working_context": dict(latest_working_context) if latest_working_context else None,
            "latest_context": dict(latest_context) if latest_context else None,
        }
    finally:
        conn.close()


def latest_trajectory_sequence(run_id: str, db_path: Optional[Path] = None) -> int:
    """Read only the run watermark without loading its event history."""
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()
        return int(row["sequence"] or 0)
    finally:
        conn.close()


def trajectory_event_exists(event_id: str, run_id: Optional[str] = None, db_path: Optional[Path] = None) -> bool:
    """Check one event identity without loading the trajectory window."""
    try:
        row_id = int(str(event_id).removeprefix("evt_"))
    except ValueError:
        return False
    conn = get_db_connection(db_path)
    try:
        query = "SELECT 1 FROM events WHERE id = ? AND source = 'trajectory'"
        params: List[Any] = [row_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        return conn.execute(query, params).fetchone() is not None
    finally:
        conn.close()


def artifact_ref_exists(ref: str, run_id: str, db_path: Optional[Path] = None) -> bool:
    """Check an artifact reference in trajectory payloads without full history materialization."""
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT payload_json FROM events
               WHERE run_id = ? AND source = 'trajectory'
                 AND event_type = 'artifact_created'
                 AND (payload_json LIKE '%"ref"%' OR payload_json LIKE '%"path"%')""",
            (run_id,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            artifact = payload.get("artifact") or {}
            if str(artifact.get("ref") or artifact.get("path") or "") == str(ref):
                return True
        return False
    finally:
        conn.close()


def _decode_finding_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "finding_id": row["finding_id"],
        "finding_key": row["finding_key"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "node": row["node"],
        "agent": row["agent"],
        "agent_session_id": row["agent_session_id"],
        "finding_type": row["finding_type"],
        "severity": row["severity"],
        "status": row["status"] or "open",
        "summary": row["summary"],
        "suspected_cause": row["suspected_cause"],
        "recommended_action": row["recommended_action"],
        "confidence": row["confidence"],
        "evidence": json.loads(row["evidence_json"] or "[]"),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }


def upsert_trajectory_finding(
    finding: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Record/refresh one analysis finding and return the canonical persisted row.

    Findings are deliberately separated from trajectory events: the events
    ledger records what happened, this table records what HAFlow suspects it
    means. ``finding_key`` is the dedup contract (UNIQUE):

    - first observation inserts the row;
    - later observations of the SAME episode (same key) refresh the analysis
      fields in place (severity/summary/evidence/... escalating as the chain
      grows) while ``finding_id``/``created_at`` stay canonical;
    - a concurrent observer that lost the insert race re-reads and returns the
      canonical row, so every caller converges on one ``finding_id``;
    - a lower-severity observation never downgrades a stored row.

    Returns None only when the row cannot be read back at all.
    """
    finding_key = finding.get("finding_key")
    finding_id = finding.get("finding_id")
    run_id = finding.get("run_id")
    finding_type = finding.get("finding_type")
    if not finding_key or not finding_id or not run_id or not finding_type:
        raise ValueError("finding_id, finding_key, run_id and finding_type are required")
    metadata = dict(finding.get("metadata") or {})
    for relation_key in ("supersedes", "superseded_by"):
        if finding.get(relation_key) is not None and relation_key not in metadata:
            metadata[relation_key] = finding[relation_key]

    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO trajectory_findings (
                finding_id, finding_key, run_id, task_id, workflow_id, node,
                agent, agent_session_id, finding_type, severity, status,
                summary, suspected_cause, recommended_action, confidence,
                evidence_json, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(finding_key) DO UPDATE SET
                task_id = excluded.task_id,
                workflow_id = excluded.workflow_id,
                node = excluded.node,
                agent = excluded.agent,
                agent_session_id = excluded.agent_session_id,
                severity = excluded.severity,
                status = excluded.status,
                summary = excluded.summary,
                suspected_cause = excluded.suspected_cause,
                recommended_action = excluded.recommended_action,
                confidence = excluded.confidence,
                evidence_json = excluded.evidence_json,
                metadata_json = excluded.metadata_json
            WHERE CASE excluded.severity
                      WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END
                  >= CASE trajectory_findings.severity
                      WHEN 'critical' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END;
            """,
            (
                finding_id,
                finding_key,
                run_id,
                finding.get("task_id"),
                finding.get("workflow_id"),
                finding.get("node"),
                finding.get("agent"),
                finding.get("agent_session_id"),
                finding_type,
                finding.get("severity") or "warning",
                finding.get("status") or "open",
                finding.get("summary"),
                finding.get("suspected_cause"),
                finding.get("recommended_action"),
                float(finding.get("confidence") or 0.0),
                json.dumps(finding.get("evidence") or [], ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                float(finding["created_at"]) if finding.get("created_at") is not None else time.time(),
            ),
        )
        # Whether this call inserted, updated, or lost a race, the canonical
        # row is the single answer every caller must return.
        row = conn.execute(
            "SELECT * FROM trajectory_findings WHERE finding_key = ?", (finding_key,)
        ).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        conn.close()


def get_trajectory_finding(
    finding_key: str,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one finding by its dedup key (None when absent)."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True
    try:
        row = conn.execute(
            "SELECT * FROM trajectory_findings WHERE finding_key = ?", (finding_key,)
        ).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()


def get_trajectory_finding_by_id(
    finding_id: str,
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch one finding by its public identity for reference verification."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM trajectory_findings WHERE finding_id = ?"
        params: List[Any] = [finding_id]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        row = conn.execute(query, params).fetchone()
        return _decode_finding_row(row) if row is not None else None
    finally:
        conn.close()


def list_trajectory_findings(
    run_id: Optional[str] = None,
    finding_type: Optional[str] = None,
    limit: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List analysis findings in stable creation order."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM trajectory_findings WHERE 1=1"
        params: List[Any] = []
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        if finding_type is not None:
            query += " AND finding_type = ?"
            params.append(finding_type)
        query += " ORDER BY created_at ASC, rowid ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_finding_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def list_trajectory_findings_bounded(
    run_id: str,
    limit: int,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return the most important recent findings with SQL-side bounding."""
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM trajectory_findings
               WHERE run_id = ?
               ORDER BY CASE severity
                          WHEN 'critical' THEN 3
                          WHEN 'warning' THEN 2
                          ELSE 1
                        END DESC,
                        created_at DESC, rowid DESC
               LIMIT ?""",
            (run_id, int(limit)),
        ).fetchall()
        return [_decode_finding_row(row) for row in rows]
    finally:
        conn.close()


def _finding_source_fingerprint(findings: List[Dict[str, Any]]) -> str:
    payload = json.dumps(findings, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _observation_source_fingerprint(observations: List[Dict[str, Any]]) -> str:
    payload = json.dumps(observations, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compact_observation_metadata(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "observation_id": row["observation_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "sha256": row["sha256"],
            "excerpt": row.get("excerpt"),
        }
        for row in rows
    ]


def _context_source_version_in_conn(
    conn: sqlite3.Connection,
    run_id: str,
    task_id: Optional[str],
    finding_limit: int,
    observation_limit: int = 20,
) -> Dict[str, Any]:
    sequence_row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
        (run_id,),
    ).fetchone()
    task_updated_at = None
    if task_id:
        task_row = conn.execute(
            "SELECT updated_at FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if task_row is not None:
            task_updated_at = task_row["updated_at"]
    finding_rows = conn.execute(
        """SELECT * FROM trajectory_findings
           WHERE run_id = ?
           ORDER BY CASE severity
                      WHEN 'critical' THEN 3
                      WHEN 'warning' THEN 2
                      ELSE 1
                    END DESC,
                    created_at DESC, rowid DESC
           LIMIT ?""",
        (run_id, int(finding_limit)),
    ).fetchall()
    findings = [_decode_finding_row(row) for row in finding_rows]
    observation_rows = conn.execute(
        """SELECT * FROM observations
           WHERE run_id = ?
           ORDER BY created_at DESC, observation_id DESC
           LIMIT ?""",
        (run_id, int(observation_limit)),
    ).fetchall()
    observations = _compact_observation_metadata(
        [_decode_observation_row(row) for row in observation_rows]
    )
    observations.reverse()
    return {
        "task_id": task_id,
        "trajectory_sequence": int(sequence_row["sequence"] or 0),
        "task_updated_at": task_updated_at,
        "finding_fingerprint": _finding_source_fingerprint(findings),
        "finding_limit": int(finding_limit),
        "observation_fingerprint": _observation_source_fingerprint(observations),
        "observation_limit": int(observation_limit),
    }


def get_context_source_version(
    run_id: str,
    task_id: Optional[str],
    finding_limit: int,
    observation_limit: int = 20,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read the compact source watermark/revisions from one SQLite snapshot."""
    conn = get_db_connection(db_path)
    try:
        return _context_source_version_in_conn(conn, run_id, task_id, finding_limit, observation_limit)
    finally:
        conn.close()


def _decode_observation_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "observation_id": row["observation_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "content_ref": row["content_ref"],
        "media_type": row["media_type"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "excerpt": row["excerpt"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }


def insert_observation(
    observation: Dict[str, Any],
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Insert immutable Observation metadata and return the inserted row."""
    should_close = conn is None
    conn = conn or get_db_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO observations (
                observation_id, run_id, task_id, workflow_id, source_type,
                source_ref, content_ref, media_type, size_bytes, sha256,
                excerpt, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation["observation_id"],
                observation["run_id"],
                observation.get("task_id"),
                observation.get("workflow_id"),
                observation["source_type"],
                observation["source_ref"],
                observation["content_ref"],
                observation["media_type"],
                int(observation["size_bytes"]),
                observation["sha256"],
                observation.get("excerpt"),
                json.dumps(observation.get("metadata") or {}, ensure_ascii=False),
                float(observation["created_at"]),
            ),
        )
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?",
            (observation["observation_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError("observation insert was not readable")
        return _decode_observation_row(row)
    finally:
        if should_close:
            conn.close()


def get_observation(
    observation_id: str,
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Read one immutable Observation metadata row."""
    should_close = conn is None
    conn = conn or get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return _decode_observation_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()


def find_observation_by_dedup(
    run_id: str,
    source_type: str,
    source_ref: str,
    sha256: str,
    *,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Dict[str, Any]]:
    """Find the canonical row for an Observation deduplication key."""
    should_close = conn is None
    conn = conn or get_db_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT * FROM observations
            WHERE run_id = ? AND source_type = ? AND source_ref = ? AND sha256 = ?
            """,
            (run_id, source_type, source_ref, sha256),
        ).fetchone()
        return _decode_observation_row(row) if row is not None else None
    finally:
        if should_close:
            conn.close()


def list_observations(
    *,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: Optional[int] = None,
    desc: bool = False,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """List Observation metadata with access-pattern-aligned filters."""
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM observations WHERE 1=1"
        params: List[Any] = []
        for column, value in (("run_id", run_id), ("task_id", task_id), ("source_type", source_type)):
            if value is not None:
                query += f" AND {column} = ?"
                params.append(value)
        direction = "DESC" if desc else "ASC"
        query += f" ORDER BY created_at {direction}, observation_id {direction}"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        return [_decode_observation_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def _decode_context_pack_row(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "context_id": row["context_id"],
        "run_id": row["run_id"],
        "task_id": row["task_id"],
        "workflow_id": row["workflow_id"],
        "goal": row["goal"],
        "current_state": json.loads(row["current_state_json"] or "{}"),
        "completed": json.loads(row["completed_json"] or "[]"),
        "verified_facts": json.loads(row["verified_facts_json"] or "[]"),
        "important_findings": json.loads(row["important_findings_json"] or "[]"),
        "evidence_refs": json.loads(row["evidence_refs_json"] or "[]"),
        "artifact_refs": json.loads(row["artifact_refs_json"] or "[]"),
        "open_issues": json.loads(row["open_issues_json"] or "[]"),
        "next_focus": json.loads(row["next_focus_json"] or "[]"),
        "source_event_sequence": int(row["source_event_sequence"] or 0),
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": float(row["created_at"]),
    }


def _trajectory_rows_in_conn(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    event_type: Optional[str] = None,
    limit: int,
    desc: bool = True,
) -> List[Dict[str, Any]]:
    query = "SELECT * FROM events WHERE run_id = ? AND source = 'trajectory'"
    params: List[Any] = [run_id]
    if event_type is not None:
        query += " AND event_type = ?"
        params.append(event_type)
    direction = "DESC" if desc else "ASC"
    query += f" ORDER BY sequence {direction}, id {direction} LIMIT ?"
    params.append(int(limit))
    return [
        {
            "id": row["id"],
            "workflow_id": row["workflow_id"],
            "node_id": row["node_id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "event_type": row["event_type"],
            "timestamp": row["timestamp"],
            "source": row["source"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "payload": json.loads(row["payload_json"] or "{}"),
        }
        for row in conn.execute(query, tuple(params)).fetchall()
    ]


def _task_matches_run(task: Optional[Dict[str, Any]], run_id: str) -> bool:
    if not task:
        return False
    try:
        from herdr.trajectory import run_id_for_task
        return str(run_id_for_task(task)) == str(run_id)
    except Exception:
        return False


def read_context_compact_snapshot(
    run_id: str,
    *,
    task: Optional[Dict[str, Any]] = None,
    max_recent_events: int = 100,
    max_findings: int = 10,
    max_observations: int = 20,
    verification_limit: int = 10,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read all compact inputs and their source version in one SQLite snapshot."""
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN;")
        recent_rows = _trajectory_rows_in_conn(
            conn, run_id, limit=max_recent_events, desc=True,
        )
        verification_rows = _trajectory_rows_in_conn(
            conn, run_id, event_type="verification_completed", limit=verification_limit, desc=True,
        )
        all_rows = recent_rows + verification_rows
        requested_task_id = (task or {}).get("task_id")
        if requested_task_id is None:
            requested_task_id = next(
                (row.get("task_id") for row in reversed(all_rows) if row.get("task_id")),
                None,
            )
        selected_task = None
        if requested_task_id:
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (str(requested_task_id),)
            ).fetchone()
            if task_row is not None:
                candidate_task = _decode_task_row(task_row)
                if _task_matches_run(candidate_task, run_id):
                    selected_task = candidate_task
            elif _task_matches_run(task, run_id):
                selected_task = task
        elif _task_matches_run(task, run_id):
            selected_task = task

        finding_rows = conn.execute(
            """SELECT * FROM trajectory_findings
               WHERE run_id = ?
               ORDER BY CASE severity
                          WHEN 'critical' THEN 3
                          WHEN 'warning' THEN 2
                          ELSE 1
                        END DESC,
                        created_at DESC, rowid DESC
               LIMIT ?""",
            (run_id, int(max_findings)),
        ).fetchall()
        findings = [_decode_finding_row(row) for row in finding_rows]
        observation_rows = conn.execute(
            """SELECT * FROM observations
               WHERE run_id = ?
               ORDER BY created_at DESC, observation_id DESC
               LIMIT ?""",
            (run_id, int(max_observations)),
        ).fetchall()
        observations = _compact_observation_metadata(
            [_decode_observation_row(row) for row in observation_rows]
        )
        observations.reverse()
        sequence_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events WHERE run_id = ? AND source = 'trajectory'",
            (run_id,),
        ).fetchone()
        source_sequence = int(sequence_row["sequence"] or 0)
        source_version = _context_source_version_in_conn(
            conn,
            run_id,
            (selected_task or {}).get("task_id"),
            max_findings,
            max_observations,
        )
        source_version["trajectory_sequence"] = source_sequence
        latest_row = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        conn.commit()
        return {
            "source_sequence": source_sequence,
            "recent_rows": recent_rows,
            "verification_rows": verification_rows,
            "task": selected_task,
            "findings": findings,
            "observations": observations,
            "source_version": source_version,
            "latest": _decode_context_pack_row(latest_row) if latest_row is not None else None,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_context_pack(
    context_pack: Dict[str, Any],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append one ContextPack, returning the existing pack on exact fingerprint dedup."""
    required = ("context_id", "run_id")
    if any(not context_pack.get(key) for key in required):
        raise ValueError("context_id and run_id are required")
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        fingerprint = (context_pack.get("metadata") or {}).get("context_source_fingerprint")
        source_version = (context_pack.get("metadata") or {}).get("context_source_version")
        source_version_valid = False
        latest = conn.execute(
            """SELECT * FROM context_packs
               WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (context_pack["run_id"],),
        ).fetchone()
        if isinstance(source_version, dict):
            current_source_version = _context_source_version_in_conn(
                conn,
                context_pack["run_id"],
                source_version.get("task_id"),
                int(source_version.get("finding_limit") or 0),
                int(source_version.get("observation_limit") or 0),
            )
            if current_source_version != source_version:
                conn.commit()
                if latest is not None:
                    return _decode_context_pack_row(latest)
                raise ValueError("stale ContextPack source version")
            source_version_valid = True
        if latest is not None:
            existing_metadata = json.loads(latest["metadata_json"] or "{}")
            if fingerprint and existing_metadata.get("context_source_fingerprint") == fingerprint:
                # Fingerprint match alone must not preserve a corrupt task_status.
                # The candidate is built by the current cleanup logic (single
                # current status or none); only dedup when latest carries the
                # same task_status values.
                try:
                    latest_facts = json.loads(latest["verified_facts_json"] or "[]")
                except Exception:
                    latest_facts = []
                candidate_facts = context_pack.get("verified_facts") or []
                def _status_values(facts: Any) -> List[Any]:
                    values = []
                    if isinstance(facts, list):
                        for fact in facts:
                            if isinstance(fact, dict) and fact.get("fact_type") == "task_status":
                                values.append(fact.get("status"))
                    return values
                if _status_values(latest_facts) == _status_values(candidate_facts):
                    conn.commit()
                    return _decode_context_pack_row(latest)
            # Compact requests may finish out of order.  The request start
            # timestamp is the snapshot's logical ordering key, so a late
            # older request must never become the latest snapshot.  A later
            # A->B->A request still has a newer timestamp and is appended.
            candidate_created_at = float(context_pack.get("created_at") or time.time())
            latest_created_at = float(latest["created_at"] or 0.0)
            if not source_version_valid and latest_created_at > candidate_created_at:
                conn.commit()
                return _decode_context_pack_row(latest)
        insert_created_at = float(context_pack.get("created_at") or time.time())
        if source_version_valid and latest is not None:
            # Source-version validation, rather than request start time, is
            # authoritative for a candidate that was read after waiting for
            # the writer lock.  Make that accepted snapshot the SQL latest.
            insert_created_at = max(insert_created_at, float(latest["created_at"] or 0.0) + 1e-9)
        conn.execute(
            """INSERT INTO context_packs (
                context_id, run_id, task_id, workflow_id, goal,
                current_state_json, completed_json, verified_facts_json,
                important_findings_json, evidence_refs_json, artifact_refs_json,
                open_issues_json, next_focus_json, source_event_sequence,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                context_pack["context_id"], context_pack["run_id"],
                context_pack.get("task_id"), context_pack.get("workflow_id"), context_pack.get("goal"),
                json.dumps(context_pack.get("current_state") or {}, ensure_ascii=False),
                json.dumps(context_pack.get("completed") or [], ensure_ascii=False),
                json.dumps(context_pack.get("verified_facts") or [], ensure_ascii=False),
                json.dumps(context_pack.get("important_findings") or [], ensure_ascii=False),
                json.dumps(context_pack.get("evidence_refs") or [], ensure_ascii=False),
                json.dumps(context_pack.get("artifact_refs") or [], ensure_ascii=False),
                json.dumps(context_pack.get("open_issues") or [], ensure_ascii=False),
                json.dumps(context_pack.get("next_focus") or [], ensure_ascii=False),
                int(context_pack.get("source_event_sequence") or 0),
                json.dumps(context_pack.get("metadata") or {}, ensure_ascii=False),
                insert_created_at,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM context_packs WHERE context_id = ?", (context_pack["context_id"],)
        ).fetchone()
        if row is None:
            raise RuntimeError("context pack insert was not readable")
        return _decode_context_pack_row(row)
    finally:
        conn.close()


def get_context_pack(
    context_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM context_packs WHERE context_id = ?", (context_id,)).fetchone()
        return _decode_context_pack_row(row) if row is not None else None
    finally:
        conn.close()


def get_latest_context_pack(
    run_id: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""", (run_id,)
        ).fetchone()
        return _decode_context_pack_row(row) if row is not None else None
    finally:
        conn.close()


def list_context_packs(
    run_id: str,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM context_packs WHERE run_id = ?
               ORDER BY created_at ASC, rowid ASC""", (run_id,)
        ).fetchall()
        return [_decode_context_pack_row(row) for row in rows]
    finally:
        conn.close()


def _decode_working_context_row(row: sqlite3.Row) -> Dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    metrics = json.loads(row["metrics_json"] or "{}")
    payload.update({
        "context_id": row["context_id"],
        "run_scope": row["run_scope"],
        "run_id": row["run_id"],
        "workflow_id": row["workflow_id"],
        "task_id": row["task_id"],
        "node_id": row["node_id"],
        "agent_role": row["agent_role"],
        "context_fingerprint": row["context_fingerprint"],
        "source_version": row["source_version"],
        "source_watermark": int(row["source_watermark"] or 0),
        "metrics": metrics,
        "compiled_at": row["compiled_at"],
    })
    return payload


def _valid_context_source_ref(value: Any) -> bool:
    if not isinstance(value, str) or ":" not in value:
        return False
    prefix, rest = value.split(":", 1)
    return prefix in {
        "task", "workflow", "trajectory", "finding", "observation",
        "collaboration", "eval", "policy", "evidence", "artifact",
    } and bool(rest.strip()) and not any(char.isspace() for char in rest)


def _validate_context_source_existence(
    conn: sqlite3.Connection, context: Dict[str, Any],
) -> None:
    refs = set()
    item_bindings: Dict[str, List[Tuple[Optional[str], Optional[str]]]] = {}
    refs.update(str(ref) for ref in (context.get("source_refs") or []))
    refs.update(str(ref) for ref in (context.get("goal_source_ref"), context.get("next_action_source_ref")) if ref)
    refs.update(str(ref) for ref in (context.get("current_state_refs") or {}).values())
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in context.get(field_name) or []:
            item_ref = str(item.get("source_ref") or "")
            refs.add(item_ref)
            item_bindings.setdefault(item_ref, []).append((
                str(item.get("source_task")) if item.get("source_task") else None,
                str(item.get("source_run")) if item.get("source_run") else None,
            ))
            for evidence_ref in item.get("evidence_refs") or []:
                refs.add(str(evidence_ref))

    from herdr.trajectory import run_id_for_task

    taskless_allowed_runs: set[str] = set()
    def task_record(task_id: str):
        row = conn.execute(
            "SELECT task_id, workflow_id, node, stage, agent, payload_json FROM tasks WHERE task_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        try:
            effective_run_id = str(run_id_for_task({**payload, "task_id": task_id}))
        except ValueError:
            effective_run_id = ""
        return {
            "task_id": task_id,
            "workflow_id": row["workflow_id"] or payload.get("workflow_id"),
            "node": row["node"] or payload.get("node"),
            "stage": row["stage"] or payload.get("stage"),
            "agent": row["agent"] or payload.get("agent"),
            "run_id": effective_run_id,
            "agent_role": payload.get("agent_role"),
            "scope": payload.get("workflow_run_id") or payload.get("execution_id") or row["workflow_id"],
        }

    target_record = task_record(str(context.get("task_id") or ""))
    if (
        target_record
        and str(target_record.get("workflow_id") or "") == str(context.get("workflow_id") or "")
        and str(target_record.get("scope") or "") == str(context.get("run_scope") or "")
    ):
        taskless_allowed_runs.add(str(target_record.get("run_id") or ""))
    if str(context.get("run_scope") or "") != str(context.get("workflow_id") or ""):
        for scoped_task_id in (
            row["task_id"] for row in conn.execute(
                "SELECT task_id FROM tasks WHERE workflow_id = ?",
                (str(context.get("workflow_id") or ""),),
            ).fetchall()
        ):
            scoped_task = task_record(str(scoped_task_id))
            if (
                scoped_task
                and str(scoped_task.get("scope") or "") == str(context.get("run_scope") or "")
            ):
                taskless_allowed_runs.add(str(scoped_task.get("run_id") or ""))

    def record_scope(record):
        task_id = record.get("task_id")
        if task_id:
            task = task_record(str(task_id))
            if task is None:
                return False
            if (
                str(context.get("run_scope") or "") == str(context.get("workflow_id") or "")
                and str(task_id) != str(context.get("task_id") or "")
                and str(task_id) not in verified_handoff_tasks
            ):
                return False
            if str(task.get("workflow_id") or "") != str(context.get("workflow_id") or ""):
                return False
            if str(task.get("scope") or "") != str(context.get("run_scope") or ""):
                return False
            if record.get("run_id") and str(task.get("run_id") or "") != str(record["run_id"]):
                return False
            return True
        if not record.get("workflow_id"):
            return False
        run_id = str(record.get("run_id") or "")
        if not run_id:
            return False
        if taskless_allowed_runs:
            return run_id in taskless_allowed_runs
        return False

    def fetch_record(prefix: str, object_id: str):
        if prefix == "task":
            return task_record(object_id)
        if prefix == "workflow":
            row = conn.execute(
                "SELECT workflow_id FROM workflows WHERE workflow_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
            return {"workflow_id": row["workflow_id"]} if row is not None else None
        if prefix == "observation":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM observations WHERE observation_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        elif prefix == "finding":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM trajectory_findings WHERE finding_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        elif prefix == "eval":
            row = conn.execute(
                "SELECT run_id, task_id, workflow_id FROM eval_results WHERE eval_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        elif prefix == "trajectory":
            event_number = object_id.removeprefix("evt_")
            try:
                row = conn.execute(
                    "SELECT run_id, task_id, workflow_id FROM events WHERE id = ? AND source = 'trajectory' LIMIT 1",
                    (int(event_number),),
                ).fetchone()
            except ValueError:
                row = conn.execute(
                    "SELECT run_id, task_id, workflow_id FROM events WHERE source = 'trajectory' AND json_extract(payload_json, '$.event_id') = ? LIMIT 1",
                    (object_id,),
                ).fetchone()
        elif prefix == "collaboration":
            row = conn.execute(
                "SELECT run_id, workflow_id, from_task_id, to_task_id, type, status FROM collaboration_events WHERE event_id = ? LIMIT 1",
                (object_id,),
            ).fetchone()
        else:
            return {"synthetic": True}
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    verified_handoff_tasks: set[str] = set()
    if str(context.get("run_scope") or "") == str(context.get("workflow_id") or ""):
        target_task_id = str(context.get("task_id") or "")
        for ref in refs:
            if not ref.startswith("collaboration:"):
                continue
            object_id = ref.split(":", 1)[1].split(":", 1)[0]
            handoff = fetch_record("collaboration", object_id)
            if not handoff or str(handoff.get("type") or "").upper() != "HANDOFF":
                continue
            endpoints = {
                str(value) for value in (
                    handoff.get("from_task_id"), handoff.get("to_task_id")
                ) if value
            }
            if target_task_id not in endpoints:
                continue
            verified_handoff_tasks.update(endpoints - {target_task_id})

    for ref in sorted(refs):
        if not ref:
            continue
        if ref.startswith("policy:"):
            if ref in item_bindings:
                raise ValueError(f"working context item cannot use a policy source: {ref}")
            continue
        if ref.startswith(("artifact:", "evidence:")):
            raise ValueError(f"working context source reference is not a stored source: {ref}")
        prefix, remainder = ref.split(":", 1)
        object_id = remainder.split(":", 1)[0]
        if not object_id:
            raise ValueError("working context source reference has no object id")
        record = fetch_record(prefix, object_id)
        if record is None:
            raise ValueError(f"working context source reference does not exist: {ref}")
        if record.get("workflow_id") and str(record["workflow_id"]) != str(context.get("workflow_id") or ""):
            raise ValueError(f"working context source reference crosses workflow: {ref}")
        bound_task = record.get("task_id") or record.get("to_task_id")
        bound_run = record.get("run_id")
        for source_task, source_run in item_bindings.get(ref, []):
            if bound_task:
                if str(source_task or "") != str(bound_task):
                    raise ValueError(f"working context item source_task does not match reference: {ref}")
            elif source_task:
                raise ValueError(f"working context taskless item has a source_task: {ref}")
            if bound_run and str(source_run or "") != str(bound_run):
                raise ValueError(f"working context item source_run does not match reference: {ref}")
        referenced_tasks = [bound_task] if bound_task else []
        if prefix == "collaboration":
            referenced_tasks.extend(
                task_id for task_id in (record.get("from_task_id"), record.get("to_task_id")) if task_id
            )
        for referenced_task in referenced_tasks:
            task = task_record(str(referenced_task))
            if (
                task
                and str(task.get("workflow_id") or "") == str(context.get("workflow_id") or "")
                and str(task.get("scope") or "") == str(context.get("run_scope") or "")
                and (
                    str(context.get("run_scope") or "") != str(context.get("workflow_id") or "")
                    or str(referenced_task) == str(context.get("task_id") or "")
                    or str(referenced_task) in verified_handoff_tasks
                )
            ):
                taskless_allowed_runs.add(str(task.get("run_id") or ""))
        if prefix == "collaboration":
            if str(record.get("run_id") or "") != str(context.get("run_scope") or ""):
                raise ValueError(f"working context collaboration scope mismatch: {ref}")
            for task_id in (record.get("from_task_id"), record.get("to_task_id")):
                if not record_scope({"task_id": task_id}):
                    raise ValueError(f"working context collaboration task scope mismatch: {ref}")
        elif prefix != "workflow" and not record_scope(record):
            raise ValueError(f"working context source reference crosses run scope: {ref}")

    head = conn.execute(
        """
        SELECT 1 FROM working_context_source_heads
        WHERE run_scope = ? AND workflow_id = ?
        LIMIT 1
        """,
        (
            str(context.get("run_scope") or ""),
            str(context.get("workflow_id") or ""),
        ),
    ).fetchone()
    if head is None:
        raise ValueError("working context source revision is not registered")
    target_task = task_record(str(context.get("task_id") or ""))
    if target_task is None or str(target_task.get("workflow_id") or "") != str(context.get("workflow_id") or ""):
        raise ValueError("working context target task is not in the requested workflow")
    if str(target_task.get("scope") or "") != str(context.get("run_scope") or ""):
        raise ValueError("working context target task scope does not match run_scope")
    if str(target_task.get("run_id") or "") != str(context.get("run_id") or ""):
        raise ValueError("working context run_id does not match target task")

def save_working_context(
    context: Dict[str, Any], db_path: Optional[Path] = None,
    *, fingerprint_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append an immutable WorkingContext, reusing the latest equal fingerprint."""
    required = (
        "context_id", "run_scope", "run_id", "workflow_id", "task_id",
        "agent_role", "context_fingerprint",
    )
    if any(not context.get(key) for key in required):
        raise ValueError("WorkingContext identity and fingerprint are required")
    if not re.fullmatch(r"wc_[A-Za-z0-9_-]+", str(context["context_id"])):
        raise ValueError("working context context_id is invalid")
    try:
        from .observation import _redact_value
        context = _redact_value(dict(context))
    except ImportError:
        context = dict(context)
    if context.get("agent_role") not in {"developer", "reviewer", "tester", "coordinator"}:
        raise ValueError("working context agent_role is invalid")
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in context.get(field_name) or []:
            if not isinstance(item, dict) or not item.get("source_ref"):
                raise ValueError(f"working context item in {field_name} requires source_ref")
            if not _valid_context_source_ref(item["source_ref"]):
                raise ValueError(f"working context item in {field_name} has invalid source_ref")
            if field_name == "verification":
                value = item.get("value")
                if isinstance(value, dict):
                    for key in ("passed", "verification_passed", "requirements_satisfied"):
                        if key in value and value[key] is not None and not isinstance(value[key], bool):
                            raise ValueError("working context verification values must be strict booleans")
            for ref in item.get("evidence_refs") or []:
                if not _valid_context_source_ref(ref):
                    raise ValueError(f"working context item in {field_name} has invalid evidence_ref")
    for ref in context.get("source_refs") or []:
        if not _valid_context_source_ref(ref):
            raise ValueError("working context source_refs contains an invalid reference")
    for ref in (context.get("goal_source_ref"), context.get("next_action_source_ref")):
        if ref and not _valid_context_source_ref(ref):
            raise ValueError("working context scalar source_ref is invalid")
    for ref in (context.get("current_state_refs") or {}).values():
        if not _valid_context_source_ref(ref):
            raise ValueError("working context current_state_refs contains an invalid reference")
    if not re.fullmatch(r"[0-9a-f]{64}", str(context.get("context_fingerprint") or "")):
        raise ValueError("working context fingerprint is invalid")
    if fingerprint_config is None:
        fingerprint_config = context.get("_fingerprint_config")
    if not isinstance(fingerprint_config, dict):
        raise ValueError("working context requires fingerprint configuration")
    max_chars = int(fingerprint_config.get("max_chars", 20000))
    if max_chars < 1 or len(json.dumps(context, ensure_ascii=False)) > max_chars:
        raise ValueError("working context exceeds fingerprint configuration budget")
    max_items = int(fingerprint_config.get("max_items", 20000))
    item_fields = (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    )
    protected_fields = {"blockers", "verification", "open_questions"}
    nonprotected_count = sum(
        len(context.get(field) or [])
        for field in item_fields
        if field not in protected_fields
    )
    if max_items < 1 or nonprotected_count > max_items:
        raise ValueError("working context exceeds fingerprint item budget")
    kind_caps = fingerprint_config.get("max_items_per_kind") or {}
    for field in item_fields:
        cap = kind_caps.get(field)
        if cap is not None and len(context.get(field) or []) > int(cap):
            raise ValueError(f"working context exceeds fingerprint cap for {field}")
    from .context_models import WorkingContext, _hash
    from .context_projection import _fingerprint_payload
    try:
        fingerprint_candidate = WorkingContext.from_mapping(context)
        expected_fingerprint = _hash(_fingerprint_payload(fingerprint_candidate, fingerprint_config))
    except Exception as exc:
        raise ValueError("working context fingerprint input is invalid") from exc
    if str(expected_fingerprint) != str(context.get("context_fingerprint") or ""):
        raise ValueError("working context fingerprint does not match payload")
    all_source_refs = set(context.get("source_refs") or [])
    all_source_refs.update(str(ref) for ref in (context.get("goal_source_ref"), context.get("next_action_source_ref")) if ref)
    all_source_refs.update(str(ref) for ref in (context.get("current_state_refs") or {}).values())
    for field_name in (
        "completed", "artifacts", "evidence", "findings", "decisions", "blockers",
        "open_questions", "verification", "handoffs",
    ):
        for item in context.get(field_name) or []:
            all_source_refs.add(str(item.get("source_ref") or ""))
    goal_source_ref = str(context.get("goal_source_ref") or "")
    if (
        not goal_source_ref
        or goal_source_ref.startswith(("policy:", "artifact:", "evidence:"))
    ):
        raise ValueError("working context requires a stored goal_source_ref")
    source_backed = True
    if source_backed:
        source_clock_value = (context.get("metrics") or {}).get("source_clock")
        if (
            isinstance(source_clock_value, bool)
            or not isinstance(source_clock_value, int)
            or source_clock_value < 0
        ):
            raise ValueError("source-backed working context requires a non-negative integer source_clock")
        from .context_models import _payload_digest
        if (context.get("metrics") or {}).get("payload_digest") != _payload_digest(context):
            raise ValueError("working context payload digest does not match fingerprint input")
    payload_json = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    if len(json.dumps(context, ensure_ascii=False)) > 20000:
        raise ValueError("working context exceeds the storage size limit")
    metrics_json = json.dumps(
        context.get("metrics") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        existing_id = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?",
            (str(context["context_id"]),),
        ).fetchone()
        if existing_id is not None:
            if (
                str(existing_id["run_scope"]) != str(context["run_scope"])
                or str(existing_id["task_id"]) != str(context["task_id"])
                or str(existing_id["agent_role"]) != str(context["agent_role"])
                or str(existing_id["workflow_id"] or "") != str(context.get("workflow_id") or "")
            ):
                raise ValueError("context_id is already used by another WorkingContext scope")
            from .context_models import _payload_digest
            existing_mapping = _decode_working_context_row(existing_id)
            if (
                str(existing_id["context_fingerprint"]) != str(context["context_fingerprint"])
                or _payload_digest(existing_mapping) != _payload_digest(context)
            ):
                raise ValueError("context_id is already used by a different WorkingContext payload")
            conn.commit()
            return existing_mapping
        source_clock = context.get("metrics", {}).get("source_clock")
        if source_backed:
            clock_row = conn.execute(
                """
                SELECT revision
                FROM working_context_source_clock
                WHERE run_scope = ? AND workflow_id = ?
                """,
                (
                    str(context.get("run_scope") or ""),
                    str(context.get("workflow_id") or ""),
                ),
            ).fetchone()
            current_clock = int(clock_row["revision"] if clock_row else 0)
            if current_clock != int(source_clock):
                conn.commit()
                result = dict(context)
                result["_stale_snapshot"] = True
                return result
        _validate_context_source_existence(conn, context)
        source_head = conn.execute(
            """
            SELECT source_version, revision
            FROM working_context_source_heads
            WHERE run_scope = ? AND workflow_id = ?
            """,
            (
                str(context["run_scope"]),
                str(context.get("workflow_id") or ""),
            ),
        ).fetchone()
        if source_head is not None and (
            str(source_head["source_version"]) != str(context.get("source_version") or "")
            or int(source_head["revision"] or 0) != int(context.get("source_watermark") or 0)
        ):
            conn.commit()
            result = dict(context)
            result["_stale_snapshot"] = True
            return result
        latest = conn.execute(
            """SELECT * FROM working_contexts
               WHERE task_id = ? AND agent_role = ?
                 AND run_scope = ? AND workflow_id IS ?
               ORDER BY source_watermark DESC, compiled_at DESC, rowid DESC LIMIT 1""",
            (
                str(context["task_id"]), str(context["agent_role"]),
                str(context["run_scope"]), context.get("workflow_id"),
            ),
        ).fetchone()
        if latest is not None and latest["context_fingerprint"] == context["context_fingerprint"]:
            conn.commit()
            return _decode_working_context_row(latest)
        # A late request with an older logical revision is still retained as
        # immutable history, but cannot become the latest snapshot.
        candidate_time = float(context.get("compiled_at") or time.time())
        candidate_watermark = int(context.get("source_watermark") or 0)
        latest_watermark = int(latest["source_watermark"] or 0) if latest is not None else 0
        stale_candidate = bool(
            latest is not None and (
                latest_watermark > candidate_watermark
                or (latest_watermark == candidate_watermark and float(latest["compiled_at"] or 0.0) > candidate_time)
            )
        )
        conn.execute(
            """INSERT INTO working_contexts (
                context_id, run_scope, run_id, workflow_id, task_id, node_id,
                agent_role, context_fingerprint, source_version, source_watermark,
                payload_json, metrics_json, compiled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                context["context_id"], context["run_scope"], context.get("run_id"),
                context.get("workflow_id"), context["task_id"], context.get("node_id"),
                context["agent_role"], context["context_fingerprint"],
                context.get("source_version"), int(context.get("source_watermark") or 0),
                payload_json, metrics_json, candidate_time,
            ),
        )
        row = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?",
            (str(context["context_id"]),),
        ).fetchone()
        if row is None:
            raise RuntimeError("working context insert was not readable")
        conn.commit()
        result = _decode_working_context_row(row)
        if stale_candidate:
            result["_stale_snapshot"] = True
        return result
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def register_working_context_source(
    *,
    run_scope: str,
    workflow_id: Optional[str],
    source_version: str,
    db_path: Optional[Path] = None,
) -> int:
    """Register a source projection and return its monotonic scope revision."""
    if not run_scope or not source_version:
        raise ValueError("source scope and version are required")
    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        normalized_workflow_id = str(workflow_id or "")
        row = conn.execute(
            """
            SELECT source_version, revision
            FROM working_context_source_heads
            WHERE run_scope = ? AND workflow_id = ?
            """,
            (str(run_scope), normalized_workflow_id),
        ).fetchone()
        if row is not None and row["source_version"] == str(source_version):
            revision = int(row["revision"] or 0)
        else:
            revision = int(row["revision"] or 0) + 1 if row is not None else 1
            conn.execute(
                """
                INSERT INTO working_context_source_heads
                    (run_scope, workflow_id, source_version, revision, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_scope, workflow_id) DO UPDATE SET
                    source_version = excluded.source_version,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    str(run_scope),
                    normalized_workflow_id,
                    str(source_version),
                    revision,
                    time.time(),
                ),
            )
        conn.commit()
        return revision
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def working_context_source_is_current(
    context: Dict[str, Any], db_path: Optional[Path] = None,
) -> bool:
    """Check that a candidate still belongs to the current source generation."""
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT source_version, revision
            FROM working_context_source_heads
            WHERE run_scope = ? AND workflow_id = ?
            """,
            (
                str(context.get("run_scope") or ""),
                str(context.get("workflow_id") or ""),
            ),
        ).fetchone()
        return bool(
            row is not None
            and row["source_version"] == str(context.get("source_version") or "")
            and int(row["revision"] or 0) == int(context.get("source_watermark") or 0)
        )
    finally:
        conn.close()


def record_working_context_metric(
    context: Dict[str, Any],
    *,
    reused: bool,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record one compile invocation separately from immutable snapshots."""
    conn = get_db_connection(db_path)
    try:
        metric = {
            "metric_id": f"wcm_{uuid.uuid4().hex}",
            "context_id": str(context.get("context_id") or ""),
            "run_scope": str(context.get("run_scope") or ""),
            "run_id": context.get("run_id"),
            "workflow_id": context.get("workflow_id"),
            "task_id": str(context.get("task_id") or ""),
            "agent_role": str(context.get("agent_role") or ""),
            "reused": 1 if reused else 0,
            "changed": 0 if reused else 1,
            "created_at": time.time(),
        }
        if not metric["context_id"] or not metric["run_scope"] or not metric["task_id"]:
            raise ValueError("context metric identity is incomplete")
        conn.execute(
            """INSERT INTO working_context_metric_events (
                metric_id, context_id, run_scope, run_id, workflow_id, task_id,
                agent_role, reused, changed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(metric.values()),
        )
        conn.commit()
        return metric
    finally:
        conn.close()


def get_working_context(
    context_id: str, db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM working_contexts WHERE context_id = ?", (str(context_id),),
        ).fetchone()
        return _decode_working_context_row(row) if row is not None else None
    finally:
        conn.close()


def get_latest_working_context(
    task_id: str,
    agent_role: Optional[str] = None,
    db_path: Optional[Path] = None,
    run_scope: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM working_contexts WHERE task_id = ?"
        params: List[Any] = [str(task_id)]
        if agent_role is not None:
            query += " AND agent_role = ?"
            params.append(str(agent_role))
        if run_scope is not None:
            query += " AND run_scope = ?"
            params.append(str(run_scope))
        if workflow_id is not None:
            query += " AND workflow_id = ?"
            params.append(str(workflow_id))
        query += " ORDER BY source_watermark DESC, compiled_at DESC, rowid DESC LIMIT 1"
        row = conn.execute(query, params).fetchone()
        return _decode_working_context_row(row) if row is not None else None
    finally:
        conn.close()


def list_working_contexts(
    task_id: str,
    agent_role: Optional[str] = None,
    db_path: Optional[Path] = None,
    run_scope: Optional[str] = None,
    workflow_id: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    conn = get_db_connection(db_path)
    try:
        query = "SELECT * FROM working_contexts WHERE task_id = ?"
        params: List[Any] = [str(task_id)]
        if agent_role is not None:
            query += " AND agent_role = ?"
            params.append(str(agent_role))
        if run_scope is not None:
            query += " AND run_scope = ?"
            params.append(str(run_scope))
        if workflow_id is not None:
            query += " AND workflow_id = ?"
            params.append(str(workflow_id))
        query += " ORDER BY compiled_at ASC, rowid ASC LIMIT ? OFFSET ?"
        params.extend([max(0, int(limit)), max(0, int(offset))])
        return [_decode_working_context_row(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


PROTECTED_TASK_METADATA_FIELDS = {
    "task_id",
    "workflow_id",
    "run_id",
    "node",
    "stage",
    "agent",
    "status",
    "created_at",
    "updated_at",
}

PROTECTED_WORKFLOW_METADATA_FIELDS = {
    "workflow_id",
    "status",
    "created_at",
    "updated_at",
}

RESERVED_EVENT_METADATA_FIELDS = {
    "from",
    "to",
    "from_status",
    "to_status",
    "reason",
    "source",
    "timestamp",
    "forced",
}


def transition_task(
    task_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Atomically validate and transition a task status, appending a canonical WorkflowEvent."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Task '{task_id}' not found")

        old_status = row["status"]
        validate_task_transition(old_status, to_status, force=force)

        meta = dict(metadata or {})
        forbidden = set(meta.keys()) & PROTECTED_TASK_METADATA_FIELDS
        if forbidden:
            raise ValueError(f"Cannot overwrite protected task fields via metadata: {sorted(forbidden)}")
        forbidden_event = set(meta.keys()) & RESERVED_EVENT_METADATA_FIELDS
        if forbidden_event:
            raise ValueError(f"Cannot overwrite reserved event fields via metadata: {sorted(forbidden_event)}")

        now = time.time()
        payload = json.loads(row["payload_json"] or "{}")
        task_dict = dict(payload)
        task_dict.update({
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "node": row["node"],
            "stage": row["stage"],
            "agent": row["agent"],
            "status": old_status,
            "stage_verdict": row["stage_verdict"],
            "stage_verdict_note": row["stage_verdict_note"],
            "pane_id": row["pane_id"],
            "goal": row["goal"],
            "blocker": row["blocker"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })

        if force:
            task_dict["forced"] = True
        for k, v in meta.items():
            task_dict[k] = v

        task_dict["status"] = to_status
        task_dict["updated_at"] = now
        task_dict["last_activity_at"] = now

        if to_status in ACTIVE_TASK_STATUSES and not task_dict.get("started_at"):
            task_dict["started_at"] = now

        if to_status in COMPLETED_TASK_STATUSES and not task_dict.get("last_result"):
            task_dict["last_result"] = to_status

        # Append to status_history in payload (meta unpacked first, canonical fields last)
        status_history = list(task_dict.get("status_history") or [])
        status_history_entry = {
            **meta,
            "from": old_status,
            "to": to_status,
            "reason": reason,
            "source": source,
            "timestamp": now,
        }
        if force:
            status_history_entry["forced"] = True
        status_history.append(status_history_entry)
        task_dict["status_history"] = status_history

        save_task(task_dict, db_path=None, conn=conn)

        event_payload = {
            **meta,
            "from_status": old_status,
            "to_status": to_status,
            "reason": reason,
            "forced": force,
        }
        event = record_event(
            {
                "workflow_id": task_dict.get("workflow_id"),
                "node_id": task_dict.get("node") or task_dict.get("stage"),
                "task_id": task_id,
                "agent_id": task_dict.get("agent"),
                "event_type": "task_transition",
                "payload": event_payload,
                "source": source,
                "timestamp": now,
            },
            conn=conn,
        )

        if should_close:
            conn.execute("COMMIT;")

        return {
            "ok": True,
            "task_id": task_id,
            "workflow_id": task_dict.get("workflow_id"),
            "old_status": old_status,
            "new_status": to_status,
            "reason": reason,
            "source": source,
            "event_id": event.get("id"),
            "task": task_dict,
        }
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()


def transition_workflow(
    workflow_id: str,
    to_status: str,
    reason: str,
    source: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    force: bool = False,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Atomically validate and transition a workflow status, appending a canonical WorkflowEvent."""
    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        old_status = row["status"]
        validate_workflow_transition(old_status, to_status, force=force)

        user_meta = dict(metadata or {})
        forbidden = set(user_meta.keys()) & PROTECTED_WORKFLOW_METADATA_FIELDS
        if forbidden:
            raise ValueError(f"Cannot overwrite protected workflow fields via metadata: {sorted(forbidden)}")
        forbidden_event = set(user_meta.keys()) & RESERVED_EVENT_METADATA_FIELDS
        if forbidden_event:
            raise ValueError(f"Cannot overwrite reserved event fields via metadata: {sorted(forbidden_event)}")

        now = time.time()
        meta = json.loads(row["metadata_json"] or "{}")
        cfg = json.loads(row["config_json"] or "{}")

        wf_dict = dict(meta)
        wf_dict.update({
            "workflow_id": row["workflow_id"],
            "title": row["title"],
            "status": old_status,
            "template_name": row["template_name"],
            "current_stage": row["current_stage"],
            "config": cfg,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })

        if force:
            wf_dict["forced"] = True
        for k, v in user_meta.items():
            wf_dict[k] = v

        wf_dict["status"] = to_status
        wf_dict["updated_at"] = now

        if to_status == "completed":
            wf_dict.setdefault("completed_at", now)

        save_workflow(wf_dict, db_path=None, conn=conn)

        event_payload = {
            **user_meta,
            "from_status": old_status,
            "to_status": to_status,
            "reason": reason,
            "forced": force,
        }
        event = record_event(
            {
                "workflow_id": workflow_id,
                "node_id": user_meta.get("node_id") or wf_dict.get("current_stage"),
                "task_id": None,
                "agent_id": None,
                "event_type": "workflow_transition",
                "payload": event_payload,
                "source": source,
                "timestamp": now,
            },
            conn=conn,
        )

        if should_close:
            conn.execute("COMMIT;")

        return {
            "ok": True,
            "workflow_id": workflow_id,
            "old_status": old_status,
            "new_status": to_status,
            "reason": reason,
            "source": source,
            "event_id": event.get("id"),
            "workflow": wf_dict,
        }
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()


def update_task_metadata(
    task_id: str,
    updates: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Atomically update non-protected metadata fields of a task without touching status or identity."""
    forbidden = set(updates.keys()) & PROTECTED_TASK_METADATA_FIELDS
    if forbidden:
        raise ValueError(f"Cannot update protected task fields via metadata update: {sorted(forbidden)}")

    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Task '{task_id}' not found")

        now = time.time()
        payload = json.loads(row["payload_json"] or "{}")
        t = dict(payload)
        t.update({
            "task_id": row["task_id"],
            "workflow_id": row["workflow_id"],
            "node": row["node"],
            "stage": row["stage"],
            "agent": row["agent"],
            "status": row["status"],
            "stage_verdict": row["stage_verdict"],
            "stage_verdict_note": row["stage_verdict_note"],
            "pane_id": row["pane_id"],
            "goal": row["goal"],
            "blocker": row["blocker"],
            "created_at": row["created_at"],
            "updated_at": now,
        })

        for k, v in updates.items():
            if v is None:
                t.pop(k, None)
            else:
                t[k] = v

        save_task(t, db_path=None, conn=conn)

        if should_close:
            conn.execute("COMMIT;")

        return t
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()


def update_workflow_metadata(
    workflow_id: str,
    updates: Dict[str, Any],
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Atomically update non-protected metadata fields of a workflow without touching status or identity."""
    forbidden = set(updates.keys()) & PROTECTED_WORKFLOW_METADATA_FIELDS
    if forbidden:
        raise ValueError(f"Cannot update protected workflow fields via metadata update: {sorted(forbidden)}")

    should_close = False
    if conn is None:
        conn = get_db_connection(db_path)
        should_close = True

    try:
        if should_close:
            conn.execute("BEGIN IMMEDIATE;")

        cur = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Workflow '{workflow_id}' not found")

        now = time.time()
        meta = json.loads(row["metadata_json"] or "{}")
        cfg = json.loads(row["config_json"] or "{}")
        wf = dict(meta)
        wf.update({
            "workflow_id": row["workflow_id"],
            "title": row["title"],
            "status": row["status"],
            "template_name": row["template_name"],
            "current_stage": row["current_stage"],
            "config": cfg,
            "created_at": row["created_at"],
            "updated_at": now,
        })

        for k, v in updates.items():
            if v is None:
                wf.pop(k, None)
            else:
                wf[k] = v

        save_workflow(wf, db_path=None, conn=conn)

        if should_close:
            conn.execute("COMMIT;")

        return wf
    except Exception:
        if should_close:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
        raise
    finally:
        if should_close:
            conn.close()


def create_checkpoint(
    workflow_id: str,
    tag: Optional[str] = None,
    parent_checkpoint_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
    checkpoint_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Capture an atomic point-in-time snapshot of a workflow and all its tasks."""
    conn = get_db_connection(db_path)
    try:
        cur_wf = conn.execute("SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,))
        wf_row = cur_wf.fetchone()
        if not wf_row:
            raise ValueError(f"Workflow '{workflow_id}' not found in state DB")

        cur_tasks = conn.execute("SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC", (workflow_id,))
        task_rows = cur_tasks.fetchall()

        now = time.time()
        ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        short_uuid = uuid.uuid4().hex[:6]
        cp_id = checkpoint_id or f"cp_{workflow_id}_{ts_str}_{short_uuid}"

        # Reconstruct structured objects
        wf_dict = json.loads(wf_row["metadata_json"] or "{}")
        wf_dict.update({
            "workflow_id": wf_row["workflow_id"],
            "title": wf_row["title"],
            "status": wf_row["status"],
            "template_name": wf_row["template_name"],
            "current_stage": wf_row["current_stage"],
            "config": json.loads(wf_row["config_json"] or "{}"),
            "created_at": wf_row["created_at"],
            "updated_at": wf_row["updated_at"],
        })

        tasks_list = []
        for r in task_rows:
            t_obj = json.loads(r["payload_json"] or "{}")
            t_obj.update({
                "task_id": r["task_id"],
                "workflow_id": r["workflow_id"],
                "node": r["node"],
                "stage": r["stage"],
                "agent": r["agent"],
                "status": r["status"],
                "stage_verdict": r["stage_verdict"],
                "stage_verdict_note": r["stage_verdict_note"],
                "pane_id": r["pane_id"],
                "goal": r["goal"],
                "blocker": r["blocker"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            })
            tasks_list.append(t_obj)

        snapshot_payload = {
            "checkpoint_id": cp_id,
            "workflow_id": workflow_id,
            "tag": tag or "",
            "parent_checkpoint_id": parent_checkpoint_id,
            "created_at": now,
            "workflow": wf_dict,
            "tasks": tasks_list,
            "metadata": metadata or {},
        }

        # Atomic insertion within a single transaction
        conn.execute("BEGIN TRANSACTION;")
        try:
            conn.execute("""
                INSERT INTO checkpoints (
                    checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                    created_at, workflow_status, task_count, snapshot_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                cp_id,
                workflow_id,
                tag or "",
                parent_checkpoint_id,
                now,
                wf_row["status"],
                len(tasks_list),
                json.dumps(snapshot_payload, ensure_ascii=False),
                json.dumps(metadata or {}, ensure_ascii=False),
            ))

            record_event({
                "workflow_id": workflow_id,
                "event_type": "checkpoint_created",
                "timestamp": now,
                "payload": {"checkpoint_id": cp_id, "tag": tag},
                "source": "checkpoint_store",
            }, conn=conn)
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        return {
            "ok": True,
            "workflow_id": workflow_id,
            "checkpoint_id": cp_id,
            "tag": tag or "",
            "parent_checkpoint_id": parent_checkpoint_id,
            "created_at": now,
            "task_count": len(tasks_list),
            "workflow_status": wf_row["status"],
        }
    finally:
        conn.close()


def list_checkpoints(workflow_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """List checkpoints for a workflow sorted newest first."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("""
            SELECT checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                   created_at, workflow_status, task_count, metadata_json
            FROM checkpoints
            WHERE workflow_id = ?
            ORDER BY created_at DESC;
        """, (workflow_id,))

        results = []
        for row in cur.fetchall():
            results.append({
                "checkpoint_id": row["checkpoint_id"],
                "workflow_id": row["workflow_id"],
                "tag": row["tag"],
                "parent_checkpoint_id": row["parent_checkpoint_id"],
                "created_at": row["created_at"],
                "workflow_status": row["workflow_status"],
                "task_count": row["task_count"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
            })
        return results
    finally:
        conn.close()


def get_checkpoint(workflow_id: str, checkpoint_id: str, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Retrieve full snapshot payload from database."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("""
            SELECT snapshot_json FROM checkpoints
            WHERE workflow_id = ? AND checkpoint_id = ?;
        """, (workflow_id, checkpoint_id))
        row = cur.fetchone()
        if not row:
            raise FileNotFoundError(f"Checkpoint '{checkpoint_id}' not found for workflow '{workflow_id}'")
        return json.loads(row["snapshot_json"])
    finally:
        conn.close()


def restore_checkpoint(
    workflow_id: str,
    checkpoint_id: str,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Restore workflow state and tasks atomically from a checkpoint snapshot in SQLite."""
    snapshot = get_checkpoint(workflow_id, checkpoint_id, db_path)
    restored_wf = snapshot.get("workflow")
    restored_tasks = snapshot.get("tasks", [])

    if not restored_wf:
        raise ValueError(f"Checkpoint '{checkpoint_id}' contains no workflow data")

    conn = get_db_connection(db_path)
    try:
        conn.execute("BEGIN TRANSACTION;")
        try:
            # 1. Restore workflow record
            save_workflow(restored_wf, db_path, conn=conn)

            # 2. Delete existing tasks for this workflow and insert restored tasks
            conn.execute("DELETE FROM tasks WHERE workflow_id = ?;", (workflow_id,))
            for t in restored_tasks:
                save_task(t, db_path, conn=conn)

            # 3. Record event
            record_event({
                "workflow_id": workflow_id,
                "event_type": "checkpoint_restored",
                "payload": {"checkpoint_id": checkpoint_id},
                "source": "checkpoint_store",
            }, conn=conn)

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        return {
            "ok": True,
            "workflow_id": workflow_id,
            "checkpoint_id": checkpoint_id,
            "restored_tasks": len(restored_tasks),
            "status": restored_wf.get("status"),
        }
    finally:
        conn.close()


def fork_workflow_from_checkpoint(
    checkpoint_id: str,
    new_workflow_id: str,
    new_title: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Time-travel branching: Fork a new workflow instance from a historical checkpoint."""
    conn = get_db_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
        cp_row = cur.fetchone()
        if not cp_row:
            raise FileNotFoundError(f"Source checkpoint '{checkpoint_id}' not found")

        snapshot = json.loads(cp_row["snapshot_json"])
        source_wf = snapshot.get("workflow", {})
        source_tasks = snapshot.get("tasks", [])

        now = time.time()
        forked_wf = dict(source_wf)
        forked_wf["workflow_id"] = new_workflow_id
        forked_wf["title"] = new_title or f"{source_wf.get('title', 'Workflow')} (Forked from {checkpoint_id[:12]})"
        forked_wf["created_at"] = now
        forked_wf["updated_at"] = now
        forked_wf["forked_from"] = {
            "source_workflow_id": cp_row["workflow_id"],
            "source_checkpoint_id": checkpoint_id,
            "forked_at": now,
        }

        # Clone and remap tasks
        forked_tasks = []
        for t in source_tasks:
            t_clone = dict(t)
            orig_tid = t.get("task_id", "")
            t_clone["task_id"] = f"{orig_tid}-fork-{uuid.uuid4().hex[:6]}"
            t_clone["workflow_id"] = new_workflow_id
            t_clone["created_at"] = now
            forked_tasks.append(t_clone)

        conn.execute("BEGIN TRANSACTION;")
        try:
            save_workflow(forked_wf, db_path, conn=conn)
            for t in forked_tasks:
                save_task(t, db_path, conn=conn)

            record_event({
                "workflow_id": new_workflow_id,
                "event_type": "workflow_forked",
                "timestamp": now,
                "payload": {
                    "source_checkpoint_id": checkpoint_id,
                    "source_workflow_id": cp_row["workflow_id"],
                },
                "source": "checkpoint_store",
            }, conn=conn)

            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

        return {
            "ok": True,
            "new_workflow_id": new_workflow_id,
            "new_title": forked_wf["title"],
            "source_checkpoint_id": checkpoint_id,
            "source_workflow_id": cp_row["workflow_id"],
            "cloned_tasks": len(forked_tasks),
        }
    finally:
        conn.close()


def get_checkpoint_lineage(workflow_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Retrieve the DAG lineage of checkpoints for a workflow."""
    cps = list_checkpoints(workflow_id, db_path)
    by_id = {c["checkpoint_id"]: c for c in cps}

    lineage_tree = []
    for c in cps:
        parent_id = c.get("parent_checkpoint_id")
        lineage_tree.append({
            "checkpoint_id": c["checkpoint_id"],
            "tag": c.get("tag"),
            "created_at": c["created_at"],
            "parent_id": parent_id,
            "has_parent": bool(parent_id and parent_id in by_id),
            "task_count": c["task_count"],
        })
    return lineage_tree


def migrate_v1_to_v2(
    workflows_file: Optional[Path] = None,
    tasks_file: Optional[Path] = None,
    checkpoints_dir: Optional[Path] = None,
    steering_file: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Lossless migration of V1 JSON registries and checkpoint files into SQLite."""
    wf_path = workflows_file or (CONTROLLER_DIR / "workflows.json")
    tasks_path = tasks_file or (CONTROLLER_DIR / "tasks.json")
    cp_path = checkpoints_dir or (CONTROLLER_DIR / "checkpoints")
    st_path = steering_file or (CONTROLLER_DIR / "steering.json")
    db = init_db(db_path)

    migrated_wfs = 0
    migrated_tasks = 0
    migrated_cps = 0
    migrated_steers = 0

    # 1. Migrate workflows
    if wf_path.exists():
        try:
            with open(wf_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for wid, wf_obj in data.get("workflows", {}).items():
                wf_obj.setdefault("workflow_id", wid)
                save_workflow(wf_obj, db)
                migrated_wfs += 1
        except Exception:
            pass

    # 2. Migrate tasks
    if tasks_path.exists():
        try:
            with open(tasks_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for t_obj in data.get("tasks", []):
                if t_obj.get("task_id") and t_obj.get("workflow_id"):
                    save_task(t_obj, db)
                    migrated_tasks += 1
        except Exception:
            pass

    # 3. Migrate checkpoints
    if cp_path.exists():
        for f in cp_path.rglob("cp_*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    snap = json.load(fp)
                cpid = snap.get("checkpoint_id")
                wid = snap.get("workflow_id")
                if not cpid or not wid:
                    continue

                conn = get_db_connection(db)
                try:
                    conn.execute("""
                        INSERT INTO checkpoints (
                            checkpoint_id, workflow_id, tag, parent_checkpoint_id,
                            created_at, workflow_status, task_count, snapshot_json, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(checkpoint_id) DO NOTHING;
                    """, (
                        cpid,
                        wid,
                        snap.get("tag", ""),
                        snap.get("parent_checkpoint_id"),
                        float(snap.get("created_at") or time.time()),
                        snap.get("workflow", {}).get("status", "pending"),
                        len(snap.get("tasks", [])),
                        json.dumps(snap, ensure_ascii=False),
                        json.dumps(snap.get("metadata", {}), ensure_ascii=False),
                    ))
                    migrated_cps += 1
                finally:
                    conn.close()
            except Exception:
                continue

    # 4. Migrate steering
    if st_path.exists():
        try:
            with open(st_path, "r", encoding="utf-8") as f:
                s_data = json.load(f)
            for tid, q in s_data.get("steering_queues", {}).items():
                for s_item in q:
                    s_item.setdefault("task_id", tid)
                    save_steer(s_item, db)
                    migrated_steers += 1
            for h in s_data.get("history", []):
                record_steering_history(h, db)
        except Exception:
            pass

    return {
        "ok": True,
        "db_path": str(db),
        "migrated_workflows": migrated_wfs,
        "migrated_tasks": migrated_tasks,
        "migrated_checkpoints": migrated_cps,
        "migrated_steers": migrated_steers,
    }
