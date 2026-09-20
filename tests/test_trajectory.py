import importlib.machinery
import importlib.util
import json
import threading
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr.trajectory import TrajectoryEvent, TrajectoryLedger


def _load_herdr_task_module(name):
    path = Path(__file__).resolve().parent.parent / "bin" / "herdr-task"
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_append_event_can_be_read_with_runtime_identity(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")

    stored = ledger.append_event(
        TrajectoryEvent(
            run_id="run-1",
            task_id="task-1",
            workflow_id="wf-1",
            event_type="agent_started",
            node="implementation",
            stage="implementation",
            agent="claude",
            agent_name="task-1-agent",
            agent_session_id="session-1",
            workspace_id="workspace-1",
            tab_id="tab-1",
            pane_id="pane-1",
        )
    )

    events = ledger.list_events("run-1")

    assert stored["event_id"].startswith("evt_")
    assert events[0]["event_type"] == "agent_started"
    assert events[0]["sequence"] == 1
    assert events[0]["agent"] == "claude"
    assert events[0]["agent_name"] == "task-1-agent"
    assert events[0]["agent_session_id"] == "session-1"
    assert events[0]["workspace_id"] == "workspace-1"
    assert events[0]["tab_id"] == "tab-1"
    assert events[0]["pane_id"] == "pane-1"


def test_events_are_ordered_per_run_and_isolated_between_runs(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")

    ledger.append_event({"run_id": "run-a", "event_type": "task_started"})
    ledger.append_event({"run_id": "run-b", "event_type": "task_started"})
    ledger.append_event({"run_id": "run-a", "event_type": "task_completed"})

    events = ledger.list_events("run-a")

    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["event_type"] for event in events] == [
        "task_started",
        "task_completed",
    ]
    assert [event["run_id"] for event in ledger.list_events("run-b")] == ["run-b"]


def test_optional_fields_are_omitted_without_empty_placeholders(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")

    ledger.append_event(
        {
            "run_id": "run-optional",
            "event_type": "task_started",
            "task_id": None,
            "agent": "",
            "metadata": {"source": "test"},
        }
    )

    event = ledger.list_events("run-optional")[0]

    assert "task_id" not in event
    assert "agent" not in event
    assert event["metadata"] == {"source": "test"}


def test_list_events_supports_minimal_filters(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")

    ledger.append_event(
        {
            "run_id": "run-filter",
            "task_id": "task-1",
            "event_type": "agent_started",
            "agent_session_id": "session-1",
        }
    )
    ledger.append_event(
        {
            "run_id": "run-filter",
            "task_id": "task-2",
            "event_type": "task_started",
            "agent_session_id": "session-2",
        }
    )

    assert len(ledger.list_events("run-filter", event_type="agent_started")) == 1
    assert len(ledger.list_events("run-filter", task_id="task-2")) == 1
    assert len(ledger.list_events("run-filter", agent_session_id="session-2")) == 1


def test_concurrent_appends_have_unique_contiguous_sequences(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")
    errors = []

    def append_event(index):
        try:
            ledger.append_event(
                {"run_id": "run-concurrent", "event_type": "action_completed", "metadata": {"index": index}}
            )
        except Exception as exc:  # pragma: no cover - assertion reports unexpected worker errors
            errors.append(exc)

    threads = [threading.Thread(target=append_event, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    events = ledger.list_events("run-concurrent")
    assert [event["sequence"] for event in events] == list(range(1, 9))


def test_invalid_event_is_rejected(tmp_path: Path):
    ledger = TrajectoryLedger(tmp_path / "state.db")

    with pytest.raises(ValueError, match="run_id"):
        ledger.append_event({"event_type": "task_started"})


def test_event_model_round_trips_structured_fields():
    event = TrajectoryEvent.from_mapping(
        {
            "run_id": "run-model",
            "event_type": "verification_completed",
            "verification": {"type": "tests_completed", "passed": True},
            "metadata": {"evidence_id": "e-1"},
        }
    )

    assert event.to_mapping()["verification"]["passed"] is True
    assert event.to_mapping()["metadata"] == {"evidence_id": "e-1"}


def test_existing_event_table_without_trajectory_columns_is_upgraded(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT,
            node_id TEXT,
            task_id TEXT,
            agent_id TEXT,
            event_type TEXT,
            payload_json TEXT,
            timestamp REAL,
            source TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO events (event_type, payload_json, timestamp, source) VALUES (?, ?, ?, ?)",
        ("task_transition", "{}", 1.0, "legacy"),
    )
    conn.commit()
    conn.close()

    ledger = TrajectoryLedger(db_path)
    ledger.append_event({"run_id": "run-upgrade", "event_type": "task_started"})

    assert len(ledger.list_events("run-upgrade")) == 1


def test_normal_launch_persists_one_new_run_id_for_initial_trajectory_events(tmp_path, monkeypatch):
    task_mod = _load_herdr_task_module("herdr_task_launch_run_id_regression")
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(db_path))

    worker_result = {
        "clone": str(tmp_path / "clone"),
        "branch": "context/task-launch-run-id",
        "pane_id": "pane-1",
        "agent_session_id": "session-1",
        "agent_name": "pi-1",
        "baseline_untracked": [],
        "baseline_fingerprint": {"tracked": {}, "untracked": {}},
    }

    monkeypatch.setattr(
        task_mod,
        "ensure_stage_topology",
        lambda workflow_id, node_id: {
            "workspace_id": "workspace-1",
            "tab_id": "tab-1",
            "anchor_pane_id": "anchor-1",
            "node_label": "Implementation",
            "stage_label": "Implementation",
        },
    )
    monkeypatch.setattr(
        task_mod,
        "project_for_workflow",
        lambda workflow_id: {
            "project_id": "project-1",
            "project_name": "Project",
            "project_root": str(tmp_path),
            "execution": {"mode": "context"},
            "context": {"company": str(tmp_path)},
        },
    )
    monkeypatch.setattr(task_mod, "choose_agent", lambda *args, **kwargs: "pi")
    monkeypatch.setattr(task_mod, "acquire_pane_for_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_mod, "ensure_branch_available", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_mod, "auto_init_task_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_mod, "release_agent_reservation", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_mod, "dispatch_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_mod, "_now", lambda: 1000.0)
    monkeypatch.setattr(
        task_mod.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="HERDR_WORKER_RESULT=" + json.dumps(worker_result),
            stderr="",
        ),
    )

    args = SimpleNamespace(
        task_id="task-launch-run-id",
        workflow_id="workflow-1",
        node="implementation",
        stage=None,
        agent=None,
        task_type="general",
        integration_mode="none",
        onto=None,
        source=str(tmp_path),
        goal="implement",
        acceptance=[],
        test_cmd=None,
        lint_cmd=None,
        repro_cmd=None,
        prompt="implement the task",
    )

    task_mod._launch_task(args)

    task = task_mod._get_store().get_task(args.task_id)
    assert task is not None
    run_id = task["run_id"]
    assert run_id.startswith("run_")
    assert run_id != f"run_{args.task_id}"

    events = TrajectoryLedger(db_path).list_events(run_id)
    assert [event["event_type"] for event in events] == [
        "run_started",
        "task_started",
        "agent_started",
    ]
    assert {event["run_id"] for event in events} == {run_id}
