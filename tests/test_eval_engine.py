"""Eval Engine contract tests (T2 RED).

Ownership, null semantics, human attribution, and override authority.
Initially fails: herdr.eval_engine does not exist.
"""

from __future__ import annotations

import sqlite3
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
    _save_task(db_path, task_id, run_id, status="completed", acceptance_verdict=True)
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

    task_id, run_id = "t-eng-pass", "run-eng-pass"
    _save_task(engine_db, task_id, run_id, status="completed")
    _append(engine_db, run_id, "task_started", task_id=task_id)
    _append(engine_db, run_id, "verification_completed", task_id=task_id,
            verification={"passed": True, "evidence_id": "ev-1"})
    result = eval_engine.evaluate_run(run_id, db_path=engine_db)
    assert result["run_id"] == run_id
    assert result["task_id"] == task_id
    assert result["requirements_satisfied"] is None
    assert result["final_status"] == "completed"
    assert result["verification_passed"] is True
    assert isinstance(result["evidence"], list) and result["evidence"]


def test_acceptance_fact_is_independent_of_verification(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-accept", "run-accept", status="completed",
               acceptance_verdict=True)
    _append(engine_db, "run-accept", "task_started", task_id="t-accept")
    _append(engine_db, "run-accept", "verification_completed", task_id="t-accept",
            verification={"passed": False})
    result = eval_engine.evaluate_run("run-accept", db_path=engine_db)
    assert result["requirements_satisfied"] is True
    assert result["verification_passed"] is False


