"""Controller startup watchdog: bounded reclaim, false-positive protection, recovery."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr.state_store import SQLiteStateStore, reset_state_store
from services.controller import startup_watchdog


def _scene(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    monkeypatch.setenv("STAGE_STATE_FILE", str(tmp_path / "stage-state.json"))
    reset_state_store()
    store = SQLiteStateStore(db_path)
    return store


def _record(store, wid, *, now=1_000_000.0, created_at=None, status="running",
            startup_ready=False, tasks=0, workflow_file=None):
    record = {
        "workflow_id": wid, "project_id": "p1", "status": status,
        "created_at": now - 1801 if created_at is None else created_at,
        # 非 marker 路径：SQL fixture 过滤器不会排除该记录。
        "workflow_file": workflow_file or f"/var/data/{wid}.json",
        "title": wid, "requirement": "需求",
    }
    if startup_ready is not _MISSING:
        record["startup_ready"] = startup_ready
    store.save_workflow(record)
    for index in range(tasks):
        store.save_task({
            "task_id": f"{wid}-t{index}", "workflow_id": wid, "status": "pending",
        })


class _Missing:
    pass


_MISSING = _Missing()


def _ports(store, now, cleared=None, foreign=None):
    return SimpleNamespace(
        _startup_watchdog_scan_at=0.0,
        _startup_watchdog_cursor=None,
        _startup_projection_stale=False,
        _get_store=lambda: store,
        time=SimpleNamespace(time=lambda: now, sleep=lambda _s: None),
        os=SimpleNamespace(environ={}),
        liveness=SimpleNamespace(
            workflow_is_foreign=foreign or (lambda _w: False)),
        clear_workflow_stage_state=lambda wid: (cleared if cleared is not None else []).append(wid),
    )


def test_watchdog_reclaims_once_and_unblocks_project(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-orphan")
    cleared, projected = [], []
    ports = _ports(store, 1_000_000.0, cleared)
    real_sync = startup_watchdog.sync_workflows_projection
    monkeypatch.setattr(startup_watchdog, "sync_workflows_projection",
                        lambda **_k: projected.append(True) or real_sync(**_k))

    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == ["wf-orphan"]
    record = store.get_workflow("wf-orphan")
    assert record["status"] == "completed"
    assert record["outcome"] == "startup_failed"
    assert record["failed_stage"] == "startup"
    assert record["startup_ready"] is False
    events = store.list_events(
        workflow_id="wf-orphan", event_type="workflow_transition")
    assert len(events) == 1
    assert events[0]["source"] == "controller-startup-watchdog"
    assert cleared == ["wf-orphan"]
    assert projected == [True]

    projection = json.loads((tmp_path / "workflows.json").read_text(encoding="utf-8"))
    assert projection["workflows"]["wf-orphan"]["status"] == "completed"
    assert projection["workflows"]["wf-orphan"]["outcome"] == "startup_failed"

    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_120.0) == []
    assert len(store.list_events(
        workflow_id="wf-orphan", event_type="workflow_transition")) == 1

    from herdr.projects import active_workflows_for_project
    assert active_workflows_for_project("p1") == []


@pytest.mark.parametrize("kwargs", [
    {"created_at": 1_000_000.0 - 60},          # not expired
    {"created_at": 1_000_000.0 - 1800},        # boundary equality: strictly expired only
    {"startup_ready": True},
    {"tasks": 1},
    {"status": "paused"},
    {"status": "in_progress"},
    {"status": "closing"},
    {"status": "failed"},
    {"startup_ready": _MISSING},
    {"startup_ready": 0},
    {"startup_ready": "false"},
    {"startup_ready": None},
])
def test_watchdog_never_reclaims_non_orphans(tmp_path, monkeypatch, kwargs):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-safe", **kwargs)
    ports = _ports(store, 1_000_000.0)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == []
    record = store.get_workflow("wf-safe")
    assert record["status"] == kwargs.get("status", "running")
    assert record.get("startup_ready") in (None, False, True, 0, "false")
    assert store.list_events(
        workflow_id="wf-safe", event_type="workflow_transition") == []


def test_watchdog_never_reclaims_damaged_metadata(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-damaged")
    from herdr import state_db
    conn = state_db.get_db_connection(store.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE workflows SET metadata_json = '{not json' "
                     "WHERE workflow_id = 'wf-damaged'")
        conn.execute("COMMIT")
    finally:
        conn.close()
    ports = _ports(store, 1_000_000.0)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == []
    conn = state_db.get_readonly_db_connection(store.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM workflows WHERE workflow_id = 'wf-damaged'"
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "running"


@pytest.mark.parametrize("task_status", ["pending", "working", "failed", "superseded", "cleaned"])
def test_watchdog_never_reclaims_any_task_status(tmp_path, monkeypatch, task_status):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-task-status")
    store.save_task({
        "task_id": f"task-{task_status}",
        "workflow_id": "wf-task-status",
        "status": task_status,
    })
    ports = _ports(store, 1_000_000.0)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == []
    assert store.get_workflow("wf-task-status")["status"] == "running"


def test_watchdog_skips_foreign_record(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-foreign")
    ports = _ports(store, 1_000_000.0)
    ports.liveness = SimpleNamespace(workflow_is_foreign=lambda _w: True)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == []
    assert store.get_workflow("wf-foreign")["status"] == "running"


def test_invalid_deadline_falls_back_to_1800(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-500s", created_at=1_000_000.0 - 500)
    _record(store, "wf-1801s", created_at=1_000_000.0 - 1801)
    ports = _ports(store, 1_000_000.0)
    ports.os.environ = {"HERDR_WORKFLOW_STARTUP_DEADLINE_SECONDS": "abc"}
    from herdr import workflow_startup
    monkeypatch.setattr(workflow_startup, "_warned_invalid_deadline", False, raising=False)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == ["wf-1801s"]
    assert store.get_workflow("wf-500s")["status"] == "running"


def test_watchdog_respects_scan_throttle(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-throttle")
    ports = _ports(store, 1_000_000.0)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == ["wf-throttle"]
    _record(store, "wf-throttle-2")
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_010.0) == []
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_061.0) == ["wf-throttle-2"]


def test_watchdog_reclaims_at_most_twenty_per_scan(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    for index in range(25):
        _record(store, f"wf-{index:02d}", created_at=1_000_000.0 - 5000 + index)
    ports = _ports(store, 1_000_000.0)
    reclaimed = startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0)
    assert len(reclaimed) == 20
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_061.0) == [
        f"wf-{index:02d}" for index in range(20, 25)]


def test_foreign_backlog_does_not_starve_valid_orphan(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    for index in range(120):
        _record(store, f"wf-foreign-{index:03d}",
                created_at=1_000_000.0 - 5000 + index)
    _record(store, "wf-valid", created_at=1_000_000.0 - 4000)

    def foreign(record):
        return "foreign" in (record.get("workflow_file") or "")

    ports = _ports(store, 1_000_000.0, foreign=foreign)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == []
    assert ports._startup_watchdog_cursor is not None
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_061.0) == ["wf-valid"]
    assert store.get_workflow("wf-valid")["status"] == "completed"


def test_projection_failure_keeps_db_terminal_and_recovers(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-projection")
    ports = _ports(store, 1_000_000.0)
    real_sync = startup_watchdog.sync_workflows_projection

    def failing_sync(**_k):
        raise OSError("disk full")

    monkeypatch.setattr(startup_watchdog, "sync_workflows_projection", failing_sync)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0) == ["wf-projection"]
    record = store.get_workflow("wf-projection")
    assert record["status"] == "completed"
    assert record["outcome"] == "startup_failed"
    assert len(store.list_events(
        workflow_id="wf-projection", event_type="workflow_transition")) == 1
    assert ports._startup_projection_stale is True
    assert not (tmp_path / "workflows.json").exists()

    monkeypatch.setattr(startup_watchdog, "sync_workflows_projection", real_sync)
    assert startup_watchdog.scan_startup_orphans(ports, now=1_000_061.0) == []
    assert ports._startup_projection_stale is False
    projection = json.loads((tmp_path / "workflows.json").read_text(encoding="utf-8"))
    assert projection["workflows"]["wf-projection"]["status"] == "completed"


def test_zero_task_startup_failed_does_not_notify_or_auto_close(tmp_path, monkeypatch):
    store = _scene(tmp_path, monkeypatch)
    _record(store, "wf-consumer")
    ports = _ports(store, 1_000_000.0)
    startup_watchdog.scan_startup_orphans(ports, now=1_000_000.0)

    import importlib.machinery
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "services/herdr-notifier.py"
    loader = importlib.machinery.SourceFileLoader("notifier_startup_consumer", str(path))
    spec = importlib.util.spec_from_file_location(loader.name, path)
    notifier = importlib.util.module_from_spec(spec)
    loader.exec_module(notifier)
    assert notifier.workflow_complete([]) is False

    # Real notifier scan path: zero-task startup_failed must emit no notification.
    tasks_file = tmp_path / "notifier-tasks.json"
    tasks_file.write_text(json.dumps({
        "tasks": [{
            "task_id": "other", "workflow_id": "wf-other",
            "status": "pending", "project_name": "p1",
        }],
    }), encoding="utf-8")
    state_file = tmp_path / "notifier-state.json"
    monkeypatch.setattr(notifier, "TASKS_FILE", tasks_file)
    monkeypatch.setattr(notifier, "STATE_FILE", state_file)
    notifications = []
    monkeypatch.setattr(notifier, "notify",
                        lambda *a, **k: notifications.append((a, k)))
    notifier.scan({})
    assert notifications == []
    assert "wf-consumer" not in notifier.load(
        state_file, {}).get("completed_workflows", [])

    from services.controller import workflow_close
    calls = []
    fake_ports = SimpleNamespace(
        _workflow_close_inflight=set(),
        attention_blocks_retry=lambda _key: False,
        _workflow_entry=lambda _wid: store.get_workflow("wf-consumer"),
        liveness=SimpleNamespace(workflow_is_foreign=lambda _w: False),
        git_finalize_pending_tasks=lambda _wid: calls.append("git") or [],
    )
    workflow_close.maybe_close_completed_workflow(fake_ports, "wf-consumer")
    assert calls == []


def test_sweep_invokes_watchdog_and_survives_its_failure():
    from services.controller import registry
    calls = []

    def broken():
        calls.append("scan")
        raise RuntimeError("watchdog exploded")

    ports = SimpleNamespace(
        time=SimpleNamespace(time=lambda: 100),
        _continuation_scan_at=0,
        active_registered_workflows=list,
        _get_store=lambda: None,
        schedule_workflow_continuations=lambda ids, now: None,
        scan_startup_orphans=broken,
        sync_awake_guard=lambda _ids: None,
    )
    registry.check_all_workflows_stage_advance(ports)
    assert calls == ["scan"]


def test_clear_workflow_stage_state_removes_only_target_keys(tmp_path):
    import threading

    from services.controller import stage_state

    state_file = tmp_path / "stage-state.json"
    state_file.write_text(json.dumps({
        "wf-a:requirements": "queued",
        "wf-a:plan": "notified",
        "wf-b:requirements": "queued",
    }), encoding="utf-8")
    ports = SimpleNamespace(
        STAGE_STATE_FILE=str(state_file),
        os=os,
        json=json,
        lock=threading.Lock(),
    )
    ports.load_stage_state = lambda: stage_state.load_stage_state(ports)
    ports.save_stage_state = lambda data: stage_state.save_stage_state(ports, data)
    removed = stage_state.clear_workflow_stage_state(ports, "wf-a")
    assert sorted(removed) == ["wf-a:plan", "wf-a:requirements"]
    remaining = json.loads(state_file.read_text(encoding="utf-8"))
    assert remaining == {"wf-b:requirements": "queued"}

    assert stage_state.clear_workflow_stage_state(ports, "wf-a") == []
