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


@pytest.fixture(autouse=True)
def mock_external_replay_commands(replay_engine_db: Path, monkeypatch):
    from herdr import replay_engine
    from herdr.trajectory import TrajectoryLedger

    def run_command(argv, timeout=120, db_path=None):
        if argv[:2] != ["herdr-task", "launch"]:
            return {"argv": argv, "ok": True}
        options = dict(zip(argv[2::2], argv[3::2]))
        task = {
            "task_id": options["--task-id"],
            "workflow_id": options["--workflow-id"],
            "run_id": options["--run-id"],
            "node": options["--node"],
            "stage": options["--node"],
            "agent": options["--agent"],
            "status": "working",
            "goal": options["--goal"],
            "prompt": options["--prompt"],
            "replay_of": options["--replay-of"],
            "agent_policy": __import__("json").loads(options["--agent-policy"]),
        }
        state_db.save_task(task, db_path=replay_engine_db)
        ledger = TrajectoryLedger(replay_engine_db)
        for event_type in ("run_started", "task_started"):
            ledger.append_event({
                "run_id": task["run_id"], "task_id": task["task_id"],
                "workflow_id": task["workflow_id"], "event_type": event_type,
            })
        return {"argv": argv, "ok": True}

    monkeypatch.setattr(replay_engine, "_run_probe_argv", run_command)
    return run_command


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
    assert out["spec"]["snapshot"] == out["snapshot"]
    assert out["spec"]["lineage"]["snapshot"] == out["snapshot"]
    assert out["spec"]["policy"] is None


def test_replay_uses_only_source_run_private_snapshot_by_default(
    replay_engine_db: Path, monkeypatch, tmp_path: Path,
):
    from herdr import projects, replay_engine

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "workflows"))
    src, _, workflow_id = _source_run(replay_engine_db)
    source_snapshot = projects.freeze_run_definition(
        workflow_id, definition={"nodes": [{"id": "frozen-source"}]})
    workflow = state_db.get_workflow(workflow_id, db_path=replay_engine_db)
    workflow["workflow_file"] = source_snapshot
    state_db.save_workflow(workflow, db_path=replay_engine_db)
    global_policy = tmp_path / "stage-policies.json"
    global_policy.write_text('{"agent_policy":{"mode":"global"}}', encoding="utf-8")
    monkeypatch.setenv("HERDR_STAGE_POLICIES", str(global_policy))
    out = replay_engine.replay_run(src, db_path=replay_engine_db)

    assert out["definition"]["nodes"][0]["id"] == "frozen-source"
    assert out["spec"]["policy"] is None
    assert out["spec"]["lineage"]["policy"] is None
    assert out["spec"]["snapshot"] == out["snapshot"]
    assert out["policy"] is None
    assert out["policy_source"] == "unavailable"


def test_definition_override_is_not_a_source_policy(replay_engine_db: Path):
    from herdr import replay_engine

    src, _, _ = _source_run(replay_engine_db)
    out = replay_engine.replay_run(
        src,
        definition={"nodes": [{"id": "dev", "agent_policy": {"mode": "target-only"}}]},
        db_path=replay_engine_db,
    )
    assert out["policy"] is None
    assert out["policy_source"] == "unavailable"


def test_replay_inherits_frozen_policy_and_launches_with_it(replay_engine_db: Path, monkeypatch, tmp_path: Path):
    from herdr import eval_store, projects, replay_engine

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "workflows"))
    src, _, workflow_id = _source_run(replay_engine_db)
    source_snapshot = projects.freeze_run_definition(workflow_id, definition={
        "nodes": [{"id": "dev", "agent_policy": {"mode": "strict"}}]})
    workflow = state_db.get_workflow(workflow_id, db_path=replay_engine_db)
    workflow["workflow_file"] = source_snapshot
    state_db.save_workflow(workflow, db_path=replay_engine_db)
    out = replay_engine.replay_run(src, db_path=replay_engine_db)
    options = dict(zip(out["launch_argv"][2::2], out["launch_argv"][3::2]))
    assert options["--agent-policy"] == '{"mode": "strict"}'
    assert out["policy_source"] == "source_snapshot"
    task = state_db.get_task(out["task_id"], db_path=replay_engine_db)
    assert task is not None and task["agent_policy"] == {"mode": "strict"}
    assert task["run_id"] == out["replay_run_id"]
    assert task["replay_of"] == src
    assert eval_store.get_replay_lineage(
        out["replay_run_id"], db_path=replay_engine_db) == [src, out["replay_run_id"]]


