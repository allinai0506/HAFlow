"""Replay lineage tests (replay_specs table + lineage accessors).

TDD RED stage for Task 1: lineage must be traceable from replay runs back
to source runs without writing to the source run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herdr import state_db


@pytest.fixture
def replay_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    state_db._INITIALIZED_DBS.discard(str(db_path.resolve()))
    state_db.init_db(db_path)
    return db_path


def test_record_and_get_replay_spec(replay_db: Path):
    from herdr import eval_store

    spec = eval_store.record_replay_spec(
        "run-source-A", "run-replay-B",
        workflow_id="wf-1",
        definition={"nodes": []},
        db_path=replay_db,
    )
    assert spec["source_run_id"] == "run-source-A"
    assert spec["replay_run_id"] == "run-replay-B"

    fetched = eval_store.get_replay_spec("run-replay-B", db_path=replay_db)
    assert fetched is not None
    assert fetched["spec_id"] == spec["spec_id"]
    assert fetched["definition"] == {"nodes": []}


def test_replay_record_is_idempotent_on_replay_run(replay_db: Path):
    from herdr import eval_store

    first = eval_store.record_replay_spec("run-A", "run-B", db_path=replay_db)
    second = eval_store.record_replay_spec("run-A", "run-B", db_path=replay_db)
    assert second["spec_id"] == first["spec_id"]
    assert len(eval_store.list_replay_specs(db_path=replay_db)) == 1


def test_replay_lineage_chain(replay_db: Path):
    from herdr import eval_store

    eval_store.record_replay_spec("run-A", "run-B", db_path=replay_db)
    eval_store.record_replay_spec("run-B", "run-C", db_path=replay_db)
    lineage = eval_store.get_replay_lineage("run-C", db_path=replay_db)
    assert lineage == ["run-A", "run-B", "run-C"]


def test_replay_lineage_absent_returns_empty(replay_db: Path):
    from herdr import eval_store

    assert eval_store.get_replay_lineage("run-unknown", db_path=replay_db) == []
    assert eval_store.get_replay_spec("run-unknown", db_path=replay_db) is None


def test_replay_does_not_write_source_run(replay_db: Path):
    from herdr import eval_store

    before = state_db.list_trajectory_events("run-src", db_path=replay_db)
    eval_store.record_replay_spec("run-src", "run-dst", db_path=replay_db)
    after = state_db.list_trajectory_events("run-src", db_path=replay_db)
    assert before == after == []
    dst_events = state_db.list_trajectory_events("run-dst", db_path=replay_db)
    assert dst_events == []


def test_replay_rejects_self_source(replay_db: Path):
    from herdr import eval_store

    with pytest.raises(ValueError):
        eval_store.record_replay_spec("run-same", "run-same", db_path=replay_db)


def test_replay_null_semantics(replay_db: Path):
    from herdr import eval_store

    spec = eval_store.record_replay_spec(
        "run-n1", "run-n2", workflow_id=None, definition=None,
        lineage=None, db_path=replay_db,
    )
    assert spec["workflow_id"] is None
    assert spec["definition"] is None
    fetched = eval_store.get_replay_spec("run-n2", db_path=replay_db)
    assert fetched is not None
    assert fetched["workflow_id"] is None
    assert fetched["definition"] is None


def test_freeze_run_definition_and_register_workflow(replay_db: Path, tmp_path: Path):
    from herdr import projects
    from herdr.state_store import SQLiteStateStore

    store = SQLiteStateStore(db_path=replay_db)
    src = tmp_path / "workflow.json"
    src.write_text('{"nodes": [{"id": "dev"}]}', encoding="utf-8")

    frozen = projects.freeze_run_definition(
        "wf-freeze-1", source_file=str(src)
    )
    assert frozen is not None

    project = {
        "project_id": "proj-lineage",
        "project_name": "lineage",
        "project_root": str(tmp_path),
        "base_branch": "main",
        "workspace_id": "ws-1",
        "coordinator_pane_id": "pane-1",
        "workflow_file": str(src),
    }
    # workflow_file/metadata must persist via a single write.
    projects.register_workflow(
        "wf-freeze-1", project, requirement="req", title="t",
        workflow_file=frozen, metadata={"replay_of": "run-A"},
    )
    record = store.get_workflow("wf-freeze-1")
    assert record is not None
    assert record.get("workflow_file") == frozen
    assert record.get("replay_of") == "run-A"

    # Lineage stays queryable through the spec table.
    from herdr import eval_store

    eval_store.record_replay_spec("run-A", "run-B", db_path=replay_db)
    assert eval_store.get_replay_lineage("run-B", db_path=replay_db) == [
        "run-A", "run-B",
    ]


def test_state_store_delegates_replay(replay_db: Path):
    from herdr.state_store import SQLiteStateStore

    store = SQLiteStateStore(db_path=replay_db)
    store.record_replay_spec("run-SA", "run-SB")
    assert store.get_replay_spec("run-SB")["source_run_id"] == "run-SA"
    assert store.get_replay_lineage("run-SB") == ["run-SA", "run-SB"]
