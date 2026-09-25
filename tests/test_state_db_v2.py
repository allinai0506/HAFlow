#!/usr/bin/env python3
"""Tests for Herdr Checkpoint Store V2: SQLite Embedded State Engine.

Covers:
- Database initialization and WAL mode configuration
- Workflows & Tasks CRUD operations
- Single-transaction atomic checkpoint capture
- Indexed checkpoint query and retrieval
- Atomic time-travel restoration
- Workflow forking / branching from historical checkpoints
- Checkpoint DAG lineage tracking
- V1 JSON to V2 SQLite lossless migration
- Kernel primitives bridge integration
- herdr-task CLI checkpoint commands
"""

import json
import multiprocessing
import os
import sqlite3
import subprocess
import time
from pathlib import Path
import pytest

from herdr import state_db
from herdr import kernel


HERDR_ROOT = Path(__file__).resolve().parent.parent


def _upgrade_event_columns_in_process(db_path, barrier, results):
    """Force two independent processes past PRAGMA before ALTER TABLE."""
    class BarrierConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._schema_snapshot_barrier_used = False

        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if (
                not self._schema_snapshot_barrier_used
                and sql.strip().upper().startswith("PRAGMA TABLE_INFO(EVENTS")
            ):
                self._schema_snapshot_barrier_used = True
                barrier.wait(timeout=10)
            return result

    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,
        timeout=10,
        factory=BarrierConnection,
    )
    conn.row_factory = sqlite3.Row
    try:
        state_db._ensure_event_columns(conn)
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok",))
    finally:
        conn.close()


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    """Isolated environment for state DB testing."""
    db_file = tmp_path / "state.db"
    wf_file = tmp_path / "workflows.json"
    tasks_file = tmp_path / "tasks.json"
    steer_file = tmp_path / "steering.json"
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("STEERING_FILE", str(steer_file))
    monkeypatch.setenv("CHECKPOINTS_DIR", str(cp_dir))

    wf_file.write_text(json.dumps({"workflows": {}}), encoding="utf-8")
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    steer_file.write_text(json.dumps({}), encoding="utf-8")

    return {
        "db_file": db_file,
        "wf_file": wf_file,
        "tasks_file": tasks_file,
        "steer_file": steer_file,
        "cp_dir": cp_dir,
    }


def test_init_db_and_wal_mode(state_env):
    """Verify SQLite database initialization, tables, and WAL mode."""
    db_path = state_db.init_db(state_env["db_file"])
    assert db_path.exists()

    conn = state_db.get_db_connection(db_path)
    try:
        # Check WAL mode
        cur = conn.execute("PRAGMA journal_mode;")
        mode = cur.fetchone()[0]
        assert mode.lower() == "wal"

        # Check tables existence
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}
        assert {"workflows", "tasks", "checkpoints", "events"}.issubset(tables)
    finally:
        conn.close()


def _open_state_db_worker(db_path, start_event, results):
    start_event.wait(10)
    try:
        conn = state_db.get_db_connection(Path(db_path))
        conn.execute("SELECT 1 FROM working_context_source_clock LIMIT 1").fetchall()
        conn.close()
        results.put(("ok", ""))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _run_concurrent_state_db_open(db_path, count=4):
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_open_state_db_worker,
            args=(str(db_path), start_event, results),
        )
        for _ in range(count)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(20)
    return [results.get(timeout=2) for _ in processes]


def _run_concurrent_state_db_open_paths(db_paths):
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_open_state_db_worker,
            args=(str(db_path), start_event, results),
        )
        for db_path in db_paths
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(20)
    return [results.get(timeout=2) for _ in processes]


def test_schema_lock_uses_resolved_database_identity_for_path_aliases(tmp_path):
    real_path = tmp_path / "real-clock-path.db"
    alias_path = tmp_path / "alias-clock-path.db"
    alias_path.symlink_to(real_path)
    results = _run_concurrent_state_db_open_paths([real_path, alias_path] * 3)
    assert all(result[0] == "ok" for result in results), results


def test_empty_database_concurrent_initialization_is_reentrant(tmp_path):
    db_path = tmp_path / "concurrent-empty.db"
    results = _run_concurrent_state_db_open(db_path)
    assert all(result[0] == "ok" for result in results), results


def test_legacy_source_clock_concurrent_migration_is_reentrant(tmp_path):
    db_path = tmp_path / "concurrent-legacy-clock.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE working_context_source_clock (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO working_context_source_clock (id, revision) VALUES (1, 4)"
    )
    conn.commit()
    conn.close()
    results = _run_concurrent_state_db_open(db_path)
    assert all(result[0] == "ok" for result in results), results