def test_failed_verification_does_not_imply_requirements_false(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-fail-ver", "run-fail-ver", status="completed")
    _append(engine_db, "run-fail-ver", "task_started", task_id="t-fail-ver")
    _append(engine_db, "run-fail-ver", "verification_completed", task_id="t-fail-ver",
            verification={"passed": False})
    result = eval_engine.evaluate_run("run-fail-ver", db_path=engine_db)
    assert result["requirements_satisfied"] is None
    assert result["verification_passed"] is False


def test_evaluate_null_when_verification_missing(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-no-ver", "run-no-ver", status="completed")
    _append(engine_db, "run-no-ver", "task_started", task_id="t-no-ver")
    result = eval_engine.evaluate_run("run-no-ver", db_path=engine_db)
    assert result["requirements_satisfied"] is None
    assert "insufficient_verification" in result["warnings"]
    assert result["verification_passed"] is None


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
    assert result["requirements_satisfied"] is None
    assert "run_incomplete" in result["warnings"]
    assert result["verification_passed"] is True


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
    assert result["requirements_satisfied"] is None
    assert result["human_intervention_count"] is None
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
    assert first["human_intervention_count"] == 1


def test_eval_ignores_verdict_annotations_and_uses_verification_fact(engine_db: Path):
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
    assert result["requirements_satisfied"] is True
    assert "verdict" not in result and "override" not in result


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
    assert result["requirements_satisfied"] is None


def test_loose_verification_never_coerced(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-loose", "run-loose", status="completed")
    _append(engine_db, "run-loose", "task_started", task_id="t-loose")
    _append(
        engine_db, "run-loose", "verification_completed", task_id="t-loose",
        verification={"evidence_id": "ev-x"},
    )
    result = eval_engine.evaluate_run("run-loose", db_path=engine_db)
    assert result["verification_passed"] is None
    assert result["requirements_satisfied"] is None
    assert "insufficient_verification" in result["warnings"]


def test_record_run_eval_persists_via_eval_store(engine_db: Path):
    from herdr import eval_engine, eval_store

    _completed_pass_run(engine_db, run_id="run-rec", task_id="t-rec")
    stored = eval_engine.record_run_eval("run-rec", db_path=engine_db)
    assert stored["run_id"] == "run-rec"
    assert stored["requirements_satisfied"] is True
    fetched = eval_store.get_latest_eval_result("run-rec", db_path=engine_db)
    assert fetched is not None
    assert fetched["eval_id"] == stored["eval_id"]


def test_eval_result_contains_only_authoritative_fact_fields(engine_db: Path):
    from herdr import eval_engine

    run_id, _ = _completed_pass_run(engine_db, run_id="run-facts", task_id="t-facts")
    result = eval_engine.evaluate_run(run_id, db_path=engine_db)
    assert result["requirements_satisfied"] is True
    assert result["verification_passed"] is True
    assert result["human_intervention_count"] == 0
    assert result["final_status"] == "completed"
    assert not {"verdict", "scores", "task_status", "steering",
                "observed_stage_verdict", "task_completed"} & result.keys()


def test_eval_incomplete_run_keeps_partial_facts_without_guessing(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-partial", "run-partial", status="working")
    _append(engine_db, "run-partial", "task_started", task_id="t-partial")
    _append(engine_db, "run-partial", "verification_completed",
            task_id="t-partial", verification={"passed": True})
    result = eval_engine.evaluate_run("run-partial", db_path=engine_db)
    assert result["verification_passed"] is True
    assert result["requirements_satisfied"] is None
    assert result["final_status"] == "working"
    assert "run_incomplete" in result["warnings"]


def test_eval_corrupt_verification_degrades_to_unknown(engine_db: Path):
    from herdr import eval_engine

    _save_task(engine_db, "t-corrupt-eval", "run-corrupt-eval", status="completed")
    _append(engine_db, "run-corrupt-eval", "task_started", task_id="t-corrupt-eval")
    _append(engine_db, "run-corrupt-eval", "verification_completed",
            task_id="t-corrupt-eval", verification={"passed": True})
    conn = state_db.get_db_connection(engine_db)
    try:
        conn.execute(
            "UPDATE events SET payload_json = 'not-json{' WHERE run_id = ?",
            ("run-corrupt-eval",),
        )
        conn.commit()
    finally:
        conn.close()

    result = eval_engine.evaluate_run("run-corrupt-eval", db_path=engine_db)
    assert result["verification_passed"] is None
    assert result["requirements_satisfied"] is None
    assert "insufficient_verification" in result["warnings"]


def test_eval_steering_read_failure_keeps_intervention_count_unknown(
    engine_db: Path, monkeypatch,
):
    from herdr import eval_engine

    _completed_pass_run(engine_db, run_id="run-steering-unavailable",
                        task_id="t-steering-unavailable")

    def unavailable(*, task_id, db_path=None):
        raise sqlite3.OperationalError("temporary read failure")

    monkeypatch.setattr(state_db, "list_steers", unavailable)
    result = eval_engine.evaluate_run("run-steering-unavailable", db_path=engine_db)
    assert result["human_intervention_count"] is None
    assert "steering_unavailable" in result["warnings"]


def test_eval_record_round_trip_preserves_facts_and_warnings(engine_db: Path):
    from herdr import eval_engine, eval_store

    _completed_pass_run(engine_db, run_id="run-roundtrip", task_id="t-roundtrip")
    state_db.save_steer(
        {"steer_id": "s-roundtrip", "task_id": "t-roundtrip",
         "instruction": "review", "operator": "human"}, db_path=engine_db)
    stored = eval_engine.record_run_eval("run-roundtrip", db_path=engine_db)
    fetched = eval_store.get_latest_eval_result("run-roundtrip", db_path=engine_db)
    assert fetched is not None
    for field in ("requirements_satisfied", "verification_passed",
                  "human_intervention_count", "final_status", "warnings"):
        assert fetched[field] == stored[field]
    assert fetched["human_intervention_count"] == 1
    assert "scores" not in fetched and "verdict" not in fetched


def test_eval_engine_rejects_observer_decision_metrics_imports():
    src = Path("herdr/eval_engine.py").read_text(encoding="utf-8")
    assert "from herdr.observer" not in src
    assert "from .observer" not in src
    assert "from herdr.decision" not in src
    assert "from .decision" not in src
    assert "from herdr.metrics" not in src
    assert "from .metrics" not in src
    assert "from herdr.intervention" not in src