def test_source_task_policy_precedes_workflow_policy(replay_engine_db: Path, monkeypatch, tmp_path: Path):
    from herdr import projects, replay_engine

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "workflows"))
    src, task_id, workflow_id = _source_run(replay_engine_db)
    task = state_db.get_task(task_id, db_path=replay_engine_db)
    task["agent_policy"] = {"mode": "task-strict"}
    state_db.save_task(task, db_path=replay_engine_db)
    source_snapshot = projects.freeze_run_definition(workflow_id, definition={
        "nodes": [{"id": "dev", "agent_policy": {"mode": "workflow-observe"}}]})
    workflow = state_db.get_workflow(workflow_id, db_path=replay_engine_db)
    workflow["workflow_file"] = source_snapshot
    state_db.save_workflow(workflow, db_path=replay_engine_db)

    out = replay_engine.replay_run(src, db_path=replay_engine_db)
    assert out["policy"] == {"mode": "task-strict"}
    assert out["policy_source"] == "source_snapshot"


def test_source_workflow_frozen_metadata_supplies_policy(replay_engine_db: Path):
    from herdr import replay_engine

    src, _, workflow_id = _source_run(replay_engine_db)
    workflow = state_db.get_workflow(workflow_id, db_path=replay_engine_db)
    workflow["frozen_metadata"] = {
        "nodes": [{"id": "dev", "agent_policy": {"mode": "workflow-strict"}}]
    }
    state_db.save_workflow(workflow, db_path=replay_engine_db)
    out = replay_engine.replay_run(
        src,
        definition={"nodes": [{"id": "dev", "agent_policy": {"mode": "target-only"}}]},
        db_path=replay_engine_db,
    )
    assert out["policy"] == {"mode": "workflow-strict"}
    assert out["policy_source"] == "source_snapshot"


def test_source_replay_spec_policy_precedes_task_policy(replay_engine_db: Path, monkeypatch, tmp_path: Path):
    from herdr import eval_store, projects, replay_engine

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "workflows"))
    src, task_id, workflow_id = _source_run(replay_engine_db)
    task = state_db.get_task(task_id, db_path=replay_engine_db)
    task["agent_policy"] = {"mode": "task-observe"}
    state_db.save_task(task, db_path=replay_engine_db)
    source_snapshot = projects.freeze_run_definition(workflow_id, definition={
        "nodes": [{"id": "dev", "agent_policy": {"mode": "workflow-observe"}}]})
    workflow = state_db.get_workflow(workflow_id, db_path=replay_engine_db)
    workflow["workflow_file"] = source_snapshot
    state_db.save_workflow(workflow, db_path=replay_engine_db)
    eval_store.record_replay_spec(
        "run-ancestor", src, definition={"nodes": []}, snapshot=source_snapshot,
        policy={"mode": "replay-spec-strict"}, db_path=replay_engine_db)

    out = replay_engine.replay_run(src, db_path=replay_engine_db)
    assert out["policy"] == {"mode": "replay-spec-strict"}


def test_explicit_policy_override_preserves_source_run(replay_engine_db: Path, monkeypatch, tmp_path: Path):
    from herdr import projects, replay_engine

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "workflows"))
    src, source_task_id, workflow_id = _source_run(replay_engine_db)
    source_snapshot = projects.freeze_run_definition(workflow_id, definition={
        "nodes": [{"id": "dev", "agent_policy": {"mode": "strict"}}]})
    workflow = state_db.get_workflow(workflow_id, db_path=replay_engine_db)
    workflow["workflow_file"] = source_snapshot
    state_db.save_workflow(workflow, db_path=replay_engine_db)
    before = state_db.get_task(source_task_id, db_path=replay_engine_db)
    out = replay_engine.replay_run(src, policy={"mode": "observe"}, db_path=replay_engine_db)
    assert out["policy"] == {"mode": "observe"}
    assert out["policy_source"] == "explicit_override"
    assert state_db.get_task(source_task_id, db_path=replay_engine_db) == before
    assert __import__("json").loads(Path(source_snapshot).read_text())[
        "nodes"][0]["agent_policy"] == {"mode": "strict"}


