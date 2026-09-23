"""Collaboration store tests (SQLite, tmp DB, no production writes)."""

import sys
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import state_db


def _event(**overrides):
    ev = {
        "run_id": "run-1",
        "workflow_id": "wf-1",
        "from_task_id": "task-a",
        "from_agent": "developer",
        "to_task_id": "task-b",
        "to_agent": "reviewer",
        "type": "HANDOFF",
        "summary": "Rate-limit done.",
        "artifact_refs": ["commit:abc123"],
        "evidence_refs": ["test:x"],
        "context_refs": [],
        "requires_response": True,
        "source_fact_id": "fact-1",
    }
    ev.update(overrides)
    return ev


def test_create_get_list_roundtrip(tmp_path):
    db = tmp_path / "state.db"
    created = state_db.create_collaboration_event(_event(), db_path=db)
    assert created["status"] == "created"
    assert created["identity_key"] == "run-1:task-a:task-b:HANDOFF:fact-1"
    fetched = state_db.get_collaboration_event(created["event_id"], db_path=db)
    assert fetched is not None and fetched["event_id"] == created["event_id"]
    rows = state_db.list_collaboration_events(run_id="run-1", db_path=db)
    assert len(rows) == 1


def test_duplicate_create_idempotent(tmp_path):
    db = tmp_path / "state.db"
    first = state_db.create_collaboration_event(_event(), db_path=db)
    second = state_db.create_collaboration_event(_event(), db_path=db)
    assert second["event_id"] == first["event_id"]
    rows = state_db.list_collaboration_events(run_id="run-1", db_path=db)
    assert len(rows) == 1


def test_status_lifecycle_timestamps(tmp_path):
    db = tmp_path / "state.db"
    ev = state_db.create_collaboration_event(_event(), db_path=db)
    eid = ev["event_id"]
    d = state_db.mark_collaboration_dispatched(eid, db_path=db)
    assert d["status"] == "dispatched" and d["dispatched_at"] is not None
    a = state_db.mark_collaboration_acknowledged(eid, db_path=db)
    assert a["status"] == "acknowledged" and a["acknowledged_at"] is not None
    c = state_db.mark_collaboration_completed(eid, db_path=db)
    assert c["status"] == "completed" and c["completed_at"] is not None


def test_invalid_transition_rejected(tmp_path):
    db = tmp_path / "state.db"
    ev = state_db.create_collaboration_event(_event(), db_path=db)
    try:
        state_db.mark_collaboration_completed(ev["event_id"], db_path=db)
    except ValueError:
        pass
    else:
        raise AssertionError("created -> completed must be rejected")


def test_ack_idempotent_single_write(tmp_path):
    db = tmp_path / "state.db"
    ev = state_db.create_collaboration_event(_event(), db_path=db)
    eid = ev["event_id"]
    state_db.mark_collaboration_dispatched(eid, db_path=db)
    first = state_db.mark_collaboration_acknowledged(eid, db_path=db)
    second = state_db.mark_collaboration_acknowledged(eid, db_path=db)
    assert second["acknowledged_at"] == first["acknowledged_at"]


def test_run_scoped_isolation(tmp_path):
    db = tmp_path / "state.db"
    state_db.create_collaboration_event(_event(run_id="run-A"), db_path=db)
    state_db.create_collaboration_event(
        _event(run_id="run-B", from_task_id="task-a", to_task_id="task-b",
               source_fact_id="fact-1"), db_path=db)
    rows_a = state_db.list_collaboration_events(run_id="run-A", db_path=db)
    rows_b = state_db.list_collaboration_events(run_id="run-B", db_path=db)
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0]["run_id"] == "run-A"
