"""Startup lifecycle core: deadline, bounded reason, orphan predicate, CAS reclaim."""

import pytest

from herdr import workflow_startup as ws
from herdr.state_store import SQLiteStateStore
from herdr.transitions import InvalidTransitionError


@pytest.fixture(autouse=True)
def reset_warning_latch(monkeypatch):
    monkeypatch.setattr(ws, "_warned_invalid_deadline", False, raising=False)


def _store(tmp_path):
    return SQLiteStateStore(tmp_path / "state.db")


def _orphan(tmp_path, wid="wf-orphan", *, created_at, startup_ready=False,
            status="running", tasks=0, workflow_file=None):
    store = _store(tmp_path)
    wf_file = tmp_path / f"{wid}.json"
    wf_file.write_text("{}", encoding="utf-8")
    record = {
        "workflow_id": wid,
        "project_id": "p1",
        "status": status,
        "created_at": created_at,
        "workflow_file": workflow_file or str(wf_file),
        "title": wid,
        "requirement": "需求",
    }
    if startup_ready is not _MISSING:
        record["startup_ready"] = startup_ready
    store.save_workflow(record)
    for index in range(tasks):
        store.save_task({
            "task_id": f"{wid}-t{index}",
            "workflow_id": wid,
            "status": "pending",
        })
    return store


class _Missing:
    pass


_MISSING = _Missing()


def test_deadline_validation_and_one_time_warning():
    warnings = []
    for raw in ("abc", "0", "-1", "210", "205.5", "inf", "1e309"):
        assert ws.startup_deadline_seconds(
            {ws.STARTUP_DEADLINE_ENV: raw}, warnings.append) == 1800
    assert ws.startup_deadline_seconds(
        {ws.STARTUP_DEADLINE_ENV: "3600"}, warnings.append) == 3600
    assert ws.startup_deadline_seconds({}, warnings.append) == 1800
    assert len(warnings) == 1


def test_reason_is_redacted_and_bounded_in_utf8_bytes():
    reason = ws.bound_startup_reason("token=abcdefghijklmnopqrstuvwxyz " + "x" * 5000)
    assert len(reason.encode("utf-8")) <= 2048
    assert "abcdefghijklmnopqrstuvwxyz" not in reason

    chinese = ws.bound_startup_reason("失败" * 3000)
    assert len(chinese.encode("utf-8")) <= 2048
    chinese.encode("utf-8").decode("utf-8")  # never split a multi-byte char


def test_describe_exception_and_failure_metadata_are_bounded():
    detail = ws.describe_exception(RuntimeError("boom sk-abcdefghijklmnopqrstuvwxyz"))
    assert detail.startswith("RuntimeError: ")
    assert "abcdefghijklmnopqrstuvwxyz" not in detail
    assert ws.describe_exception(RuntimeError("")) == "RuntimeError: RuntimeError"

    meta = ws.startup_failure_metadata("preflight", "x" * 5000)
    assert meta["outcome"] == "startup_failed"
    assert meta["startup_ready"] is False
    assert meta["failed_stage"] == "preflight"
    assert 0 < len(meta["startup_failure_reason"].encode("utf-8")) <= 2048
    assert ws.startup_failure_metadata("weird", "x")["failed_stage"] == "startup"


def test_orphan_predicate_matrix():
    now, deadline = 1_000_000.0, 1800
    base = {"status": "running", "startup_ready": False,
            "created_at": now - deadline - 1}
    assert ws.is_startup_orphan(base, 0, now, deadline)
    assert not ws.is_startup_orphan({**base, "startup_ready": True}, 0, now, deadline)
    assert not ws.is_startup_orphan(base, 1, now, deadline)
    assert not ws.is_startup_orphan({**base, "status": "paused"}, 0, now, deadline)
    assert not ws.is_startup_orphan(
        {**base, "created_at": now - deadline + 1}, 0, now, deadline)
    assert not ws.is_startup_orphan(
        {**base, "created_at": now - deadline}, 0, now, deadline)  # strict boundary
    assert not ws.is_startup_orphan(base, 0, now, deadline, foreign=True)
    assert not ws.is_startup_orphan(
        {k: v for k, v in base.items() if k != "startup_ready"},
        0, now, deadline)
    for value in (True, 0, 1, "false", None):
        assert not ws.is_startup_orphan(
            {**base, "startup_ready": value}, 0, now, deadline)


