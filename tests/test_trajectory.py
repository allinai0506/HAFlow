import threading
import sqlite3
from pathlib import Path

import pytest

from herdr.trajectory import TrajectoryEvent, TrajectoryLedger


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
