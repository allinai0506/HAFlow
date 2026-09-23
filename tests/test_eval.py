"""Eval persistence tests (eval_results table + eval_store accessors).

TDD RED stage for Task 1: these tests define the persistence contract and
initially fail because tables/accessors do not exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from herdr import state_db


@pytest.fixture
def eval_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    state_db._INITIALIZED_DBS.discard(str(db_path.resolve()))
    state_db.init_db(db_path)
    return db_path


def _tables(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0] for row in rows}


def test_eval_tables_exist_without_foreign_keys(eval_db: Path):
    from herdr import eval_store  # noqa: F401  (must exist)

    conn = state_db.get_db_connection(eval_db)
    try:
        tables = _tables(conn)
        assert "eval_results" in tables
        assert "replay_specs" in tables
        # No foreign keys from eval tables (deleting a workflow keeps eval rows).
        for table in ("eval_results", "replay_specs"):
            fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert list(fks) == []
        # Expected indexes exist.
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        idx_names = {row[0] for row in idx_rows}
        assert "idx_eval_results_run_revision" in idx_names
        assert "idx_replay_specs_replay" in idx_names
    finally:
        conn.close()


def test_record_and_get_eval_result_roundtrip(eval_db: Path):
    from herdr import eval_store

    stored = eval_store.record_eval_result(
        "run-eval-1",
        revision=1,
        evidence=[{"kind": "verification", "ref": "evt_1"}],
        requirements_satisfied=True,
        verification_passed=True,
        human_intervention_count=2,
        final_status="completed",
        warnings=["reviewed"],
        db_path=eval_db,
    )
    assert stored["run_id"] == "run-eval-1"
    assert stored["revision"] == 1
    assert stored["requirements_satisfied"] is True
    assert stored["verification_passed"] is True
    assert stored["human_intervention_count"] == 2
    assert stored["final_status"] == "completed"
    assert stored["warnings"] == ["reviewed"]
    assert "verdict" not in stored and "scores" not in stored

    fetched = eval_store.get_eval_result("run-eval-1", 1, db_path=eval_db)
    assert fetched is not None
    assert fetched["eval_id"] == stored["eval_id"]
    assert fetched["requirements_satisfied"] is True
    assert fetched["warnings"] == ["reviewed"]


def test_eval_record_is_idempotent_on_run_revision(eval_db: Path):
    from herdr import eval_store

    first = eval_store.record_eval_result(
        "run-idem", revision=1, db_path=eval_db
    )
    second = eval_store.record_eval_result(
        "run-idem", revision=1, db_path=eval_db
    )
    assert second["eval_id"] == first["eval_id"]
    rows = eval_store.list_eval_results("run-idem", db_path=eval_db)
    assert len(rows) == 1


def test_eval_auto_revision_allocates_max_plus_one(eval_db: Path):
    from herdr import eval_store

    eval_store.record_eval_result("run-rev", revision=1, db_path=eval_db)
    eval_store.record_eval_result("run-rev", revision=2, db_path=eval_db)
    auto = eval_store.record_eval_result("run-rev", db_path=eval_db)
    assert auto["revision"] == 3
    assert eval_store.get_max_eval_revision("run-rev", db_path=eval_db) == 3
    latest = eval_store.get_latest_eval_result("run-rev", db_path=eval_db)
    assert latest is not None and latest["revision"] == 3


def test_eval_store_rejects_non_boolean_fact_values(eval_db: Path):
    from herdr import eval_store

    with pytest.raises(ValueError, match="verification_passed must be a bool"):
        eval_store.record_eval_result(
            "run-loose-fact", verification_passed=1, db_path=eval_db)


def test_eval_absent_returns_none(eval_db: Path):
    from herdr import eval_store

    assert eval_store.get_eval_result("run-absent", 1, db_path=eval_db) is None
    assert eval_store.get_latest_eval_result("run-absent", db_path=eval_db) is None
    assert eval_store.get_max_eval_revision("run-absent", db_path=eval_db) is None
    assert eval_store.list_eval_results("run-absent", db_path=eval_db) == []


def test_eval_malformed_json_degrades_to_none(eval_db: Path):
    from herdr import eval_store

    stored = eval_store.record_eval_result(
        "run-malformed", revision=1,
        evidence=[{"kind": "verification", "ref": "x"}], db_path=eval_db,
    )
    conn = state_db.get_db_connection(eval_db)
    try:
        conn.execute(
            "UPDATE eval_results SET evidence_json = 'not-json{' WHERE eval_id = ?",
            (stored["eval_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    fetched = eval_store.get_eval_result("run-malformed", 1, db_path=eval_db)
    assert fetched is not None
    assert fetched["evidence"] is None


def test_eval_null_semantics_preserved(eval_db: Path):
    from herdr import eval_store

    stored = eval_store.record_eval_result(
        "run-null", revision=1, evidence=None, requirements_satisfied=None,
        verification_passed=None, human_intervention_count=None,
        final_status=None, db_path=eval_db,
    )
    assert stored["evidence"] is None
    for field in ("requirements_satisfied", "verification_passed",
                  "human_intervention_count", "final_status"):
        assert stored[field] is None
    fetched = eval_store.get_eval_result("run-null", 1, db_path=eval_db)
    assert fetched is not None
    assert fetched["evidence"] is None
    for field in ("requirements_satisfied", "verification_passed",
                  "human_intervention_count", "final_status"):
        assert fetched[field] is None


def test_delete_workflow_retains_eval_rows(eval_db: Path):
    from herdr import eval_store

    state_db.save_workflow(
        {"workflow_id": "wf-eval-keep", "title": "keep", "status": "running"},
        db_path=eval_db,
    )
    eval_store.record_eval_result(
        "run-keep", revision=1, requirements_satisfied=True, db_path=eval_db
    )
    assert state_db.delete_workflow("wf-eval-keep", db_path=eval_db) is True
    # Eval rows have no FK to workflows and must survive workflow deletion.
    assert eval_store.get_eval_result("run-keep", 1, db_path=eval_db) is not None


def test_latest_run_verification_fact_strict(eval_db: Path):
    from herdr import eval_store
    from herdr.trajectory import TrajectoryLedger

    # Absent run has no verification fact.
    assert eval_store.latest_run_verification_fact("run-nope", db_path=eval_db) is None

    ledger = TrajectoryLedger(eval_db)
    ledger.append_event({
        "run_id": "run-fact", "task_id": "t-1", "workflow_id": "wf-1",
        "event_type": "verification_completed", "timestamp": 10.0,
        "verification": {"passed": True, "evidence_id": "ev-1"},
    })
    fact = eval_store.latest_run_verification_fact("run-fact", db_path=eval_db)
    assert fact is not None
    assert fact["passed"] is True
    assert fact["run_id"] == "run-fact"

    # Loose variant: passed missing must NOT be coerced to False.
    ledger.append_event({
        "run_id": "run-loose", "task_id": "t-1", "workflow_id": "wf-1",
        "event_type": "verification_completed", "timestamp": 11.0,
        "verification": {"evidence_id": "ev-x"},
    })
    assert eval_store.latest_run_verification_fact("run-loose", db_path=eval_db) is None


def test_latest_run_verification_fact_malformed_returns_none(eval_db: Path):
    from herdr import eval_store
    from herdr.trajectory import TrajectoryLedger

    ledger = TrajectoryLedger(eval_db)
    ledger.append_event({
        "run_id": "run-corrupt", "event_type": "verification_completed",
        "verification": {"passed": True}, "timestamp": 5.0,
    })
    conn = state_db.get_db_connection(eval_db)
    try:
        conn.execute(
            "UPDATE events SET payload_json = 'not-json{' WHERE run_id = ?",
            ("run-corrupt",),
        )
        conn.commit()
    finally:
        conn.close()
    assert eval_store.latest_run_verification_fact("run-corrupt", db_path=eval_db) is None


def test_eval_store_rejects_observer_loose_imports():
    import pathlib

    src = pathlib.Path("herdr/eval_store.py").read_text(encoding="utf-8")
    assert "from herdr.observer" not in src
    assert "from .observer" not in src
    assert "observer.signals" not in src
    assert "from herdr.decision" not in src
    assert "from .decision" not in src
    assert "import metrics" not in src
    assert "from herdr.metrics" not in src
    assert "sync_" not in src or "sync_projection" not in src


def test_state_store_delegates_eval(eval_db: Path):
    from herdr.state_store import SQLiteStateStore

    store = SQLiteStateStore(db_path=eval_db)
    stored = store.record_eval_result("run-store", revision=1,
                                      requirements_satisfied=True)
    assert store.get_eval_result("run-store", 1)["eval_id"] == stored["eval_id"]
    assert store.get_latest_eval_result("run-store")["revision"] == 1
    assert store.get_max_eval_revision("run-store") == 1
    assert len(store.list_eval_results("run-store")) == 1
