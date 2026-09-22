import importlib
import threading
import time
from unittest.mock import patch

import pytest

from herdr.intervention import (
    ACTION_RETRY,
    ACTION_VERIFY,
    STATUS_REQUESTED,
    request_intervention,
)
from herdr.state_store import SQLiteStateStore


def _task(run_id="run-1", task_id="task-1"):
    return {
        "run_id": run_id,
        "workflow_id": "wf-1",
        "task_id": task_id,
        "node": "implementation",
        "status": "agent_done",
    }


def _evaluation():
    return {
        "evaluation_id": "eval-1",
        "metadata": {"finding_refs": ["finding-1"], "evidence_refs": ["obs-1"]},
    }


def _decision(action=ACTION_RETRY):
    return {
        "decision_id": "decision-1",
        "evaluation_id": "eval-1",
        "action": action,
        "reason": "supervisor reason",
        "reasons": ["needs retry"],
    }


def test_enforced_decision_creates_durable_intervention_with_provenance(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")

    item = request_intervention(
        store, _task(), _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )

    assert item["action"] == ACTION_RETRY
    assert item["status"] == STATUS_REQUESTED
    assert item["evaluation_id"] == "eval-1"
    assert item["decision_id"] == "decision-1"
    assert item["finding_refs"] == ["finding-1"]
    assert item["evidence_refs"] == ["obs-1"]


def test_observe_only_decision_does_not_create_intervention(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")

    assert request_intervention(
        store, _task(), _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": False, "policy": {"max_verifications": 2}},
    ) is None
    assert store.list_interventions() == []


def test_intervention_rejects_foreign_run_identity(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = _task(run_id="run-a")
    decision = _decision()
    decision["run_id"] = "run-b"

    try:
        request_intervention(
            store, task, _evaluation(), decision,
            {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
        )
    except ValueError as exc:
        assert "run" in str(exc)
    else:
        raise AssertionError("foreign decision run must be rejected")


def test_controller_retry_claims_existing_rework_and_completes(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = _task()
    intervention = request_intervention(
        store, task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )

    fresh = dict(task, status="rework", attempt_count=1)
    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "set_task_status", return_value=True), \
         patch.object(controller, "get_task", return_value=fresh):
        result = controller._execute_supervisor_intervention(
            task, {"intervention": intervention}, controller._supervisor_retry,
        )

    assert result["action"] == ACTION_RETRY
    assert store.get_intervention(intervention["intervention_id"])["status"] == "completed"
    assert not [
        event for event in store.list_events(task_id="task-1", source="trajectory")
        if event["event_type"] == "verification_completed"
    ]


def test_controller_verify_only_enters_rework_without_fabricating_verdict(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status_history=[
        {"from": "rework", "to": "agent_done", "timestamp": 9999999999.0},
    ])
    intervention = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )

    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "set_task_status", return_value=True):
        result = controller._execute_supervisor_intervention(
            task, {"intervention": intervention}, controller._supervisor_verify,
        )

    assert result["verification_pending"] is True
    assert result["new_status"] == "rework"
    assert store.get_intervention(intervention["intervention_id"])["status"] == "completed"
    assert not [
        event for event in store.list_events(task_id="task-1", source="trajectory")
        if event["event_type"] == "verification_completed"
    ]


def test_retry_budget_is_failed_before_controller_handler(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    item = request_intervention(
        store, dict(_task(), attempt_count=2), _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )

    assert item["status"] == "failed"
    assert item["error"]["code"] == "retry_budget_exhausted"
    assert not store.list_interventions(statuses=["requested", "running"])


def test_failed_action_is_durable_and_not_left_running(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    item = request_intervention(
        store, _task(), _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )

    def broken(_task, _decision):
        raise RuntimeError("rework failed")

    with patch.object(controller, "_get_store", return_value=store), \
         pytest.raises(RuntimeError, match="rework failed"):
        controller._execute_supervisor_intervention(
            _task(), {"intervention": item}, broken,
        )
    final = store.get_intervention(item["intervention_id"])
    assert final["status"] == "failed"
    assert final["error"]["type"] == "RuntimeError"


def test_two_controllers_claim_one_intervention_and_execute_once(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    item = request_intervention(
        store, _task(), _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    calls = []
    calls_lock = threading.Lock()

    def handler(_task, _decision):
        with calls_lock:
            calls.append(1)
        time.sleep(0.02)
        return {"executed": True}

    def worker():
        with patch.object(controller, "_get_store", return_value=store):
            controller._execute_supervisor_intervention(
                _task(), {"intervention": item}, handler,
            )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"


def test_recovery_completes_running_retry_when_rework_already_applied(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="rework", attempt_count=1)
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    store.claim_intervention(item["intervention_id"])

    recovered = controller.recover_pending_interventions(store=store, run_id="run-1")

    assert len(recovered) == 1
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"
    assert store.get_intervention(item["intervention_id"])["result"]["already_applied"] is True


def test_controller_checkpoint_runs_policy_to_persisted_retry_action(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    supervisor_harness = importlib.import_module("herdr.supervisor.harness")
    from herdr.supervisor.config import load_config
    from herdr.supervisor.engine import SemanticSupervisor
    from tests.test_semantic_supervisor import ALL_SIGNALS, StubProvider

    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="working", runtime={"status": "running"})
    fresh = dict(task, status="rework", attempt_count=1)
    config = load_config(path="/nonexistent-supervisor.json")
    config.update({"provider": "rule", "enforce": True, "interval": 0, "cooldown": 0})
    signals = dict(ALL_SIGNALS, worker_stuck=0.95, meaningful_progress=0.03)
    supervisor = SemanticSupervisor(config, StubProvider(signals=signals))

    with patch.object(controller, "supervisor_harness", supervisor_harness), \
         patch.object(supervisor_harness, "load_config", return_value=config), \
         patch.object(supervisor_harness, "get_supervisor", return_value=supervisor), \
         patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "get_task", side_effect=[task, fresh]), \
         patch.object(controller, "set_task_status", return_value=True):
        result = controller.supervisor_checkpoint(task, "agent_done")

    assert result["decision"]["action"] == ACTION_RETRY
    assert result["handled"] is True
    interventions = store.list_interventions(run_id="run-1", task_id="task-1")
    assert len(interventions) == 1
    assert interventions[0]["status"] == "completed"
    assert store.list_events(task_id="task-1", event_type="intervention_completed")


def test_completed_v1_intervention_is_not_pending_on_legacy_event_replay(tmp_path):
    from herdr.supervisor.config import load_config
    from herdr.supervisor.harness import pending_intervention

    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status_history=[
        {"from": "rework", "to": "agent_done", "timestamp": 9999999999.0},
    ])
    item = request_intervention(
        store, task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "provider": "rule", "policy": {"max_attempts": 2}},
    )
    store.claim_intervention(item["intervention_id"])
    store.complete_intervention(item["intervention_id"], {"ok": True})
    store.record_event(
        "supervisor_policy", {"action": "RETRY", "enforced": True},
        task_id="task-1", source="semantic_supervisor",
    )
    config = load_config(path="/nonexistent-supervisor.json")
    config.update({"provider": "rule", "enforce": True})

    assert pending_intervention(task, store, config) is None