def test_partial_source_head_migration_recovers_legacy_rows(tmp_path):
    db_path = tmp_path / "partial-source-head-migration.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE working_context_source_heads (
            run_scope TEXT NOT NULL,
            workflow_id TEXT NOT NULL DEFAULT '',
            source_version TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (run_scope, workflow_id)
        );
        CREATE TABLE working_context_source_heads_legacy (
            run_scope TEXT PRIMARY KEY,
            workflow_id TEXT,
            source_version TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO working_context_source_heads_legacy
            (run_scope, workflow_id, source_version, revision, updated_at)
        VALUES ('recover-scope', 'recover-wf', 'v1', 5, 1.0);
        """
    )
    conn.commit()
    conn.close()
    state_db.init_db(db_path)
    recovered = state_db.get_db_connection(db_path)
    try:
        primary_key = {
            row["name"]
            for row in recovered.execute(
                "PRAGMA table_info(working_context_source_heads)"
            ).fetchall()
            if int(row["pk"] or 0) > 0
        }
        assert primary_key == {"run_scope", "workflow_id", "task_id"}
        # Execution-shared legacy rows lack task_id and are never promoted
        # into task-specific heads; they stay in *_legacy for audit.
        assert recovered.execute(
            "SELECT COUNT(*) FROM working_context_source_heads"
        ).fetchone()[0] == 0
        legacy = recovered.execute(
            "SELECT run_scope, workflow_id, revision FROM working_context_source_heads_legacy"
        ).fetchall()
        assert {(row["run_scope"], row["workflow_id"], row["revision"]) for row in legacy} >= {
            ("recover-scope", "recover-wf", 5)
        }
        # Fresh per-task registration works after migration.
        assert state_db.register_working_context_source(
            run_scope="recover-scope", workflow_id="recover-wf",
            task_id="task-after-migration", source_version="v2",
            db_path=db_path,
        ) == 1
    finally:
        recovered.close()


def test_legacy_source_head_primary_key_migrates_to_scope_workflow(tmp_path):
    db_path = tmp_path / "legacy-source-head.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE working_context_source_heads (
            run_scope TEXT PRIMARY KEY,
            workflow_id TEXT,
            source_version TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO working_context_source_heads
            (run_scope, workflow_id, source_version, revision, updated_at)
        VALUES ('legacy-scope', 'legacy-wf', 'v1', 3, 1.0)
        """
    )
    conn.commit()
    conn.close()
    migrated = state_db.get_db_connection(db_path)
    try:
        primary_key = {
            row["name"]
            for row in migrated.execute(
                "PRAGMA table_info(working_context_source_heads)"
            ).fetchall()
            if int(row["pk"] or 0) > 0
        }
        assert primary_key == {"run_scope", "workflow_id", "task_id"}
        assert migrated.execute(
            "SELECT COUNT(*) FROM working_context_source_heads"
        ).fetchone()[0] == 0
        legacy_row = migrated.execute(
            """
            SELECT run_scope, workflow_id, source_version, revision
            FROM working_context_source_heads_legacy
            """
        ).fetchone()
        assert tuple(legacy_row) == ("legacy-scope", "legacy-wf", "v1", 3)
    finally:
        migrated.close()


