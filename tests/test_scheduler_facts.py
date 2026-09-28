"""Scheduler decision facts tests (HAFlow PR #107).

覆盖 scheduler_facts 的幂等冻结 + 审计事件读写,
全部使用隔离 tmp DB,不碰生产状态。
"""

import pytest

from herdr import scheduler_facts as facts
from herdr.state_store import get_state_store


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "sched-facts.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(path))
    store = get_state_store(path)
    store.save_workflow({"workflow_id": "wf-facts-01", "status": "running"})
    return path


class TestCandidateFrozen:
    def test_freeze_is_idempotent(self, db):
        first = facts.record_candidate_frozen(
            "wf-facts-01", "sha-A", source_node="implementation", db_path=db
        )
        assert first["status"] == "created"
        second = facts.record_candidate_frozen("wf-facts-01", "sha-A", db_path=db)
        assert second["status"] == "exists"
        events = facts.list_candidate_frozen_events("wf-facts-01", db_path=db)
        assert len(events) == 1

    def test_rotation_records_prior_sha(self, db):
        facts.record_candidate_frozen("wf-facts-01", "sha-A", db_path=db)
        rotated = facts.record_candidate_frozen("wf-facts-01", "sha-B", db_path=db)
        assert rotated["status"] == "created"
        assert rotated["event"]["payload"]["rotated_from"] == "sha-A"
        assert facts.latest_frozen_candidate_sha("wf-facts-01", db_path=db) == "sha-B"

    def test_empty_sha_rejected(self, db):
        with pytest.raises(ValueError):
            facts.record_candidate_frozen("wf-facts-01", "", db_path=db)


class TestDecisionAudit:
    def test_scheduler_decision_roundtrip(self, db):
        facts.record_scheduler_decision(
            "wf-facts-01", ["test", "review"],
            expected_candidate_sha="sha-A",
            dispatched=["wf-facts-01-test-auto"],
            db_path=db,
        )
        store = get_state_store(db)
        events = store.list_events(
            workflow_id="wf-facts-01", event_type=facts.EVENT_SCHEDULER_DECISION
        )
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["ready_nodes"] == ["test", "review"]
        assert payload["dispatched"] == ["wf-facts-01-test-auto"]

    def test_join_gate_verdict_roundtrip(self, db):
        facts.record_join_gate_verdict(
            "wf-facts-01", "wrapup", True, "join_satisfied",
            {"candidate_sha": "sha-A"}, db_path=db,
        )
        store = get_state_store(db)
        events = store.list_events(
            workflow_id="wf-facts-01", event_type=facts.EVENT_JOIN_GATE_VERDICT
        )
        assert len(events) == 1
        assert events[0]["payload"]["passed"] is True
        assert events[0]["node_id"] == "wrapup"
