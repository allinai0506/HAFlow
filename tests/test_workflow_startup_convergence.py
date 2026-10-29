"""Factory startup convergence, no-resurrection startup writes, and ready/reclaim race."""

import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    monkeypatch.setenv("TASKS_FILE", str(tmp_path / "tasks.json"))
    monkeypatch.setenv("STAGE_STATE_FILE", str(tmp_path / "stage-state.json"))
    from herdr.state_store import reset_state_store
    reset_state_store()
    loader = importlib.machinery.SourceFileLoader(
        "factory_startup_convergence_test", str(ROOT / "bin" / "herdr-factory"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    # Keep the real preflight callable reachable even after seam patching.
    module._real_run_workflow_preflight = module.run_workflow_preflight
    yield module
    reset_state_store()


def _project(tmp_path):
    root = tmp_path / "src"
    root.mkdir(exist_ok=True)
    wf_file = tmp_path / "workflow.json"
    wf_file.write_text(json.dumps({
        "workflow_template": "software-development-v1",
        "nodes": [
            {"id": "requirements", "label": "2需求分析"},
            {"id": "plan", "label": "3计划", "depends_on": ["requirements"]},
        ],
    }), encoding="utf-8")
    return {
        "project_id": "proj-startup-convergence",
        "project_name": "startup-convergence",
        "project_root": str(root),
        "base_branch": "main",
        "workspace_id": "ws-convergence",
        "coordinator_pane_id": "ws-convergence:p1",
        "workflow_file": str(wf_file),
    }


def _patch_startup_seams(monkeypatch, factory, project, preflight):
    monkeypatch.setattr(factory, "resolve_project_for_template", lambda *a, **k: project)
    monkeypatch.setattr(factory, "ensure_pool_for_project", lambda _pid: {})
    monkeypatch.setattr(
        "herdr.workspace_trust.ensure_controller_env_trust", lambda _root: None)
    monkeypatch.setattr(factory, "workflow_config_for", lambda _wid: json.loads(
        Path(project["workflow_file"]).read_text(encoding="utf-8")))
    monkeypatch.setattr(factory, "run_workflow_preflight", preflight)


def _store():
    from herdr.state_store import get_state_store
    return get_state_store(Path(os.environ["HERDR_STATE_DB"]))


def _fail(exc):
    def _raise(*_a, **_k):
        raise exc
    return _raise


@pytest.mark.parametrize("exc", [
    RuntimeError("preflight exploded"),
    KeyboardInterrupt(),
    SystemExit(3),
])
def test_factory_failure_converges_and_unblocks_project(factory, tmp_path, monkeypatch, exc):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, _fail(exc))
    with pytest.raises(type(exc)):
        factory.start_workflow(
            "需求", workflow_id="wf-startup-fail",
            project_path=project["project_root"])

    record = _store().get_workflow("wf-startup-fail")
    assert record["status"] == "completed"
    assert record["outcome"] == "startup_failed"
    assert record["startup_ready"] is False
    assert record["failed_stage"] == "preflight"
    assert 0 < len(record["startup_failure_reason"].encode("utf-8")) <= 2048
    events = _store().list_events(
        workflow_id="wf-startup-fail", event_type="workflow_transition")
    assert len(events) == 1
    assert events[0]["source"] == "herdr-factory"
    assert events[0]["payload"]["to_status"] == "completed"

    _patch_startup_seams(
        monkeypatch, factory, project, lambda *a, **k: (["sandbox"], {}))
    second = factory.start_workflow(
        "第二个需求", workflow_id="wf-startup-second",
        project_path=project["project_root"])
    assert second == "wf-startup-second"
    assert _store().get_workflow("wf-startup-second")["startup_ready"] is True


def test_rejection_does_not_converge_someone_elses_workflow(
        factory, tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, lambda *a, **k: (["sandbox"], {}))
    factory.start_workflow(
        "第一个需求", workflow_id="wf-active",
        project_path=project["project_root"])
    capsys.readouterr()

    with pytest.raises(SystemExit) as rejected:
        factory.start_workflow(
            "重复需求", workflow_id="wf-never-registered",
            project_path=project["project_root"])
    assert rejected.value.code == 2
    err = capsys.readouterr().err
    assert "[WORKFLOW STARTUP CONVERGE FAILED]" not in err
    assert _store().get_workflow("wf-never-registered") is None
    assert _store().get_workflow("wf-active")["status"] == "running"
    assert _store().get_workflow("wf-active")["startup_ready"] is True


def test_registration_committed_then_raises_still_converges(
        factory, tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, lambda *a, **k: (["sandbox"], {}))
    real_register = factory.register_workflow

    def register_then_raise(*args, **kwargs):
        real_register(*args, **kwargs)
        raise RuntimeError("post-commit projection exploded")

    monkeypatch.setattr(factory, "register_workflow", register_then_raise)
    with pytest.raises(RuntimeError, match="post-commit"):
        factory.start_workflow(
            "需求", workflow_id="wf-post-commit",
            project_path=project["project_root"])
    record = _store().get_workflow("wf-post-commit")
    assert record["status"] == "completed"
    assert record["outcome"] == "startup_failed"
    assert record["failed_stage"] == "startup"
    assert "[WORKFLOW STARTUP FAILED] workflow_id=wf-post-commit" in capsys.readouterr().err


def test_system_exit_before_ready_converges(factory, tmp_path, monkeypatch):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, lambda *a, **k: (["sandbox"], {}))

    def ready_exits(*_a, **_k):
        raise SystemExit(4)

    monkeypatch.setattr(factory, "mark_workflow_startup_ready", ready_exits)
    with pytest.raises(SystemExit) as exited:
        factory.start_workflow(
            "需求", workflow_id="wf-ready-exit",
            project_path=project["project_root"])
    assert exited.value.code == 4
    record = _store().get_workflow("wf-ready-exit")
    assert record["status"] == "completed"
    assert record["outcome"] == "startup_failed"
    assert record["failed_stage"] == "startup"