def test_schema_init_replaces_legacy_source_clock_trigger(tmp_path):
    db_path = tmp_path / "legacy-trigger.db"
    state_db.init_db(db_path)
    conn = state_db.get_db_connection(db_path)
    conn.execute("DROP TRIGGER trg_working_context_source_clock_workflows_update")
    conn.execute(
        """
        CREATE TRIGGER trg_working_context_source_clock_workflows_update
        AFTER UPDATE ON workflows
        BEGIN
            UPDATE working_context_source_clock
            SET revision = revision + 1
            WHERE run_scope = NEW.workflow_id;
        END;
        """
    )
    conn.close()
    state_db.init_db(db_path)
    refreshed = state_db.get_db_connection(db_path)
    try:
        sql = refreshed.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'trigger'
              AND name = 'trg_working_context_source_clock_workflows_update'
            """
        ).fetchone()["sql"]
        assert "working_context_source_heads" in sql
    finally:
        refreshed.close()


def test_source_clock_intermediate_primary_key_is_rebuilt(tmp_path):
    db_path = tmp_path / "intermediate-source-clock.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE working_context_source_heads (
               run_scope TEXT PRIMARY KEY, workflow_id TEXT,
               source_version TEXT NOT NULL, revision INTEGER NOT NULL, updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE working_context_source_clock (
               run_scope TEXT PRIMARY KEY, workflow_id TEXT,
               revision INTEGER NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO working_context_source_clock (run_scope, workflow_id, revision) VALUES (?, ?, ?)",
        ("intermediate-scope", "wf-intermediate", 9),
    )
    conn.commit()
    conn.close()
    state_db.init_db(db_path)
    migrated = state_db.get_db_connection(db_path)
    try:
        primary_key = {
            row["name"]
            for row in migrated.execute(
                "PRAGMA table_info(working_context_source_clock)"
            ).fetchall()
            if int(row["pk"] or 0) > 0
        }
        assert primary_key == {"run_scope", "workflow_id"}
        assert migrated.execute(
            "SELECT revision FROM working_context_source_clock WHERE run_scope = ? AND workflow_id = ?",
            ("intermediate-scope", "wf-intermediate"),
        ).fetchone()["revision"] == 9
    finally:
        migrated.close()


def test_residual_legacy_source_head_uses_max_revision(tmp_path):
    db_path = tmp_path / "residual-source-head.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE working_context_source_heads (
               run_scope TEXT NOT NULL, workflow_id TEXT NOT NULL,
               source_version TEXT NOT NULL, revision INTEGER NOT NULL,
               updated_at REAL NOT NULL,
               PRIMARY KEY (run_scope, workflow_id)
           )"""
    )
    conn.execute(
        "INSERT INTO working_context_source_heads VALUES (?, ?, ?, ?, ?)",
        ("scope", "wf", "current", 2, 2.0),
    )
    conn.execute(
        """CREATE TABLE working_context_source_heads_legacy (
               run_scope TEXT NOT NULL, workflow_id TEXT,
               source_version TEXT NOT NULL, revision INTEGER NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        "INSERT INTO working_context_source_heads_legacy VALUES (?, ?, ?, ?, ?)",
        ("scope", "wf", "legacy", 9, 9.0),
    )
    conn.commit()
    conn.close()
    state_db.init_db(db_path)
    migrated = state_db.get_db_connection(db_path)
    try:
        primary_key = {
            row["name"]
            for row in migrated.execute(
                "PRAGMA table_info(working_context_source_heads)"
            ).fetchall()
            if int(row["pk"] or 0) > 0
        }
        assert primary_key == {"run_scope", "workflow_id", "task_id"}
        assert migrated.execute(
            "SELECT COUNT(*) FROM working_context_source_heads"
        ).fetchone()[0] == 0
        legacy_row = migrated.execute(
            """SELECT source_version, revision, updated_at
                 FROM working_context_source_heads_legacy
                WHERE run_scope = 'scope' AND workflow_id = 'wf'"""
        ).fetchone()
        assert legacy_row["source_version"] == "legacy"
        assert legacy_row["revision"] == 9
        main_row = migrated.execute(
            """SELECT source_version, revision, updated_at
                 FROM working_context_source_heads_legacy_main
                WHERE run_scope = 'scope' AND workflow_id = 'wf'"""
        ).fetchone()
        assert main_row["source_version"] == "current"
        assert main_row["revision"] == 2
    finally:
        migrated.close()


def test_task_head_migration_seeds_baseline_from_historical_watermarks(tmp_path):
    db_path = tmp_path / "head-baseline.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE working_context_source_heads (
               run_scope TEXT NOT NULL, workflow_id TEXT NOT NULL,
               source_version TEXT NOT NULL, revision INTEGER NOT NULL,
               updated_at REAL NOT NULL,
               PRIMARY KEY (run_scope, workflow_id)
           )"""
    )
    conn.execute(
        "INSERT INTO working_context_source_heads VALUES (?, ?, ?, ?, ?)",
        ("exec-1", "wf", "shared-v7", 7, 7.0),
    )
    conn.execute(
        """CREATE TABLE working_contexts (
               context_id TEXT PRIMARY KEY, run_scope TEXT NOT NULL,
               run_id TEXT, workflow_id TEXT, task_id TEXT NOT NULL,
               node_id TEXT, agent_role TEXT NOT NULL,
               context_fingerprint TEXT NOT NULL, source_version TEXT,
               source_watermark INTEGER NOT NULL DEFAULT 0,
               payload_json TEXT NOT NULL, metrics_json TEXT NOT NULL DEFAULT '{}',
               compiled_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO working_contexts
               (context_id, run_scope, run_id, workflow_id, task_id, agent_role,
                context_fingerprint, source_version, source_watermark,
                payload_json, compiled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("wc_old_a", "exec-1", "run-a", "wf", "task-a", "developer",
         "f" * 64, "shared-v7", 7, "{}", 10.0),
    )
    conn.execute(
        """INSERT INTO working_contexts
               (context_id, run_scope, run_id, workflow_id, task_id, agent_role,
                context_fingerprint, source_version, source_watermark,
                payload_json, compiled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("wc_old_b", "exec-1", "run-b", "wf", "task-b", "developer",
         "e" * 64, "shared-v7", 7, "{}", 11.0),
    )
    conn.commit()
    conn.close()
    state_db.init_db(db_path)
    migrated = state_db.get_db_connection(db_path)
    try:
        for task_id in ("task-a", "task-b"):
            row = migrated.execute(
                """SELECT source_version, revision FROM working_context_source_heads
                    WHERE run_scope = 'exec-1' AND workflow_id = 'wf' AND task_id = ?""",
                (task_id,),
            ).fetchone()
            assert row is not None
            assert row["revision"] == 7
            assert row["source_version"] == "shared-v7"
    finally:
        migrated.close()
    # Same version re-registers at the baseline instead of dropping to 1,
    # so a fresh candidate is not stale against retained history.
    assert state_db.register_working_context_source(
        run_scope="exec-1", workflow_id="wf", task_id="task-a",
        source_version="shared-v7", db_path=db_path,
    ) == 7
    assert state_db.register_working_context_source(
        run_scope="exec-1", workflow_id="wf", task_id="task-a",
        source_version="shared-v8", db_path=db_path,
    ) == 8


def test_malformed_task_payload_does_not_block_repair_via_save_task(tmp_path):
    db_path = tmp_path / "malformed-task-repair.db"
    state_db.init_db(db_path)
    state_db.save_workflow(
        {"workflow_id": "wf", "title": "repair", "status": "running"},
        db_path=db_path,
    )
    conn = state_db.get_db_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, workflow_id, status, payload_json) VALUES (?, ?, ?, ?)",
            ("task-broken", "wf", "working", "{"),
        )
        conn.commit()
    finally:
        conn.close()
    # Repairing the damaged row through the public API must succeed: trigger
    # scope expressions read OLD.payload_json and must tolerate malformed JSON
    # instead of raising OperationalError on the repair itself.
    state_db.save_task(
        {
            "task_id": "task-broken", "workflow_id": "wf",
            "run_id": "run-broken", "workflow_run_id": "exec-1",
            "node": "review", "status": "working", "goal": "repaired",
        },
        db_path=db_path,
    )
    repaired = state_db.get_task("task-broken", db_path=db_path)
    assert repaired is not None
    assert repaired["goal"] == "repaired"
    assert repaired["run_id"] == "run-broken"
    check = state_db.get_db_connection(db_path)
    try:
        clock = check.execute(
            "SELECT revision FROM working_context_source_clock WHERE run_scope = ? AND workflow_id = ?",
            ("exec-1", "wf"),
        ).fetchone()
    finally:
        check.close()
    assert clock is not None and int(clock["revision"]) >= 1