def test_candidate_query_filters_in_sql(tmp_path):
    now = 1_000_000.0
    _orphan(tmp_path, created_at=now - 1801,
            workflow_file="/var/data/wf-orphan.json")
    _orphan(tmp_path, wid="wf-ready", created_at=now - 1801,
            startup_ready=True, workflow_file="/var/data/wf-ready.json")
    _orphan(tmp_path, wid="wf-task", created_at=now - 1801, tasks=1,
            workflow_file="/var/data/wf-task.json")
    _orphan(tmp_path, wid="wf-fresh", created_at=now - 60,
            workflow_file="/var/data/wf-fresh.json")
    _orphan(tmp_path, wid="pytest-old", created_at=now - 5000,
            workflow_file="/tmp/pytest-old/workflow.json")

    assert ws.find_startup_orphan_candidates(
        tmp_path / "state.db", now, 1800) == ["wf-orphan"]
    unfiltered = ws.find_startup_orphan_candidates(
        tmp_path / "state.db", now, 1800, exclude_foreign_paths=False)
    assert set(unfiltered) == {"wf-orphan", "pytest-old"}


def test_candidate_query_requires_explicit_boolean_false(tmp_path):
    now = 1_000_000.0
    _orphan(tmp_path, wid="wf-false", created_at=now - 1801,
            workflow_file="/var/data/a.json")
    _orphan(tmp_path, wid="wf-true", created_at=now - 1801, startup_ready=True,
            workflow_file="/var/data/a.json")
    _orphan(tmp_path, wid="wf-zero", created_at=now - 1801, startup_ready=0,
            workflow_file="/var/data/a.json")
    _orphan(tmp_path, wid="wf-string", created_at=now - 1801,
            startup_ready="false", workflow_file="/var/data/a.json")
    _orphan(tmp_path, wid="wf-null", created_at=now - 1801,
            startup_ready=None, workflow_file="/var/data/a.json")
    _orphan(tmp_path, wid="wf-missing", created_at=now - 1801,
            startup_ready=_MISSING, workflow_file="/var/data/a.json")
    _orphan(tmp_path, wid="wf-badjson", created_at=now - 1801,
            workflow_file="/var/data/a.json")
    _orphan(tmp_path, wid="wf-boundary", created_at=now - 1800,
            workflow_file="/var/data/a.json")

    from herdr import state_db
    conn = state_db.get_db_connection(tmp_path / "state.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE workflows SET metadata_json = 'not-json' "
            "WHERE workflow_id = 'wf-badjson'")
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert ws.find_startup_orphan_candidates(
        tmp_path / "state.db", now, 1800) == ["wf-false"]


def test_candidate_query_uses_tasks_index_and_bounded_limit(tmp_path):
    now = 1_000_000.0
    for index in range(3):
        _orphan(tmp_path, wid=f"wf-{index}", created_at=now - 2000 + index,
                workflow_file="/var/data/a.json")
    assert ws.find_startup_orphan_candidates(
        tmp_path / "state.db", now, 1800, limit=2) == ["wf-0", "wf-1"]

    from herdr import state_db
    conn = state_db.get_readonly_db_connection(tmp_path / "state.db")
    try:
        plan = " ".join(
            row["detail"]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + ws.CANDIDATE_SQL,
                tuple(ws.candidate_query_params(now, 1800, 100)))
        )
    finally:
        conn.close()
    assert "idx_tasks_wf_created" in plan


def test_candidate_cursor_advances_past_foreign_backlog(tmp_path):
    now = 1_000_000.0
    db_path = tmp_path / "state.db"
    for index in range(120):
        _orphan(tmp_path, wid=f"wf-foreign-{index:03d}",
                created_at=now - 5000 + index,
                workflow_file=f"/var/data/deleted-{index}.json")
    _orphan(tmp_path, wid="wf-valid", created_at=now - 4000,
            workflow_file="/var/data/valid.json")

    first = ws.find_startup_orphan_candidates(db_path, now, 1800)
    assert len(first) == 100 and "wf-valid" not in first
    last = _store(tmp_path).get_workflow(first[-1])
    cursor = (float(last["created_at"]), first[-1])
    second = ws.find_startup_orphan_candidates(db_path, now, 1800, after=cursor)
    assert "wf-valid" in second
    assert len(second) < 100


def test_converge_startup_failure_requires_zero_tasks(tmp_path):
    now = 1_000_000.0
    with_task = _orphan(tmp_path, created_at=now - 10, tasks=1)
    assert ws.converge_startup_failure(
        with_task, "wf-orphan", "preflight", "boom") == {
            "ok": False, "reason": "condition_not_met"}
    assert with_task.get_workflow("wf-orphan")["status"] == "running"

    zero_task = _orphan(tmp_path, wid="wf-zero", created_at=now - 10)
    result = ws.converge_startup_failure(
        zero_task, "wf-zero", "preflight", "boom")
    assert result["ok"] is True and result["new_status"] == "completed"
    record = zero_task.get_workflow("wf-zero")
    assert record["outcome"] == "startup_failed"
    assert record["startup_ready"] is False
    assert record["failed_stage"] == "preflight"


