import threading

import pytest

from herdr.intervention import (
    ACTION_RETRY,
    STATUS_COMPLETED,
    STATUS_REQUESTED,
    Intervention,
)
from herdr.state_store import SQLiteStateStore


def _requested(**overrides):
    value = {
        "run_id": "run-1",
        "workflow_id": "wf-1",
        "task_id": "task-1",
        "evaluation_id": "eval-1",
        "decision_id": "decision-1",
        "action": ACTION_RETRY,
        "reason": "retry requested",
        "finding_refs": ["finding-1"],
        "evidence_refs": ["observation-1"],
        "attempt": 0,
        "max_attempts": 2,
    }
    value.update(overrides)
    return value


def test_intervention_round_trip_and_lifecycle_events(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")

    created = store.create_intervention(_requested())
    assert created["status"] == STATUS_REQUESTED
    assert created["intervention_id"]
    assert created["identity_key"] == "run-1:task-1:decision-1:RETRY"

    claimed = store.claim_intervention(created["intervention_id"])
    assert claimed["status"] == "running"

    completed = store.complete_intervention(
        created["intervention_id"],
        {"action": ACTION_RETRY, "previous_status": "agent_done", "new_status": "rework"},
    )
    assert completed["status"] == STATUS_COMPLETED

    events = store.list_events(task_id="task-1", source="intervention")
    assert [event["event_type"] for event in events] == [
        "intervention_requested",
        "intervention_started",
        "intervention_completed",
    ]


def test_duplicate_decision_returns_one_canonical_intervention(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")

    first = store.create_intervention(_requested())
    second = store.create_intervention(_requested(reason="replayed"))

    assert second["intervention_id"] == first["intervention_id"]
    assert len(store.list_interventions(run_id="run-1", task_id="task-1")) == 1


def test_completed_intervention_cannot_be_claimed_or_completed_again(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    intervention = store.create_intervention(_requested())
    store.claim_intervention(intervention["intervention_id"])
    store.complete_intervention(intervention["intervention_id"], {"ok": True})

    assert store.claim_intervention(intervention["intervention_id"]) is None
    with pytest.raises(ValueError, match="completed"):
        store.complete_intervention(intervention["intervention_id"], {"ok": False})


def test_concurrent_creation_has_one_canonical_row(tmp_path):
    db_path = tmp_path / "state.db"
    barrier = threading.Barrier(2)
    results = []
    stores = [SQLiteStateStore(db_path), SQLiteStateStore(db_path)]

    def create(store):
        barrier.wait()
        results.append(store.create_intervention(_requested()))

    threads = [threading.Thread(target=create, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({item["intervention_id"] for item in results}) == 1
    assert len(SQLiteStateStore(db_path).list_interventions()) == 1


def test_intervention_model_rejects_unknown_action():
    with pytest.raises(ValueError, match="action"):
        Intervention.from_mapping(_requested(action="REROUTE"))