def test_get_latest_orders_by_time_across_scopes_not_watermark(tmp_path):
    db_path = tmp_path / "cross-scope-latest.db"
    state_db.init_db(db_path)
    conn = state_db.get_db_connection(db_path)
    try:
        for context_id, scope, watermark, compiled_at in (
            ("wc_old_exec", "exec-old", 20, 10.0),
            ("wc_new_exec", "exec-new", 3, 20.0),
        ):
            conn.execute(
                """INSERT INTO working_contexts
                       (context_id, run_scope, run_id, workflow_id, task_id,
                        agent_role, context_fingerprint, source_version,
                        source_watermark, payload_json, compiled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (context_id, scope, f"run-{scope}", "wf", "task-x",
                 "developer", "f" * 64, "v", watermark, "{}", compiled_at),
            )
        conn.commit()
    finally:
        conn.close()
    assert state_db.get_latest_working_context("task-x", db_path=db_path)["context_id"] == "wc_new_exec"
    assert state_db.get_latest_working_context(
        "task-x", db_path=db_path, run_scope="exec-old",
    )["context_id"] == "wc_old_exec"


def test_get_latest_with_scope_but_no_workflow_orders_by_time_across_heads(tmp_path):
    db_path = tmp_path / "cross-workflow-latest.db"
    state_db.init_db(db_path)
    conn = state_db.get_db_connection(db_path)
    try:
        for context_id, workflow_id, watermark, compiled_at in (
            ("wc_head_a", "wf-a", 20, 10.0),
            ("wc_head_b", "wf-b", 3, 20.0),
        ):
            conn.execute(
                """INSERT INTO working_contexts
                       (context_id, run_scope, run_id, workflow_id, task_id,
                        agent_role, context_fingerprint, source_version,
                        source_watermark, payload_json, compiled_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (context_id, "exec-1", "run-x", workflow_id, "task-x",
                 "developer", "f" * 64, "v", watermark, "{}", compiled_at),
            )
        conn.commit()
    finally:
        conn.close()
    # Watermarks belong to independent (run_scope, workflow_id, task_id)
    # heads, so a scope-only lookup must not compare them: wall clock wins.
    assert state_db.get_latest_working_context(
        "task-x", db_path=db_path, run_scope="exec-1",
    )["context_id"] == "wc_head_b"
    # Pinning the full head identity restores watermark ordering.
    assert state_db.get_latest_working_context(
        "task-x", db_path=db_path, run_scope="exec-1", workflow_id="wf-a",
    )["context_id"] == "wc_head_a"
    assert state_db.get_latest_working_context(
        "task-x", db_path=db_path, run_scope="exec-1", workflow_id="wf-b",
    )["context_id"] == "wc_head_b"


def test_residual_legacy_source_clock_uses_max_revision(tmp_path):
    db_path = tmp_path / "residual-source-clock.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE working_context_source_clock (run_scope TEXT, workflow_id TEXT, revision INTEGER NOT NULL, PRIMARY KEY (run_scope, workflow_id))"
    )
    conn.execute(
        "INSERT INTO working_context_source_clock (run_scope, workflow_id, revision) VALUES ('scope', 'wf', 2)"
    )
    conn.execute(
        "CREATE TABLE working_context_source_clock_legacy (run_scope TEXT, workflow_id TEXT, revision INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO working_context_source_clock_legacy (run_scope, workflow_id, revision) VALUES ('scope', 'wf', 9)"
    )
    conn.commit()
    conn.close()
    state_db.init_db(db_path)
    migrated = state_db.get_db_connection(db_path)
    try:
        assert migrated.execute(
            "SELECT revision FROM working_context_source_clock WHERE run_scope = 'scope' AND workflow_id = 'wf'"
        ).fetchone()["revision"] == 9
    finally:
        migrated.close()


def test_global_source_clock_schema_migrates_to_execution_scope(tmp_path):
    db_path = tmp_path / "legacy-source-clock.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE working_context_source_clock (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO working_context_source_clock (id, revision) VALUES (1, 7)"
    )
    conn.commit()
    conn.close()

    migrated = state_db.get_db_connection(db_path)
    try:
        columns = {
            row["name"]
            for row in migrated.execute(
                "PRAGMA table_info(working_context_source_clock)"
            ).fetchall()
        }
        assert {"run_scope", "workflow_id", "revision"}.issubset(columns)
        assert "id" not in columns
        legacy = migrated.execute(
            "SELECT revision FROM working_context_source_clock_legacy WHERE id = 1"
        ).fetchone()
        assert int(legacy["revision"]) == 7
    finally:
        migrated.close()


