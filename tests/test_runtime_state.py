#!/usr/bin/env python3
"""Tests for RuntimeState (Task/Workflow State vs Runtime State separation).

Covers:
1. Node execution (task) can be created with an associated RuntimeState.
2. RuntimeState follows execution running -> completed / failed.
3. Legacy executions without RuntimeState still read and transition normally.
4. RuntimeState never alters the original workflow/task state machine outcome.
"""

import time

import pytest

from herdr import runtime_state as rs
from herdr.state_store import SQLiteStateStore, reset_state_store


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    db_file = tmp_path / "state.db"
    tasks_file = tmp_path / "tasks.json"
    wf_file = tmp_path / "workflows.json"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_file))
    monkeypatch.setenv("TASKS_FILE", str(tasks_file))
    monkeypatch.setenv("WORKFLOWS_FILE", str(wf_file))
    reset_state_store()
    yield {"db_file": db_file}
    reset_state_store()


def _task(task_id="t-runtime-1", **overrides):
    base = {
        "task_id": task_id,
        "workflow_id": "wf-runtime",
        "node": "node-123",
        "stage": "node-123",
        "agent": "claude",
        "status": "pending",
    }
    base.update(overrides)
    return base


def test_task_created_with_runtime_state(store_env):
    store = SQLiteStateStore(db_path=store_env["db_file"])
    runtime = rs.build_runtime_state(
        agent="claude",
        agent_session_id="ses_abc123",
        workspace_id="w5",
        tab_id="t8",
        pane_id="p7",
        cwd="/xxx/project",
        status="running",
    )
    task = _task(runtime=runtime)
    store.save_task(task)

    fetched = store.get_task("t-runtime-1")
    assert fetched is not None
    got = rs.get_task_runtime(fetched)
    assert got is not None
    assert got["agent"] == "claude"
    assert got["agent_session_id"] == "ses_abc123"
    assert got["workspace_id"] == "w5"
    assert got["tab_id"] == "t8"
    assert got["pane_id"] == "p7"
    assert got["cwd"] == "/xxx/project"
    assert got["status"] == "running"
    # Legacy top-level execution fields stay untouched.
    assert fetched["status"] == "pending"
    assert fetched["node"] == "node-123"


def test_runtime_follows_task_lifecycle(store_env):
    from herdr import kernel

    store = SQLiteStateStore(db_path=store_env["db_file"])
    store.save_task(_task(
        task_id="t-runtime-2",
        runtime=rs.build_runtime_state(
            agent="claude", workspace_id="w5", tab_id="t8",
            pane_id="p7", cwd="/xxx/project", status="created",
        ),
    ))

    kernel.transition_task(
        task_id="t-runtime-2", to_status="dispatched",
        reason="test", source="test", store=store,
    )
    assert rs.get_task_runtime(store.get_task("t-runtime-2"))["status"] == "running"

    kernel.transition_task(
        task_id="t-runtime-2", to_status="working",
        reason="test", source="test", store=store,
    )
    kernel.transition_task(
        task_id="t-runtime-2", to_status="agent_done",
        reason="test", source="test", store=store,
    )
    assert rs.get_task_runtime(store.get_task("t-runtime-2"))["status"] == "running"

    kernel.transition_task(
        task_id="t-runtime-2", to_status="completed",
        reason="test", source="test", store=store,
    )
    assert rs.get_task_runtime(store.get_task("t-runtime-2"))["status"] == "completed"

    # Failed path.
    store.save_task(_task(
        task_id="t-runtime-3",
        runtime=rs.build_runtime_state(agent="codex", pane_id="p9", status="running"),
    ))
    kernel.transition_task(
        task_id="t-runtime-3", to_status="failed",
        reason="test", source="test", store=store,
    )
    assert rs.get_task_runtime(store.get_task("t-runtime-3"))["status"] == "failed"


def test_legacy_task_without_runtime_still_works(store_env):
    from herdr import kernel

    store = SQLiteStateStore(db_path=store_env["db_file"])
    store.save_task(_task(task_id="t-legacy-1"))

    fetched = store.get_task("t-legacy-1")
    assert rs.get_task_runtime(fetched) is None

    res = kernel.transition_task(
        task_id="t-legacy-1", to_status="dispatched",
        reason="test", source="test", store=store,
    )
    assert res["new_status"] == "dispatched"
    after = store.get_task("t-legacy-1")
    assert after["status"] == "dispatched"
    assert rs.get_task_runtime(after) is None


def test_runtime_does_not_change_task_outcome(store_env):
    from herdr import kernel

    store = SQLiteStateStore(db_path=store_env["db_file"])
    store.save_task(_task(
        task_id="t-runtime-4",
        runtime=rs.build_runtime_state(agent="claude", pane_id="p7", status="running"),
    ))

    res = kernel.transition_task(
        task_id="t-runtime-4", to_status="dispatched",
        reason="test", source="test", store=store,
        metadata={"stage_verdict": "pass"},
    )
    task = res["task"]
    assert task["status"] == "dispatched"
    assert task["stage_verdict"] == "pass"
    # Runtime keys never leak into protected task identity fields.
    assert task["agent"] == "claude"
    assert task["node"] == "node-123"
    assert task["task_id"] == "t-runtime-4"


def test_normalize_rejects_unknown_status_and_keys():
    raw = {
        "agent": "claude",
        "pane_id": "p7",
        "status": "flying",
        "hacker_field": "drop-me",
        "workspace_id": "w5",
    }
    norm = rs.normalize_runtime_state(raw)
    assert norm is not None
    assert "hacker_field" not in norm
    # Unknown status falls back to a safe default instead of persisting garbage.
    assert norm["status"] in rs.RUNTIME_STATUSES
    assert norm["agent"] == "claude"
    assert norm["pane_id"] == "p7"


def test_build_runtime_needs_real_evidence():
    assert rs.build_runtime_state() is None
    assert rs.build_runtime_state(status="running") is None
    minimal = rs.build_runtime_state(pane_id="p7")
    assert minimal is not None
    assert minimal["status"] == "running"
    assert minimal["pane_id"] == "p7"