def test_replay_runs_preflight_then_real_launch_without_synthetic_events(
    replay_engine_db: Path, monkeypatch, mock_external_replay_commands,
):
    from herdr import eval_store, replay_engine

    src, _, _ = _source_run(replay_engine_db)
    calls = []

    def run_existing_chain(argv, timeout=120, db_path=None):
        calls.append(argv)
        return mock_external_replay_commands(argv, timeout, db_path)

    monkeypatch.setattr(replay_engine, "_run_probe_argv", run_existing_chain)
    out = replay_engine.replay_run(
        src, definition={"nodes": [{"id": "dev"}]}, db_path=replay_engine_db,
    )

    assert calls == [out["preflight_argv"], out["launch_argv"]]
    assert out["launch_argv"][:2] == ["herdr-task", "launch"]
    target_events = state_db.list_trajectory_events(
        out["replay_run_id"], db_path=replay_engine_db)
    assert [event["event_type"] for event in target_events] == [
        "run_started", "task_started"]
    task = state_db.get_task(out["task_id"], db_path=replay_engine_db)
    assert task["run_id"] == out["replay_run_id"]
    assert task["replay_of"] == src
    assert eval_store.get_replay_lineage(
        out["replay_run_id"], db_path=replay_engine_db) == [src, out["replay_run_id"]]


def test_replay_rejects_missing_source_frozen_definition(replay_engine_db: Path):
    from herdr import eval_store, replay_engine

    src, _, _ = _source_run(replay_engine_db)
    with pytest.raises(ValueError, match="frozen definition"):
        replay_engine.replay_run(src, db_path=replay_engine_db)
    assert eval_store.list_replay_specs(db_path=replay_engine_db) == []


def test_failed_launch_is_reported_without_fabricating_run_events(
    replay_engine_db: Path, monkeypatch,
):
    from herdr import eval_store, replay_engine

    src, _, _ = _source_run(replay_engine_db)

    def fail_launch(argv, timeout=120, db_path=None):
        if argv[:2] == ["herdr-task", "launch"]:
            options = dict(zip(argv[2::2], argv[3::2]))
            state_db.save_task({
                "task_id": options["--task-id"],
                "workflow_id": options["--workflow-id"],
                "run_id": options["--run-id"],
                "status": "working",
                "replay_of": options["--replay-of"],
            }, db_path=replay_engine_db)
            from herdr.trajectory import TrajectoryLedger

            TrajectoryLedger(replay_engine_db).append_event({
                "run_id": options["--run-id"], "task_id": options["--task-id"],
                "workflow_id": options["--workflow-id"], "event_type": "task_started",
            })
            from herdr.state_store import get_state_store, sync_tasks_projection

            sync_tasks_projection(store=get_state_store(db_path=replay_engine_db))
        return {"argv": argv, "ok": argv[0] == "herdr-preflight"}

    monkeypatch.setattr(replay_engine, "_run_probe_argv", fail_launch)
    with pytest.raises(RuntimeError, match="launch chain failed"):
        replay_engine.replay_run(
            src, replay_run_id="run-failed-target", task_id="t-failed-target",
            workflow_id="wf-failed-target", definition={"nodes": [{"id": "dev"}]},
            db_path=replay_engine_db)

    specs = eval_store.list_replay_specs(db_path=replay_engine_db)
    assert specs == []
    assert state_db.get_task("t-failed-target", db_path=replay_engine_db) is None
    assert state_db.list_trajectory_events("run-failed-target", db_path=replay_engine_db) == []
    assert eval_store.get_replay_lineage("run-failed-target", db_path=replay_engine_db) == []
    workflows_projection = replay_engine_db.parent / "workflows.json"
    tasks_projection = replay_engine_db.parent / "tasks.json"
    if workflows_projection.exists():
        assert "wf-failed-target" not in workflows_projection.read_text(encoding="utf-8")
    if tasks_projection.exists():
        assert "t-failed-target" not in tasks_projection.read_text(encoding="utf-8")


def test_definition_override_allows_replay_without_source_snapshot(
    replay_engine_db: Path,
):
    from herdr import replay_engine

    src, _, _ = _source_run(replay_engine_db)
    out = replay_engine.replay_run(
        src, definition={"nodes": [{"id": "override"}]}, db_path=replay_engine_db,
    )
    assert Path(out["snapshot"]).exists()
    assert out["spec"]["definition"] == {"nodes": [{"id": "override"}]}


def test_target_snapshot_freeze_failure_rejects_persisted_replay(
    replay_engine_db: Path, monkeypatch,
):
    from herdr import eval_store, projects, replay_engine

    src, _, _ = _source_run(replay_engine_db)
    monkeypatch.setattr(projects, "freeze_run_definition", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="could not be frozen"):
        replay_engine.replay_run(
            src, definition={"nodes": [{"id": "dev"}]}, db_path=replay_engine_db)
    assert eval_store.list_replay_specs(db_path=replay_engine_db) == []
    assert len(state_db.list_workflows(db_path=replay_engine_db)) == 1


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