def test_concurrent_legacy_event_schema_upgrade_is_idempotent(tmp_path):
    """Two independent processes can upgrade the same legacy events table."""
    db_path = tmp_path / "legacy-events.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT,
            task_id TEXT,
            event_type TEXT,
            payload_json TEXT,
            timestamp REAL
        )
        """
    )
    conn.commit()
    conn.close()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_upgrade_event_columns_in_process,
            args=(db_path, barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert all(not process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0, 0]
    outcomes = [results.get(timeout=2) for _ in processes]
    assert outcomes == [("ok",), ("ok",)]

    conn = sqlite3.connect(db_path)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(events)")]
    assert columns.count("run_id") == 1
    assert columns.count("sequence") == 1
    conn.close()

    from herdr.trajectory import TrajectoryLedger

    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-concurrent-upgrade", "event_type": "task_started"})
    assert [event["event_type"] for event in ledger.list_events("run-concurrent-upgrade")] == [
        "task_started",
    ]


def test_workflow_event_stream_records_full_context_and_filters(state_env):
    """Verify the canonical WorkflowEvent stream captures shared context."""
    state_db.init_db(state_env["db_file"])

    recorded = state_db.record_event({
        "workflow_id": "wf-event-01",
        "node_id": "implement",
        "task_id": "task-event-01",
        "agent_id": "codex",
        "event_type": "task.started",
        "timestamp": 1773471000.5,
        "payload": {"attempt": 1, "status": "working"},
        "source": "controller",
    }, db_path=state_env["db_file"])

    assert recorded["event_type"] == "task.started"
    assert recorded["node_id"] == "implement"
    assert recorded["agent_id"] == "codex"
    assert recorded["payload"]["attempt"] == 1

    events = state_db.list_events(
        workflow_id="wf-event-01",
        task_id="task-event-01",
        agent_id="codex",
        event_type="task.started",
        source="controller",
        db_path=state_env["db_file"],
    )
    assert len(events) == 1
    assert events[0]["workflow_id"] == "wf-event-01"
    assert events[0]["node_id"] == "implement"
    assert events[0]["task_id"] == "task-event-01"
    assert events[0]["agent_id"] == "codex"
    assert events[0]["event_type"] == "task.started"
    assert events[0]["timestamp"] == 1773471000.5
    assert events[0]["payload"] == {"attempt": 1, "status": "working"}
    assert events[0]["source"] == "controller"


def test_workflow_and_task_upsert(state_env):
    """Verify workflow and task upsert and query."""
    state_db.init_db(state_env["db_file"])

    wid = "wf-test-v2-01"
    wf_data = {
        "workflow_id": wid,
        "title": "测试工作流 V2",
        "status": "running",
        "template_name": "software-development-v1",
        "config": {"nodes": [{"id": "code", "label": "开发"}]},
        "custom_attr": "value_123",
    }
    state_db.save_workflow(wf_data, state_env["db_file"])

    fetched_wf = state_db.get_workflow(wid, state_env["db_file"])
    assert fetched_wf is not None
    assert fetched_wf["title"] == "测试工作流 V2"
    assert fetched_wf["status"] == "running"
    assert fetched_wf["custom_attr"] == "value_123"

    # Add task
    t_data = {
        "task_id": "task-v2-101",
        "workflow_id": wid,
        "node": "code",
        "stage": "code",
        "agent": "claude",
        "status": "working",
        "goal": "实现核心逻辑",
    }
    state_db.save_task(t_data, state_env["db_file"])

    tasks = state_db.get_tasks(wid, state_env["db_file"])
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == "task-v2-101"
    assert tasks[0]["goal"] == "实现核心逻辑"


def test_create_and_list_checkpoints_atomic(state_env):
    """Verify atomic checkpoint capture and indexed query."""
    state_db.init_db(state_env["db_file"])
    wid = "wf-cp-atomic-02"

    state_db.save_workflow({
        "workflow_id": wid,
        "title": "检查点原子性测试",
        "status": "running",
    }, state_env["db_file"])

    state_db.save_task({
        "task_id": "t-cp-1",
        "workflow_id": wid,
        "node": "step1",
        "status": "completed",
    }, state_env["db_file"])

    # Create Checkpoint 1
    cp1 = state_db.create_checkpoint(
        workflow_id=wid,
        tag="step1_done",
        db_path=state_env["db_file"],
    )
    assert cp1["ok"] is True
    assert cp1["task_count"] == 1

    # Create Checkpoint 2 linked to Parent
    time.sleep(0.01)
    cp2 = state_db.create_checkpoint(
        workflow_id=wid,
        tag="step2_ready",
        parent_checkpoint_id=cp1["checkpoint_id"],
        db_path=state_env["db_file"],
    )
    assert cp2["ok"] is True
    assert cp2["parent_checkpoint_id"] == cp1["checkpoint_id"]

    # List checkpoints
    cps = state_db.list_checkpoints(wid, state_env["db_file"])
    assert len(cps) == 2
    # Sorted newest first
    assert cps[0]["checkpoint_id"] == cp2["checkpoint_id"]
    assert cps[1]["checkpoint_id"] == cp1["checkpoint_id"]

    # Fetch individual checkpoint
    snap = state_db.get_checkpoint(wid, cp1["checkpoint_id"], state_env["db_file"])
    assert snap["checkpoint_id"] == cp1["checkpoint_id"]
    assert snap["tag"] == "step1_done"
    assert len(snap["tasks"]) == 1

    events = state_db.list_events(workflow_id=wid, db_path=state_env["db_file"])
    assert [e["event_type"] for e in events] == ["checkpoint_created", "checkpoint_created"]
    assert events[0]["source"] == "checkpoint_store"
    assert events[0]["payload"]["checkpoint_id"] == cp1["checkpoint_id"]


def test_restore_checkpoint_atomic(state_env):
    """Verify atomic restoration of workflow and tasks from historical checkpoint."""
    state_db.init_db(state_env["db_file"])
    wid = "wf-cp-restore-03"

    state_db.save_workflow({
        "workflow_id": wid,
        "title": "基准工作流",
        "status": "running",
    }, state_env["db_file"])

    state_db.save_task({
        "task_id": "t-base-1",
        "workflow_id": wid,
        "node": "node_a",
        "status": "completed",
    }, state_env["db_file"])

    cp = state_db.create_checkpoint(wid, tag="golden_state", db_path=state_env["db_file"])
    cpid = cp["checkpoint_id"]

    # Mutate state to disaster
    state_db.save_workflow({
        "workflow_id": wid,
        "title": "已崩溃被篡改",
        "status": "failed",
    }, state_env["db_file"])
    state_db.save_task({
        "task_id": "t-bad-2",
        "workflow_id": wid,
        "node": "node_b",
        "status": "failed",
    }, state_env["db_file"])

    # Restore from golden checkpoint
    res = state_db.restore_checkpoint(wid, cpid, state_env["db_file"])
    assert res["ok"] is True
    assert res["status"] == "running"
    assert res["restored_tasks"] == 1

    # Verify state reverted exactly
    wf_after = state_db.get_workflow(wid, state_env["db_file"])
    assert wf_after["title"] == "基准工作流"
    assert wf_after["status"] == "running"

    tasks_after = state_db.get_tasks(wid, state_env["db_file"])
    assert len(tasks_after) == 1
    assert tasks_after[0]["task_id"] == "t-base-1"


def test_fork_workflow_from_checkpoint(state_env):
    """Verify state forking (branching a new workflow from a historical checkpoint)."""
    state_db.init_db(state_env["db_file"])
    source_wid = "wf-source-04"

    state_db.save_workflow({
        "workflow_id": source_wid,
        "title": "母体工作流",
        "status": "running",
        "config": {"key": "base_val"},
    }, state_env["db_file"])

    state_db.save_task({
        "task_id": "t-source-1",
        "workflow_id": source_wid,
        "node": "stage1",
        "status": "completed",
    }, state_env["db_file"])

    cp = state_db.create_checkpoint(source_wid, tag="fork_point", db_path=state_env["db_file"])
    cpid = cp["checkpoint_id"]

    # Fork new workflow
    forked_wid = "wf-forked-branch-04"
    fork_res = state_db.fork_workflow_from_checkpoint(
        checkpoint_id=cpid,
        new_workflow_id=forked_wid,
        new_title="沙盒分支测试",
        db_path=state_env["db_file"],
    )
    assert fork_res["ok"] is True
    assert fork_res["new_workflow_id"] == forked_wid
    assert fork_res["source_checkpoint_id"] == cpid
    assert fork_res["cloned_tasks"] == 1

    # Verify forked workflow exists independently
    forked_wf = state_db.get_workflow(forked_wid, state_env["db_file"])
    assert forked_wf is not None
    assert forked_wf["title"] == "沙盒分支测试"
    assert forked_wf["forked_from"]["source_workflow_id"] == source_wid

    # Verify tasks were cloned with new IDs
    forked_tasks = state_db.get_tasks(forked_wid, state_env["db_file"])
    assert len(forked_tasks) == 1
    assert forked_tasks[0]["workflow_id"] == forked_wid
    assert forked_tasks[0]["task_id"] != "t-source-1"
    assert "t-source-1-fork-" in forked_tasks[0]["task_id"]

    events = state_db.list_events(workflow_id=forked_wid, db_path=state_env["db_file"])
    assert len(events) == 1
    assert events[0]["event_type"] == "workflow_forked"
    assert events[0]["source"] == "checkpoint_store"
    assert events[0]["payload"]["source_checkpoint_id"] == cpid


def test_checkpoint_lineage(state_env):
    """Verify checkpoint parent-child DAG lineage retrieval."""
    state_db.init_db(state_env["db_file"])
    wid = "wf-lineage-05"

    state_db.save_workflow({"workflow_id": wid, "status": "running"}, state_env["db_file"])

    cp1 = state_db.create_checkpoint(wid, tag="root", db_path=state_env["db_file"])
    cp2 = state_db.create_checkpoint(wid, tag="branch_a", parent_checkpoint_id=cp1["checkpoint_id"], db_path=state_env["db_file"])

    lineage = state_db.get_checkpoint_lineage(wid, state_env["db_file"])
    assert len(lineage) == 2
    l2 = next(x for x in lineage if x["checkpoint_id"] == cp2["checkpoint_id"])
    assert l2["parent_id"] == cp1["checkpoint_id"]
    assert l2["has_parent"] is True


def test_migrate_v1_to_v2(state_env):
    """Verify lossless migration from V1 JSON files into SQLite state DB."""
    wid = "wf-migrate-v1"
    tid = "task-migrate-1"
    cpid = f"cp_{wid}_20260913_210000_aabbcc"

    # Seed V1 workflows.json
    v1_wf = {
        "workflows": {
            wid: {
                "workflow_id": wid,
                "title": "V1 历史工作流",
                "status": "completed",
            }
        }
    }
    state_env["wf_file"].write_text(json.dumps(v1_wf), encoding="utf-8")

    # Seed V1 tasks.json
    v1_tasks = {
        "tasks": [
            {
                "task_id": tid,
                "workflow_id": wid,
                "node": "coding",
                "status": "completed",
                "goal": "迁移前代码已完成",
            }
        ]
    }
    state_env["tasks_file"].write_text(json.dumps(v1_tasks), encoding="utf-8")

    # Seed V1 checkpoint file
    wf_cp_dir = state_env["cp_dir"] / wid
    wf_cp_dir.mkdir(parents=True, exist_ok=True)
    cp_file = wf_cp_dir / f"{cpid}.json"
    cp_file.write_text(json.dumps({
        "checkpoint_id": cpid,
        "workflow_id": wid,
        "tag": "v1_legacy_tag",
        "created_at": 1773470000,
        "workflow": v1_wf["workflows"][wid],
        "tasks": v1_tasks["tasks"],
    }), encoding="utf-8")

    # Perform migration
    res = state_db.migrate_v1_to_v2(
        workflows_file=state_env["wf_file"],
        tasks_file=state_env["tasks_file"],
        checkpoints_dir=state_env["cp_dir"],
        db_path=state_env["db_file"],
    )
    assert res["ok"] is True
    assert res["migrated_workflows"] == 1
    assert res["migrated_tasks"] == 1
    assert res["migrated_checkpoints"] == 1

    # Verify SQLite DB contents
    migrated_wf = state_db.get_workflow(wid, state_env["db_file"])
    assert migrated_wf is not None
    assert migrated_wf["title"] == "V1 历史工作流"

    migrated_tasks = state_db.get_tasks(wid, state_env["db_file"])
    assert len(migrated_tasks) == 1
    assert migrated_tasks[0]["task_id"] == tid

    migrated_cps = state_db.list_checkpoints(wid, state_env["db_file"])
    assert len(migrated_cps) == 1
    assert migrated_cps[0]["checkpoint_id"] == cpid
    assert migrated_cps[0]["tag"] == "v1_legacy_tag"


def test_kernel_bridge_integration(state_env):
    """Verify kernel primitives transparently bridge with state_db."""
    wid = "wf-bridge-06"
    state_env["wf_file"].write_text(json.dumps({
        "workflows": {
            wid: {
                "workflow_id": wid,
                "title": "Kernel 桥接双写验证",
                "status": "running",
            }
        }
    }), encoding="utf-8")

    state_env["tasks_file"].write_text(json.dumps({
        "tasks": [
            {
                "task_id": "t-bridge-1",
                "workflow_id": wid,
                "node": "init",
                "status": "completed",
            }
        ]
    }), encoding="utf-8")

    # 1. Create checkpoint via kernel
    cp_res = kernel.create_checkpoint(wid, tag="bridge_cp")
    assert cp_res["ok"] is True
    cpid = cp_res["checkpoint_id"]

    # 2. Verify list_checkpoints via kernel (checks both SQLite & JSON)
    cps = kernel.list_checkpoints(wid)
    assert len(cps) >= 1
    assert any(c["checkpoint_id"] == cpid for c in cps)

    # 3. Verify get_checkpoint via kernel
    snap = kernel.get_checkpoint(wid, cpid)
    assert snap["checkpoint_id"] == cpid

    # 4. Verify fork_workflow_from_checkpoint via kernel
    forked_wid = "wf-fork-kernel-bridge"
    fork_res = kernel.fork_workflow_from_checkpoint(cpid, forked_wid, new_title="分叉新任务")
    assert fork_res["ok"] is True
    assert fork_res["new_workflow_id"] == forked_wid

    # Verify forked workflow is in workflows.json
    wf_data = json.loads(state_env["wf_file"].read_text(encoding="utf-8"))
    assert forked_wid in wf_data["workflows"]
    assert wf_data["workflows"][forked_wid]["title"] == "分叉新任务"


def test_cli_checkpoint_commands(state_env):
    """Verify herdr-task CLI commands for checkpoint lifecycle."""
    task_bin = HERDR_ROOT / "bin" / "herdr-task"
    env = os.environ.copy()
    env["HERDR_STATE_DB"] = str(state_env["db_file"])
    env["WORKFLOWS_FILE"] = str(state_env["wf_file"])
    env["TASKS_FILE"] = str(state_env["tasks_file"])
    env["CHECKPOINTS_DIR"] = str(state_env["cp_dir"])

    wid = "wf-cli-test-07"
    state_env["wf_file"].write_text(json.dumps({
        "workflows": {
            wid: {
                "workflow_id": wid,
                "title": "CLI 快照测试",
                "status": "running",
            }
        }
    }), encoding="utf-8")

    # 1. checkpoint-create
    r_create = subprocess.run(
        [str(task_bin), "checkpoint-create", wid, "--tag", "cli_tag_1"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r_create.returncode == 0
    assert "[CHECKPOINT_CREATED]" in r_create.stdout
    cpid = [w for w in r_create.stdout.split() if w.startswith("cp_")][0]

    # 2. checkpoint-list
    r_list = subprocess.run(
        [str(task_bin), "checkpoint-list", wid, "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r_list.returncode == 0
    cps = json.loads(r_list.stdout)
    assert len(cps) >= 1
    assert cps[0]["checkpoint_id"] == cpid

    # 3. checkpoint-restore
    r_restore = subprocess.run(
        [str(task_bin), "checkpoint-restore", wid, cpid],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r_restore.returncode == 0
    assert "[CHECKPOINT_RESTORED]" in r_restore.stdout

    # 4. checkpoint-fork
    forked_wid = "wf-cli-forked-07"
    r_fork = subprocess.run(
        [str(task_bin), "checkpoint-fork", cpid, forked_wid, "--title", "CLI分叉测试"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r_fork.returncode == 0
    assert "[WORKFLOW_FORKED]" in r_fork.stdout


def test_unified_state_db_extensions(state_env):
    """Verify task, workflow, and steering query extensions on state_db."""
    db = state_env["db_file"]
    state_db.init_db(db)

    # 1. Workflows listing & deletion
    state_db.save_workflow({"workflow_id": "wf-ext-1", "title": "WF Ext 1", "status": "running"}, db)
    state_db.save_workflow({"workflow_id": "wf-ext-2", "title": "WF Ext 2", "status": "completed"}, db)
    wfs = state_db.list_workflows(db_path=db)
    assert len(wfs) == 2
    assert {w["workflow_id"] for w in wfs} == {"wf-ext-1", "wf-ext-2"}
    wfs_completed = state_db.list_workflows(status="completed", db_path=db)
    assert len(wfs_completed) == 1
    assert wfs_completed[0]["workflow_id"] == "wf-ext-2"

    # 2. Tasks get, list, and auto-creation of placeholder workflow if needed
    t1 = {"task_id": "t-ext-1", "workflow_id": "wf-ext-1", "node": "dev", "status": "working"}
    t2 = {"task_id": "t-ext-2", "workflow_id": "wf-ext-1", "node": "test", "status": "pending"}
    # Standalone task with un-saved workflow should NOT fail foreign key:
    t3 = {"task_id": "t-ext-3", "workflow_id": "wf-standalone", "node": "plan", "status": "pending"}
    state_db.save_task(t1, db)
    state_db.save_task(t2, db)
    state_db.save_task(t3, db)

    fetched_t1 = state_db.get_task("t-ext-1", db)
    assert fetched_t1 is not None
    assert fetched_t1["node"] == "dev"
    assert fetched_t1["status"] == "working"

    all_tasks = state_db.list_tasks(db_path=db)
    assert len(all_tasks) == 3
    wf1_tasks = state_db.list_tasks(workflow_id="wf-ext-1", db_path=db)
    assert len(wf1_tasks) == 2
    working_tasks = state_db.list_tasks(status="working", db_path=db)
    assert len(working_tasks) == 1

    # 3. Steering CRUD & history
    steer_item = {
        "steer_id": "str-001",
        "task_id": "t-ext-1",
        "instruction": "Stop loop and write tests",
        "operator": "commander",
        "urgent": True,
        "status": "pending",
    }
    state_db.save_steer(steer_item, db)
    fetched_steer = state_db.get_steer("str-001", db)
    assert fetched_steer is not None
    assert fetched_steer["instruction"] == "Stop loop and write tests"
    assert fetched_steer["urgent"] is True
    assert fetched_steer["status"] == "pending"

    pending_steers = state_db.list_steers(task_id="t-ext-1", status="pending", db_path=db)
    assert len(pending_steers) == 1

    # Update steer status
    now_ts = time.time()
    updated = state_db.update_steer_status("str-001", "dispatched", dispatched_at=now_ts, db_path=db)
    assert updated is True
    fetched_steer2 = state_db.get_steer("str-001", db)
    assert fetched_steer2["status"] == "dispatched"
    assert fetched_steer2["dispatched_at"] == now_ts

    # Steering history
    state_db.record_steering_history({
        "action": "steer_dispatched",
        "workflow_id": "wf-ext-1",
        "task_id": "t-ext-1",
        "steer_id": "str-001",
        "instruction": "Stop loop and write tests",
        "operator": "commander",
    }, db)
    history = state_db.list_steering_history(task_id="t-ext-1", db_path=db)
    assert len(history) == 1
    assert history[0]["action"] == "steer_dispatched"

    events = state_db.list_events(task_id="t-ext-1", db_path=db)
    assert len(events) == 1
    assert events[0]["workflow_id"] == "wf-ext-1"
    assert events[0]["event_type"] == "steering.steer_dispatched"
    assert events[0]["source"] == "steering"
    assert events[0]["payload"]["steer_id"] == "str-001"


def test_steering_history_and_workflow_event_rollback_together(state_env, monkeypatch):
    """A failed WorkflowEvent append must rollback the steering audit row."""
    db = state_env["db_file"]
    state_db.init_db(db)

    def fail_record_event(*args, **kwargs):
        raise RuntimeError("simulated event append failure")

    monkeypatch.setattr(state_db, "record_event", fail_record_event)

    with pytest.raises(RuntimeError, match="simulated event append failure"):
        state_db.record_steering_history({
            "action": "steer_dispatched",
            "workflow_id": "wf-rollback",
            "task_id": "t-rollback",
            "steer_id": "str-rollback",
            "instruction": "must rollback as a unit",
            "operator": "commander",
        }, db)

    assert state_db.list_steering_history(task_id="t-rollback", db_path=db) == []

    conn = state_db.get_db_connection(db)
    try:
        event_count = conn.execute("SELECT COUNT(*) FROM events;").fetchone()[0]
    finally:
        conn.close()
    assert event_count == 0