def test_converge_startup_failure_is_idempotent_and_audited(tmp_path):
    now = 1_000_000.0
    store = _orphan(tmp_path, created_at=now - 10)
    assert ws.converge_startup_failure(
        store, "wf-orphan", "startup", "boom")["ok"] is True
    again = ws.converge_startup_failure(store, "wf-orphan", "startup", "boom")
    assert again == {"ok": False, "reason": "condition_not_met"}
    events = store.list_events(
        workflow_id="wf-orphan", event_type="workflow_transition")
    assert len(events) == 1
    assert events[0]["source"] == "herdr-factory"


def test_startup_terminal_does_not_trigger_delivery_validation(tmp_path, monkeypatch):
    now = 1_000_000.0
    store = _orphan(tmp_path, created_at=now - 10)
    calls = []
    monkeypatch.setattr(
        "herdr.workflow.validate_delivery_inventory",
        lambda *a, **k: calls.append(True))
    assert ws.converge_startup_failure(
        store, "wf-orphan", "preflight", "boom")["ok"] is True
    assert calls == []


def test_reclaim_is_atomic_idempotent_and_audited(tmp_path):
    now = 1_000_000.0
    store = _orphan(tmp_path, created_at=now - 1801)
    result = ws.reclaim_startup_orphan(
        store, "wf-orphan", now, 1800, foreign_check=lambda _: False)
    assert result["ok"] is True and result["new_status"] == "completed"

    record = store.get_workflow("wf-orphan")
    assert record["outcome"] == "startup_failed"
    assert record["failed_stage"] == "startup"
    assert record["startup_ready"] is False
    assert 0 < len(record["startup_failure_reason"]) <= 2048

    events = store.list_events(
        workflow_id="wf-orphan", event_type="workflow_transition")
    assert len(events) == 1
    assert events[0]["source"] == "controller-startup-watchdog"
    assert events[0]["payload"]["to_status"] == "completed"

    again = ws.reclaim_startup_orphan(
        store, "wf-orphan", now + 120, 1800, foreign_check=lambda _: False)
    assert again == {"ok": False, "reason": "condition_not_met"}
    assert len(store.list_events(
        workflow_id="wf-orphan", event_type="workflow_transition")) == 1


def test_reclaim_refuses_boundary_and_foreign_records(tmp_path):
    now = 1_000_000.0
    store = _orphan(tmp_path, wid="wf-boundary", created_at=now - 1800)
    assert ws.reclaim_startup_orphan(
        store, "wf-boundary", now, 1800,
        foreign_check=lambda _: False) == {"ok": False, "reason": "condition_not_met"}
    assert store.get_workflow("wf-boundary")["status"] == "running"

    foreign = _orphan(tmp_path, wid="wf-foreign", created_at=now - 5000)
    assert ws.reclaim_startup_orphan(
        foreign, "wf-foreign", now, 1800,
        foreign_check=lambda _: True) == {"ok": False, "reason": "condition_not_met"}
    assert foreign.get_workflow("wf-foreign")["status"] == "running"


def test_reclaim_refuses_when_task_appeared(tmp_path):
    now = 1_000_000.0
    store = _orphan(tmp_path, created_at=now - 1801)
    store.save_task({"task_id": "late", "workflow_id": "wf-orphan",
                     "status": "pending"})
    assert ws.reclaim_startup_orphan(
        store, "wf-orphan", now, 1800,
        foreign_check=lambda _: False) == {"ok": False, "reason": "condition_not_met"}
    assert store.list_events(
        workflow_id="wf-orphan", event_type="workflow_transition") == []


def test_metadata_cas_rejects_terminal_state(tmp_path):
    now = 1_000_000.0
    store = _orphan(tmp_path, created_at=now - 1801)
    ws.reclaim_startup_orphan(
        store, "wf-orphan", now, 1800, foreign_check=lambda _: False)
    with pytest.raises(InvalidTransitionError):
        store.update_workflow_metadata(
            "wf-orphan", {"startup_ready": True}, expected_status="running")
    record = store.get_workflow("wf-orphan")
    assert record["status"] == "completed"
    assert record["startup_ready"] is False


def test_metadata_cas_matches_expected_status(tmp_path):
    now = 1_000_000.0
    store = _orphan(tmp_path, created_at=now - 10)
    result = store.update_workflow_metadata(
        "wf-orphan", {"startup_ready": True}, expected_status="running")
    assert result["status"] == "running"
    assert result["startup_ready"] is True
    assert store.get_workflow("wf-orphan")["startup_ready"] is True