def test_real_preflight_no_ready_agent_converges(factory, tmp_path, monkeypatch):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, factory.run_workflow_preflight)

    class Proc:
        returncode = 0
        stdout = json.dumps({
            "agents": [{"agent": "codex", "final_status": "TIMEOUT"}],
        })
        stderr = ""

    monkeypatch.setattr(factory, "run", lambda *a, **k: Proc())
    with pytest.raises(RuntimeError, match="未发现任何 READY Agent"):
        factory.start_workflow(
            "需求", workflow_id="wf-no-ready",
            project_path=project["project_root"])
    record = _store().get_workflow("wf-no-ready")
    assert record["status"] == "completed"
    assert record["outcome"] == "startup_failed"
    assert record["failed_stage"] == "preflight"


def test_ready_submission_after_reclaim_cannot_resurrect(factory, tmp_path, monkeypatch):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, _fail(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        factory.start_workflow(
            "需求", workflow_id="wf-race",
            project_path=project["project_root"])
    from herdr.projects import mark_workflow_startup_ready
    from herdr.transitions import InvalidTransitionError
    with pytest.raises(InvalidTransitionError):
        mark_workflow_startup_ready("wf-race", healthy_agents=["codex"])
    assert _store().get_workflow("wf-race")["status"] == "completed"
    assert _store().get_workflow("wf-race")["startup_ready"] is False


def test_convergence_refuses_to_close_workflow_with_tasks(
        factory, tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)

    def fail_with_task(*args, **kwargs):
        _store().save_task({
            "task_id": "manual-task",
            "workflow_id": "wf-with-task",
            "status": "pending",
        })
        raise RuntimeError("preflight exploded")

    _patch_startup_seams(monkeypatch, factory, project, fail_with_task)
    with pytest.raises(RuntimeError):
        factory.start_workflow(
            "需求", workflow_id="wf-with-task",
            project_path=project["project_root"])
    record = _store().get_workflow("wf-with-task")
    assert record["status"] == "running"
    assert record["startup_ready"] is False
    err = capsys.readouterr().err
    assert "[WORKFLOW STARTUP CONVERGE FAILED]" in err
    assert "herdr-task close-workflow wf-with-task --abandon" in err


def test_startup_window_writes_rejected_after_terminal(factory, tmp_path, monkeypatch):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, _fail(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        factory.start_workflow(
            "需求", workflow_id="wf-terminal",
            project_path=project["project_root"])

    from herdr.agent_router import set_workflow_agent_override
    from herdr.transitions import InvalidTransitionError
    with pytest.raises(InvalidTransitionError):
        set_workflow_agent_override("wf-terminal", "codex")

    class Proc:
        returncode = 0
        stdout = json.dumps({"agents": [{"agent": "codex", "final_status": "READY"}]})
        stderr = ""

    monkeypatch.setattr(factory, "run", lambda *a, **k: Proc())
    monkeypatch.setattr(
        factory, "run_workflow_preflight", factory._real_run_workflow_preflight)
    with pytest.raises(InvalidTransitionError):
        factory.run_workflow_preflight(project, "wf-terminal")

    record = _store().get_workflow("wf-terminal")
    assert record["status"] == "completed"
    assert record["startup_ready"] is False
    assert "preflight_checked_at" not in record


def test_preflight_fails_closed_without_state_store_record(factory, tmp_path, monkeypatch):
    project = _project(tmp_path)

    class Proc:
        returncode = 0
        stdout = json.dumps({"agents": [{"agent": "codex", "final_status": "READY"}]})
        stderr = ""

    monkeypatch.setattr(factory, "run", lambda *a, **k: Proc())
    with pytest.raises(RuntimeError, match="Workflow registry missing in StateStore"):
        factory.run_workflow_preflight(project, "wf-missing")


def test_convergence_failure_prints_manual_guidance(factory, tmp_path, monkeypatch, capsys):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, _fail(RuntimeError("preflight exploded")))
    import herdr.workflow_startup as workflow_startup
    monkeypatch.setattr(
        workflow_startup, "transition_workflow",
        _fail(sqlite3.OperationalError("disk full")))
    with pytest.raises(RuntimeError):
        factory.start_workflow("需求", workflow_id="wf-converge-fail",
                               project_path=project["project_root"])
    stderr = capsys.readouterr().err
    assert "[WORKFLOW STARTUP CONVERGE FAILED]" in stderr
    assert "herdr-task close-workflow wf-converge-fail --abandon" in stderr
    assert _store().get_workflow("wf-converge-fail")["status"] == "running"


def test_cli_entry_returns_failure_and_second_create_passes(
        factory, tmp_path, monkeypatch, capsys):
    import sys
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project,
        _fail(RuntimeError("no READY agent")))
    monkeypatch.setattr(sys, "argv", [
        "herdr-factory", "run", "--project", project["project_root"],
        "--workflow-id", "wf-cli-fail", "需求",
    ])
    with pytest.raises(SystemExit) as first:
        factory.main()
    assert first.value.code == 2
    err = capsys.readouterr().err
    assert "[WORKFLOW STARTUP FAILED] workflow_id=wf-cli-fail" in err
    assert "herdr-task close-workflow wf-cli-fail --abandon" in err
    assert _store().get_workflow("wf-cli-fail")["status"] == "completed"

    _patch_startup_seams(
        monkeypatch, factory, project, lambda *a, **k: (["sandbox"], {}))
    monkeypatch.setattr(sys, "argv", [
        "herdr-factory", "run", "--project", project["project_root"],
        "--workflow-id", "wf-cli-second", "第二个需求",
    ])
    with pytest.raises(SystemExit) as second:
        factory.main()
    assert second.value.code == 0


