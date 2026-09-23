"""Replay Engine contract tests (T3 RED).

Frozen snapshot, lineage, source immutability, dry-run purity, P2 guard.
Initially fails: herdr.replay_engine does not exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herdr import state_db


@pytest.fixture
def replay_engine_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    state_db._INITIALIZED_DBS.discard(str(db_path.resolve()))
    state_db.init_db(db_path)
    return db_path


def _source_run(db_path: Path, run_id="run-src-1", task_id="t-src-1",
                status="completed", workflow_id="wf-replay-src"):
    state_db.save_workflow(
        {"workflow_id": workflow_id, "title": "src", "status": "running"},
        db_path=db_path,
    )
    state_db.save_task(
        {"task_id": task_id, "workflow_id": workflow_id, "run_id": run_id,
         "node": "dev", "stage": "dev", "agent": "opencode",
         "status": status, "stage_verdict": "pass", "goal": "ship"},
        db_path=db_path,
    )
    from herdr.trajectory import TrajectoryLedger

    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": run_id, "task_id": task_id,
                         "workflow_id": workflow_id,
                         "event_type": "task_started", "timestamp": 10.0})
    ledger.append_event({"run_id": run_id, "task_id": task_id,
                         "workflow_id": workflow_id,
                         "event_type": "verification_completed",
                         "timestamp": 11.0,
                         "verification": {"passed": True}})
    return run_id, task_id, workflow_id


def test_replay_creates_spec_freeze_and_lineage(replay_engine_db: Path):
    from herdr import eval_store, replay_engine

    src, _, _ = _source_run(replay_engine_db)
    out = replay_engine.replay_run(
        src,
        definition={"nodes": [{"id": "dev"}]},
        db_path=replay_engine_db,
    )
    assert out["spec"]["source_run_id"] == src
    assert out["spec"]["replay_run_id"] != src
    assert Path(str(out["snapshot"])).exists()
    lineage = eval_store.get_replay_lineage(
        out["spec"]["replay_run_id"], db_path=replay_engine_db)
    assert lineage == [src, out["spec"]["replay_run_id"]]
    workflow = state_db.get_workflow(out["workflow_id"], db_path=replay_engine_db)
    assert workflow is not None
    assert workflow.get("replay_of") == src


def test_replay_does_not_mutate_source(replay_engine_db: Path):
    from herdr import replay_engine

    src, task_id, _ = _source_run(replay_engine_db)
    before_task = state_db.get_task(task_id, db_path=replay_engine_db)
    before_events = state_db.list_trajectory_events(src, db_path=replay_engine_db)
    out = replay_engine.replay_run(
        src, definition={"nodes": [{"id": "dev"}]},
        db_path=replay_engine_db,
    )
    assert state_db.get_task(task_id, db_path=replay_engine_db) == before_task
    assert state_db.list_trajectory_events(
        src, db_path=replay_engine_db) == before_events
    assert out["spec"]["replay_run_id"] != src


def test_dry_run_writes_nothing(replay_engine_db: Path):
    from herdr import eval_store, replay_engine

    src, _, _ = _source_run(replay_engine_db)
    before_specs = eval_store.list_replay_specs(db_path=replay_engine_db)
    before_workflows = state_db.list_workflows(db_path=replay_engine_db)
    out = replay_engine.replay_run(
        src, definition={"nodes": [{"id": "dev"}]},
        dry_run=True, db_path=replay_engine_db,
    )
    assert out["dry_run"] is True
    assert out["snapshot"] is None
    assert eval_store.list_replay_specs(
        db_path=replay_engine_db) == before_specs
    assert state_db.list_workflows(
        db_path=replay_engine_db) == before_workflows


def test_rejects_active_source_run(replay_engine_db: Path):
    from herdr import eval_store, replay_engine

    src, _, _ = _source_run(
        replay_engine_db, run_id="run-active", task_id="t-active",
        status="working",
    )
    with pytest.raises(ValueError):
        replay_engine.replay_run(
            src, definition={"nodes": []}, db_path=replay_engine_db)
    assert eval_store.list_replay_specs(
        db_path=replay_engine_db) == []


def test_rejects_self_source(replay_engine_db: Path):
    from herdr import replay_engine

    src, _, _ = _source_run(replay_engine_db)
    with pytest.raises(ValueError):
        replay_engine.replay_run(
            src, replay_run_id=src, definition={"nodes": []},
            db_path=replay_engine_db)


def test_frozen_snapshot_content_matches_definition(replay_engine_db: Path):
    import json

    from herdr import replay_engine

    src, _, _ = _source_run(replay_engine_db)
    definition = {"nodes": [{"id": "dev", "agent": "opencode"}]}
    out = replay_engine.replay_run(
        src, definition=definition, db_path=replay_engine_db)
    snapshot = Path(str(out["snapshot"]))
    assert json.loads(snapshot.read_text(encoding="utf-8")) == definition


def test_replay_engine_rejects_forbidden_imports():
    src = Path("herdr/replay_engine.py").read_text(encoding="utf-8")
    assert "from herdr.observer" not in src
    assert "from herdr.decision" not in src
    assert "from herdr.intervention" not in src
    assert "fork_workflow_from_checkpoint" not in src
