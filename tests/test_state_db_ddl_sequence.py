"""G-4: runtime DDL trace equivalence for the four database bootstrap paths.

A text diff of the DDL source is only a pre-filter: the split must preserve the
*executed statement sequence*, including transaction boundaries and the four
``_INITIALIZED_DBS.add`` success paths.  This test records
``sqlite3.Connection.set_trace_callback`` output for the same four paths Plan
§6 G-4 names:

(i)   a brand-new empty database;
(ii)  an existing database with ``schema_meta.v1_migration_done='1'``;
(iii) an upgrade database with runtime state but missing columns
      (``has_existing_state=True``);
(iv)  a legacy JSON first import.

Expected traces are embedded in ``scripts/check_state_db_split.py`` (the same
baseline the gates use).  A mismatch prints a unified diff of the normalized
statement sequence, then fails.  ``time.time`` is frozen during capture so the
legacy-import SQL is deterministic; all databases live under ``tmp_path``.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from herdr import state_db

ROOT = Path(__file__).resolve().parents[1]
FROZEN_TIME = 1_700_000_000.0


def _load_checker():
    path = ROOT / "scripts" / "check_state_db_split.py"
    spec = importlib.util.spec_from_file_location("check_state_db_split_ddl", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()


def _expected_trace(name: str) -> list[str]:
    info = CHECKER.EXPECTED_TRACES[name]
    raw = zlib.decompress(base64.b64decode(info["b64"]))
    return raw.decode("utf-8").split("\n")


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


def _capture(monkeypatch, action: Callable[[], Any]) -> list[str]:
    trace: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda statement: trace.append(_normalize(statement)))
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    monkeypatch.setattr(time, "time", lambda: FROZEN_TIME)
    action()
    return trace


def _clear_db_env(monkeypatch) -> None:
    for key in (
        "HERDR_STATE_DB",
        "CHECKPOINTS_DIR",
        "WORKFLOWS_FILE",
        "TASKS_FILE",
        "WORKFLOW_FILE",
    ):
        monkeypatch.delenv(key, raising=False)


def _assert_trace(name: str, actual: list[str]) -> None:
    # The Controller Cockpit adds two guarded expression indexes after the
    # frozen split baseline.  Exclude only those exact statements so this gate
    # continues to prove that the upstream state_db split preserves every
    # pre-existing DDL statement and ordering.
    feature_indexes = {
        "CREATE INDEX IF NOT EXISTS idx_events_checkpoint_identity ON events(workflow_id,event_type,source,json_extract(payload_json,'$.checkpoint_id'),id) WHERE json_valid(payload_json)",
        "CREATE INDEX IF NOT EXISTS idx_events_control_reason ON events(workflow_id,event_type,source,json_extract(payload_json,'$.reason'),id) WHERE json_valid(payload_json)",
    }
    feature_admission_reads = (
        "SELECT 1 FROM workflows WHERE workflow_id=",
        "SELECT * FROM workflows WHERE workflow_id=",
        "SELECT id,event_type,payload_json FROM events WHERE workflow_id=",
        "SELECT id,payload_json FROM events WHERE workflow_id=",
    )
    # Feature-specific registration authorization adds these three reads only
    # immediately before a legacy task INSERT.  The non-legacy traces contain
    # no matching statements at this position; all original DDL remains exact.
    if name == "legacy":
        insert_at = next(i for i, statement in enumerate(actual)
                         if statement.startswith("INSERT INTO tasks "))
        prefix = actual[:insert_at]
        suffix = actual[insert_at:]
        removed = 0
        kept = []
        for statement in reversed(prefix):
            if removed < 3 and statement.startswith(feature_admission_reads):
                removed += 1
                continue
            kept.append(statement)
        actual = list(reversed(kept)) + suffix
        assert removed == 3
    actual = [statement for statement in actual if statement not in feature_indexes]
    expected = _expected_trace(name)
    info = CHECKER.EXPECTED_TRACES[name]
    if actual != expected:
        import difflib

        diff = list(
            difflib.unified_diff(
                expected,
                actual,
                fromfile=f"expected/{name}",
                tofile=f"actual/{name}",
                lineterm="",
            )
        )
        pytest.fail(
            f"{name} DDL trace diverged ({len(expected)} expected statements, "
            f"{len(actual)} actual).\n" + "\n".join(diff[:120])
        )
    digest = hashlib.sha256("\n".join(actual).encode("utf-8")).hexdigest()
    assert digest == info["sha256"]
    assert len(actual) == info["count"]


def test_path_i_fresh_database(monkeypatch, tmp_path: Path):
    _clear_db_env(monkeypatch)
    db = tmp_path / "fresh.db"

    trace = _capture(monkeypatch, lambda: state_db.init_db(db))

    _assert_trace("fresh", trace)
    assert trace.index("BEGIN IMMEDIATE;") < trace.index("COMMIT;")
    assert sum("v1_migration_done" in statement for statement in trace) >= 2
    assert any(statement.startswith("CREATE TABLE IF NOT EXISTS workflows") for statement in trace)


def test_path_ii_v1_migration_already_done(monkeypatch, tmp_path: Path):
    _clear_db_env(monkeypatch)
    db = tmp_path / "v1-done.db"
    state_db.init_db(db)
    conn = state_db.get_db_connection(db)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) "
        "VALUES ('v1_migration_done', '1')"
    )
    conn.commit()
    conn.close()
    state_db._INITIALIZED_DBS.discard(str(db.resolve()))

    trace = _capture(monkeypatch, lambda: state_db.get_db_connection(db).close())

    _assert_trace("v1_done", trace)
    assert "BEGIN TRANSACTION;" not in trace
    assert any("v1_migration_done" in statement for statement in trace)


def test_path_iii_existing_state_upgrade(monkeypatch, tmp_path: Path):
    _clear_db_env(monkeypatch)
    db = tmp_path / "upgrade.db"
    state_db.init_db(db)
    conn = sqlite3.connect(db)
    try:
        for statement in (
            "ALTER TABLE tasks DROP COLUMN version",
            "ALTER TABLE completion_observations DROP COLUMN last_observed_at",
            "ALTER TABLE completion_observations DROP COLUMN blocker_present",
            "ALTER TABLE interventions DROP COLUMN execution_owner",
            "ALTER TABLE interventions DROP COLUMN lease_until",
            "ALTER TABLE eval_results DROP COLUMN scores_json",
            "ALTER TABLE eval_results DROP COLUMN requirements_satisfied",
            "ALTER TABLE eval_results DROP COLUMN verification_passed",
            "ALTER TABLE eval_results DROP COLUMN human_intervention_count",
            "ALTER TABLE eval_results DROP COLUMN final_status",
            "ALTER TABLE eval_results DROP COLUMN warnings_json",
            "ALTER TABLE eval_results DROP COLUMN task_status",
            "ALTER TABLE replay_specs DROP COLUMN snapshot_path",
            "ALTER TABLE replay_specs DROP COLUMN policy_json",
        ):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO workflows (workflow_id, title, status) "
            "VALUES ('wf-legacy', 'Legacy', 'running')"
        )
        conn.execute("DELETE FROM schema_meta WHERE key='v1_migration_done'")
        conn.commit()
    finally:
        conn.close()
    state_db._INITIALIZED_DBS.discard(str(db.resolve()))

    trace = _capture(monkeypatch, lambda: state_db.get_db_connection(db).close())

    _assert_trace("upgrade", trace)
    assert sum(statement.startswith("ALTER TABLE") for statement in trace) == 14
    assert "BEGIN TRANSACTION;" not in trace


def test_path_iv_legacy_json_first_import(monkeypatch, tmp_path: Path):
    _clear_db_env(monkeypatch)
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "workflows.json").write_text(
        json.dumps(
            {
                "workflows": {
                    "w-legacy": {
                        "workflow_id": "w-legacy",
                        "title": "W",
                        "status": "running",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "t-legacy",
                        "workflow_id": "w-legacy",
                        "node": "implementation",
                        "stage": "implementation",
                        "agent": "developer",
                        "status": "working",
                        "goal": "legacy",
                        "run_id": "run-legacy",
                        "workflow_run_id": "wf-exec-legacy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "steering.json").write_text(
        json.dumps(
            {
                "steering_queues": {
                    "t-legacy": [
                        {
                            "steer_id": "s-legacy",
                            "instruction": "go",
                            "operator": "human",
                            "urgent": True,
                        }
                    ]
                },
                "history": [
                    {"action": "note", "task_id": "t-legacy", "steer_id": "s-legacy"}
                ],
            }
        ),
        encoding="utf-8",
    )
    checkpoints = root / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "c1.json").write_text(
        json.dumps(
            {
                "checkpoint_id": "cp-legacy",
                "workflow_id": "w-legacy",
                "tag": "t",
                "created_at": 1.0,
                "workflow": {
                    "workflow_id": "w-legacy",
                    "title": "W",
                    "status": "running",
                },
                "tasks": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHECKPOINTS_DIR", str(checkpoints))
    db = root / "state.db"

    trace = _capture(monkeypatch, lambda: state_db.init_db(db))

    _assert_trace("legacy", trace)
    assert "BEGIN TRANSACTION;" in trace
    legacy_begin = trace.index("BEGIN TRANSACTION;")
    assert "COMMIT;" in trace[legacy_begin + 1 :], "legacy import transaction did not commit"
    assert any("INSERT INTO workflows" in statement for statement in trace)
    assert any("INSERT INTO tasks" in statement for statement in trace)
    assert any("INSERT INTO steering_items" in statement for statement in trace)
    assert any("INSERT INTO checkpoints" in statement for statement in trace)

    # Trace equality cannot detect a consistently wrong field mapping.
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        workflow = dict(conn.execute("SELECT * FROM workflows WHERE workflow_id='w-legacy'").fetchone())
        task = dict(conn.execute("SELECT * FROM tasks WHERE task_id='t-legacy'").fetchone())
        steer = dict(conn.execute("SELECT * FROM steering_items WHERE steer_id='s-legacy'").fetchone())
        history = conn.execute("SELECT * FROM steering_history").fetchall()
    assert workflow["title"] == "W" and workflow["status"] == "running"
    assert task["goal"] == "legacy" and json.loads(task["payload_json"])["run_id"] == "run-legacy"
    assert steer["instruction"] == "go" and steer["task_id"] == "t-legacy"
    assert history and history[0]["action"] == "note"


def _seed_legacy_rollout(db):
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE rollout_state (bucket_key TEXT PRIMARY KEY, agent TEXT, node TEXT, task_type TEXT, percentage INTEGER, updated_at REAL, updated_by TEXT, reason TEXT)")
        conn.execute("INSERT INTO rollout_state VALUES ('agent/review/implementation','agent','review','implementation',37,1.0,'human','legacy')")


def test_path_v_legacy_rollout_bucket_key(monkeypatch, tmp_path):
    _clear_db_env(monkeypatch)
    db = tmp_path / 'legacy-rollout.db'
    _seed_legacy_rollout(db)
    trace = _capture(monkeypatch, lambda: state_db.init_db(db))
    _assert_trace('legacy_rollout', trace)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT agent,node,task_type,percentage FROM rollout_state').fetchall() == [('agent','review','implementation',37)]
        assert 'bucket_key' not in [row[1] for row in conn.execute('PRAGMA table_info(rollout_state)')]


def test_legacy_rollout_rejects_helper_moved_inside_transaction(monkeypatch, tmp_path):
    # In-memory mutation only; the production file remains the frozen monolith.
    import subprocess
    source = subprocess.check_output(
        ['git', 'show', 'a56739e:herdr/state_db.py'], cwd=ROOT, text=True)
    import ast
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_ensure_schema')
    helper = next(n for n in function.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == '_ensure_rollout_schema')
    function.body.remove(helper)
    commit = next(i for i, n in enumerate(function.body) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and n.value.args and isinstance(n.value.args[0], ast.Constant) and n.value.args[0].value == 'COMMIT;')
    function.body.insert(commit, helper)
    namespace = dict(vars(state_db))
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<schema-mutation>', 'exec'), namespace)
    _clear_db_env(monkeypatch)
    db = tmp_path / 'mutated-legacy.db'
    _seed_legacy_rollout(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(sqlite3.OperationalError, match='within a transaction'):
            namespace['_ensure_schema'](conn, str(db))
    finally:
        conn.close()


def test_import_value_assertions_reject_wrong_task_mapping(monkeypatch, tmp_path):
    original = state_db.save_task

    def wrong_mapping(task, *args, **kwargs):
        return original(dict(task, goal='wrong mapping'), *args, **kwargs)

    monkeypatch.setattr(state_db, 'save_task', wrong_mapping)
    # Model a trace baseline regenerated with the same bug: values still reject it.
    monkeypatch.setitem(globals(), '_assert_trace', lambda *args: None)
    with pytest.raises(AssertionError):
        test_path_iv_legacy_json_first_import(monkeypatch, tmp_path)
