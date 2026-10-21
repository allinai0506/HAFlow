"""schema persistence; extracted without changing behavior."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
import fcntl
from .constants import _INITIALIZED_DBS
from .schema_ddl_helpers import _ensure_eval_replay_columns, _ensure_rollout_schema
from .schema_ddl_txn import _ensure_schema_ddl_transaction
from .schema_v1 import _bootstrap_v1


@contextmanager
def _schema_initialization_lock(path: Path):
    lock_path = Path(f"{path}.schema.lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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

    from herdr.recovery_store import ensure_schema as ensure_recovery_schema
    ensure_recovery_schema(conn)

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
            version INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id) ON DELETE CASCADE
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS completion_observations (
            task_id TEXT PRIMARY KEY,
            workflow_id TEXT,
            observed_status TEXT,
            observed_version INTEGER,
            observed_updated_at REAL,
            marker_present INTEGER NOT NULL DEFAULT 0,
            agent_status TEXT,
            first_seen_at REAL,
            last_seen_at REAL,
            last_sample_at REAL,
            last_observed_at REAL,
            consecutive_samples INTEGER NOT NULL DEFAULT 0,
            vanished INTEGER NOT NULL DEFAULT 0,
            epoch_changed INTEGER NOT NULL DEFAULT 0,
            blocker_present INTEGER,
            updated_at REAL NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
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

    _ensure_schema_ddl_transaction(conn)

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
    # Immutable Agent Execution Outcome facts (write-time truth for the
    # Adaptive Router; see herdr/execution_outcome.py). No foreign keys:
    # outcomes survive workflow deletion like eval rows do.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_execution_outcomes (
            outcome_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            node TEXT NOT NULL,
            task_type TEXT NOT NULL DEFAULT '',
            started_at REAL,
            finished_at REAL,
            wall_time_seconds REAL,
            final_status TEXT NOT NULL,
            requirements_satisfied INTEGER NOT NULL,
            verification_passed INTEGER NOT NULL,
            qualified_success INTEGER NOT NULL,
            rework_count INTEGER NOT NULL DEFAULT 0,
            blocked_count INTEGER NOT NULL DEFAULT 0,
            human_intervention_count INTEGER NOT NULL DEFAULT 0,
            recorded_at REAL NOT NULL,
            source_eval_id TEXT NOT NULL,
            source_eval_revision INTEGER NOT NULL,
            source_task_version INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(task_id, run_id)
        );
    """)
    existing_bucket = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'idx_outcomes_bucket'"
    ).fetchone()
    if existing_bucket is not None and "outcome_id" not in str(
        existing_bucket["sql"] or ""
    ):
        # Mid-branch DBs may carry the first 4-column revision; rebuild so
        # the ORDER BY tiebreaker stays index-served (no temp B-tree).
        conn.execute("DROP INDEX idx_outcomes_bucket")
        existing_bucket = None
    if existing_bucket is None:
        conn.execute(
            "CREATE INDEX idx_outcomes_bucket "
            "ON agent_execution_outcomes(agent, node, task_type, recorded_at DESC, outcome_id DESC);"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_specs_source ON replay_specs(source_run_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_specs_replay ON replay_specs(replay_run_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replay_specs_workflow ON replay_specs(workflow_id);")
    _ensure_rollout_schema(conn)

    _bootstrap_v1(conn, path_key)
