"""Eval Engine contract tests (T2 RED).

Ownership, null semantics, human attribution, and override authority.
Initially fails: herdr.eval_engine does not exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herdr import state_db


@pytest.fixture
def engine_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    state_db._INITIALIZED_DBS.discard(str(db_path.resolve()))
    state_db.init_db(db_path)
    return db_path


def _save_task(db_path: Path, task_id: str, run_id: str, **overrides):
    task = {
        "task_id": task_id,
        "workflow_id": overrides.get("workflow_id", "wf-eng-1"),
        "run_id": run_id,
        "node": overrides.get("node", "dev"),
        "stage": overrides.get("node", "dev"),
        "agent": overrides.get("agent", "opencode"),
        "status": overrides.get("status", "completed"),
        "stage_verdict": overrides.get("stage_verdict", "pass"),
        "goal": "ship it",
    }
    task.update({k: v for k, v in overrides.items() if k not in task})
    state_db.save_task(task, db_path=db_path)
    return task


def _append(db_path: Path, run_id: str, event_type: str, **fields):
    from herdr.trajectory import TrajectoryLedger

    ledger = TrajectoryLedger(db_path)
    payload: dict = {
        "run_id": run_id,
        "task_id": fields.pop("task_id", None),
        "workflow_id": fields.pop("workflow_id", "wf-eng-1"),
        "event_type": event_type,
        "timestamp": fields.pop("timestamp", 10.0),
    }
    payload.update(fields)
    return ledger.append_event(payload)


def _completed_pass_run(db_path: Path, run_id="run-eng-pass", task_id="t-eng-pass"):
    _save_task(db_path, task_id, run_id, status="completed", stage_verdict="pass")
    _append(db_path, run_id, "task_started", task_id=task_id)
    _append(
        db_path,
        run_id,
        "verification_completed",
        task_id=task_id,
        verification={"passed": True, "evidence_id": "ev-1"},
    )
    return run_id, task_id


def test_evaluate_pass_collects_authoritative_facts(engine_db: Path):
    from herdr import eval_engine

    run_id, task_id = _completed_pass_run(engine_db)
    result = eval_engine.evaluate_run(run_id, db_path=engine_db)
    assert result["run_id"] == run_id
    assert result["task_id"] == task_id
    assert result["verdict"] == "pass"
    assert result["task_completed"] is True
    assert result["verification"] is not None
    assert result["verification"]["passed"] is True
    assert result["observed_stage_verdict"] == "pass"
    assert isinstance(result["evidence"], list) and result["evidence"]


def test_evaluate_null_when_verification_missing(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-no-ver", "run-no-ver", status="completed")
    _append(engine_db, "run-no-ver", "task_started", task_id="t-no-ver")
    result = eval_engine.evaluate_run("run-no-ver", db_path=engine_db)
    assert result["verdict"] is None
    assert "insufficient_verification" in result["warnings"]
    assert result["verification"] is None


def test_evaluate_null_when_run_incomplete(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-working", "run-working", status="working")
    _append(engine_db, "run-working", "task_started", task_id="t-working")
    _append(
        engine_db,
        "run-working",
        "verification_completed",
        task_id="t-working",
        verification={"passed": True},
    )
    result = eval_engine.evaluate_run("run-working", db_path=engine_db)
    assert result["verdict"] is None
    assert "run_incomplete" in result["warnings"]
    assert result["task_completed"] is False


def test_ownership_mismatch_never_leaks_foreign_task(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-owned-a", "run-A", status="completed")
    # Run B reuses task A's id in its own trajectory; engine must not
    # attribute A's task identity or status to run B.
    _append(engine_db, "run-B", "task_started", task_id="t-owned-a")
    _append(
        engine_db,
        "run-B",
        "verification_completed",
        task_id="t-owned-a",
        verification={"passed": True},
    )
    result = eval_engine.evaluate_run("run-B", db_path=engine_db)
    assert result["task_id"] is None
    assert result["workflow_id"] is None
    assert result["verdict"] is None
    assert "task_ownership_mismatch" in result["warnings"]


def test_human_attribution_counts_only_owned_task(engine_db: Path):
    from herdr import eval_engine

    _completed_pass_run(engine_db, run_id="run-R1", task_id="t-R1")
    _completed_pass_run(engine_db, run_id="run-R2", task_id="t-R2")
    state_db.save_steer(
        {"steer_id": "s-R1", "task_id": "t-R1", "instruction": "fix",
         "operator": "human"},
        db_path=engine_db,
    )
    state_db.save_steer(
        {"steer_id": "s-R2", "task_id": "t-R2", "instruction": "fix",
         "operator": "human"},
        db_path=engine_db,
    )
    first = eval_engine.evaluate_run("run-R1", db_path=engine_db)
    assert first["steering"]["total"] == 1
    assert first["steering"]["human"] == 1
    assert first["steering"]["task_id"] == "t-R1"


def test_gate_override_is_authoritative_over_note(engine_db: Path):
    from herdr import eval_engine

    state_db.save_workflow(
        {"workflow_id": "wf-eng-1", "title": "t", "status": "running",
         "gate_overrides": {"dev": {"verdict": "pass", "operator": "human"}}},
        db_path=engine_db,
    )
    task = _save_task(
        engine_db, "t-ovr", "run-ovr", status="completed",
        stage_verdict="pass", stage_verdict_note="stale note",
    )
    task["stage_verdict_note"] = "stale note"
    _append(engine_db, "run-ovr", "task_started", task_id="t-ovr")
    _append(
        engine_db, "run-ovr", "verification_completed", task_id="t-ovr",
        verification={"passed": True},
    )
    result = eval_engine.evaluate_run("run-ovr", db_path=engine_db)
    assert result["override"] is not None
    assert result["override"]["verdict"] == "pass"
    assert result["override"]["operator"] == "human"


def test_stage_verdict_empty_normalizes_to_null(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-empty", "run-empty", status="completed",
               stage_verdict="")
    _append(engine_db, "run-empty", "task_started", task_id="t-empty")
    _append(
        engine_db, "run-empty", "verification_completed", task_id="t-empty",
        verification={"passed": True},
    )
    result = eval_engine.evaluate_run("run-empty", db_path=engine_db)
    assert result["observed_stage_verdict"] is None


def test_loose_verification_never_coerced(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-loose", "run-loose", status="completed")
    _append(engine_db, "run-loose", "task_started", task_id="t-loose")
    _append(
        engine_db, "run-loose", "verification_completed", task_id="t-loose",
        verification={"evidence_id": "ev-x"},
    )
    result = eval_engine.evaluate_run("run-loose", db_path=engine_db)
    assert result["verification"] is None
    assert result["verdict"] is None
    assert "insufficient_verification" in result["warnings"]


def test_record_run_eval_persists_via_eval_store(engine_db: Path):
    from herdr import eval_engine, eval_store

    _completed_pass_run(engine_db, run_id="run-rec", task_id="t-rec")
    stored = eval_engine.record_run_eval("run-rec", db_path=engine_db)
    assert stored["run_id"] == "run-rec"
    assert stored["verdict"] == "pass"
    fetched = eval_store.get_latest_eval_result("run-rec", db_path=engine_db)
    assert fetched is not None
    assert fetched["eval_id"] == stored["eval_id"]


def test_eval_engine_rejects_observer_decision_metrics_imports():
    src = Path("herdr/eval_engine.py").read_text(encoding="utf-8")
    assert "from herdr.observer" not in src
    assert "from .observer" not in src
    assert "from herdr.decision" not in src
    assert "from .decision" not in src
    assert "from herdr.metrics" not in src
    assert "from .metrics" not in src
    assert "from herdr.intervention" not in src