def _orphan_for_race(db_path, wid="wf-race-core", now=1_000_000.0):
    from herdr.state_store import get_state_store
    store = get_state_store(db_path)
    store.save_workflow({
        "workflow_id": wid, "project_id": "p1", "status": "running",
        "startup_ready": False, "created_at": now - 1801,
        "workflow_file": str(Path(db_path).parent / "wf.json"),
    })
    return store


@pytest.mark.parametrize("first", ["ready", "reclaim"])
def test_ready_and_reclaim_are_serialized(tmp_path, monkeypatch, first):
    now = 1_000_000.0
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    from herdr.state_store import reset_state_store
    reset_state_store()
    store = _orphan_for_race(db_path)

    from herdr import projects, workflow_startup as ws
    from herdr.projects import mark_workflow_startup_ready
    monkeypatch.setattr(projects, "_get_store", lambda: store)

    acquired = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    results, errors = {}, {}

    if first == "ready":
        original_save = store.save_workflow

        def blocked_save(workflow, conn=None):
            original_save(workflow, conn=conn)
            acquired.set()
            assert release.wait(10)

        monkeypatch.setattr(store, "save_workflow", blocked_save)
    else:
        original_transition = ws.transition_workflow

        def blocked_transition(*args, **kwargs):
            acquired.set()
            assert release.wait(10)
            return original_transition(*args, **kwargs)

        monkeypatch.setattr(ws, "transition_workflow", blocked_transition)

    def ready():
        second_started.set()
        try:
            mark_workflow_startup_ready("wf-race-core", healthy_agents=["codex"])
        except BaseException as exc:
            errors["ready"] = exc

    def reclaim():
        second_started.set()
        try:
            results["reclaim"] = ws.reclaim_startup_orphan(
                store, "wf-race-core", now, 1800, foreign_check=lambda _: False)
        except BaseException as exc:
            errors["reclaim"] = exc

    first_thread = threading.Thread(target=ready if first == "ready" else reclaim)
    second_thread = threading.Thread(target=reclaim if first == "ready" else ready)
    first_thread.start()
    assert acquired.wait(10), "first writer never entered its critical section"
    second_thread.start()
    assert second_started.wait(10)
    # The second writer can only proceed after the first commits; the first is
    # still holding the write transaction here, so this ordering is forced.
    release.set()
    first_thread.join(10)
    second_thread.join(10)
    assert not first_thread.is_alive() and not second_thread.is_alive()

    from herdr.transitions import InvalidTransitionError
    final = store.get_workflow("wf-race-core")
    if first == "ready":
        assert final["status"] == "running" and final["startup_ready"] is True
        assert results["reclaim"] == {"ok": False, "reason": "condition_not_met"}
        assert "ready" not in errors
    else:
        assert final["status"] == "completed" and final["startup_ready"] is False
        assert results["reclaim"]["ok"] is True
        assert isinstance(errors.get("ready"), InvalidTransitionError)
    assert not (final["status"] == "completed" and final["startup_ready"] is True)
    events = store.list_events(
        workflow_id="wf-race-core", event_type="workflow_transition")
    assert len(events) == (0 if first == "ready" else 1)


@pytest.mark.parametrize("writer", ["override", "preflight"])
def test_startup_window_writes_never_resurrect_terminal(tmp_path, monkeypatch, factory, writer):
    project = _project(tmp_path)
    _patch_startup_seams(
        monkeypatch, factory, project, _fail(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        factory.start_workflow(
            "需求", workflow_id="wf-write-race",
            project_path=project["project_root"])
    from herdr.transitions import InvalidTransitionError
    if writer == "override":
        from herdr.agent_router import set_workflow_agent_override as write
        with pytest.raises(InvalidTransitionError):
            write("wf-write-race", "claude")
    else:
        class Proc:
            returncode = 0
            stdout = json.dumps({"agents": [{"agent": "codex", "final_status": "READY"}]})
            stderr = ""
        monkeypatch.setattr(factory, "run", lambda *a, **k: Proc())
        monkeypatch.setattr(
            factory, "run_workflow_preflight", factory._real_run_workflow_preflight)
        with pytest.raises(InvalidTransitionError):
            factory.run_workflow_preflight(project, "wf-write-race")
    record = _store().get_workflow("wf-write-race")
    assert record["status"] == "completed"
    assert record["startup_ready"] is False
    assert "agent_override" not in record
    assert "preflight_checked_at" not in record
