"""Eval -> Replay -> Eval -> Compare E2E (T2/T3 RED).

Historical Run -> Eval -> Replay -> Eval -> Compare chain plus boundary
coverage: cross-run isolation, incomplete run, human attribution,
override, and frozen snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herdr import state_db


@pytest.fixture
def e2e_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    state_db._INITIALIZED_DBS.discard(str(db_path.resolve()))
    state_db.init_db(db_path)
    return db_path


def _history_run(db_path: Path):
    state_db.save_workflow(
        {"workflow_id": "wf-e2e", "title": "e2e", "status": "running",
         "gate_overrides": {"dev": {"verdict": "pass", "operator": "human"}}},
        db_path=db_path,
    )
    state_db.save_task(
        {"task_id": "t-hist", "workflow_id": "wf-e2e", "run_id": "run-hist",
         "node": "dev", "stage": "dev", "agent": "opencode",
         "status": "completed", "stage_verdict": "pass", "goal": "ship"},
        db_path=db_path,
    )
    state_db.save_steer(
        {"steer_id": "s-hist", "task_id": "t-hist",
         "instruction": "hold the line", "operator": "human"},
        db_path=db_path,
    )
    from herdr.trajectory import TrajectoryLedger

    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-hist", "task_id": "t-hist",
                         "workflow_id": "wf-e2e", "event_type": "task_started",
                         "timestamp": 10.0})
    ledger.append_event({"run_id": "run-hist", "task_id": "t-hist",
                         "workflow_id": "wf-e2e",
                         "event_type": "verification_completed",
                         "timestamp": 11.0,
                         "verification": {"passed": True,
                                          "evidence_id": "ev-hist"}})


def test_historical_run_to_compare_chain(e2e_db: Path, monkeypatch):
    from herdr import eval_engine, eval_store, replay_engine
    from herdr.trajectory import TrajectoryLedger

    launched = []

    def run_external(argv, timeout=120, db_path=None):
        launched.append(argv)
        if argv[:2] == ["herdr-task", "launch"]:
            options = dict(zip(argv[2::2], argv[3::2]))
            task = {
                "task_id": options["--task-id"],
                "workflow_id": options["--workflow-id"],
                "run_id": options["--run-id"],
                "node": options["--node"], "stage": options["--node"],
                "agent": options["--agent"], "status": "working",
                "goal": options["--goal"], "prompt": options["--prompt"],
                "replay_of": options["--replay-of"],
            }
            state_db.save_task(task, db_path=e2e_db)
            ledger = TrajectoryLedger(e2e_db)
            for event_type in ("run_started", "task_started"):
                ledger.append_event({
                    "run_id": task["run_id"], "task_id": task["task_id"],
                    "workflow_id": task["workflow_id"], "event_type": event_type,
                })
        return {"argv": argv, "ok": True}

    monkeypatch.setattr(replay_engine, "_run_probe_argv", run_external)

    _history_run(e2e_db)

    before = eval_engine.record_run_eval("run-hist", db_path=e2e_db)
    assert before["requirements_satisfied"] is True
    assert before["verification_passed"] is True

    # Incomplete run stays null and never guesses.
    state_db.save_task(
        {"task_id": "t-part", "workflow_id": "wf-e2e", "run_id": "run-part",
         "node": "dev", "stage": "dev", "agent": "opencode",
         "status": "working", "goal": "ship"},
        db_path=e2e_db,
    )
    from herdr.trajectory import TrajectoryLedger as _E2ELedger

    _E2ELedger(e2e_db).append_event(
        {"run_id": "run-part", "task_id": "t-part",
         "workflow_id": "wf-e2e", "event_type": "task_started",
         "timestamp": 12.0})
    partial = eval_engine.evaluate_run("run-part", db_path=e2e_db)
    assert partial["requirements_satisfied"] is None
    assert "run_incomplete" in partial["warnings"]

    # Replay the historical run with a frozen snapshot.
    out = replay_engine.replay_run(
        "run-hist", definition={"nodes": [{"id": "dev"}]},
        db_path=e2e_db,
    )
    assert [argv[0] for argv in launched] == ["herdr-preflight", "herdr-task"]
    replay_run_id = out["spec"]["replay_run_id"]
    assert Path(str(out["snapshot"])).exists()
    assert eval_store.get_replay_lineage(
        replay_run_id, db_path=e2e_db) == ["run-hist", replay_run_id]
    replay_task = state_db.get_task(out["task_id"], db_path=e2e_db)
    assert replay_task["run_id"] == replay_run_id
    assert replay_task["replay_of"] == "run-hist"

    # Replay eval must not see source-run steering (cross-run isolation).
    replay_eval = eval_engine.evaluate_run(replay_run_id, db_path=e2e_db)
    assert replay_eval["human_intervention_count"] == 0
    assert replay_eval["task_id"] is not None
    assert replay_eval["task_id"] != "t-hist"

    # Record the replay eval and compare factually.
    stored_replay = eval_engine.record_run_eval(replay_run_id, db_path=e2e_db)
    diff = eval_engine.compare_evals(before, stored_replay)
    assert diff["before"]["requirements_satisfied"] is True
    assert diff["after"]["final_status"] == "working"
    assert set(diff) == {"before", "after"}

    # Source history is unchanged by replay and eval writes.
    assert state_db.get_task("t-hist", db_path=e2e_db)["status"] == "completed"
    assert len(state_db.list_trajectory_events(
        "run-hist", db_path=e2e_db)) == 2
