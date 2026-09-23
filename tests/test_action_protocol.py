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
from herdr.supervisor.engine import SemanticSupervisor
from herdr.supervisor.harness import run_checkpoint
from herdr.supervisor.config import load_config
from tests.test_semantic_supervisor import ALL_SIGNALS, StubProvider


def _task(run_id="run-1", task_id="task-1"):
    return {
        "run_id": run_id,
        "workflow_id": "wf-1",
        "task_id": task_id,
        "node": "implementation",
        "pane_id": "w1:p1",
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
         patch.object(controller, "get_task", return_value=fresh), \
         patch.object(controller.subprocess, "run", return_value=type("Result", (), {
             "returncode": 0, "stdout": "", "stderr": "",
         })()):
        result = controller._execute_supervisor_intervention(
            task, {"intervention": intervention}, controller._supervisor_retry,
        )

    assert result["action"] == ACTION_RETRY
    assert store.get_intervention(intervention["intervention_id"])["status"] == "completed"
    assert not [
        event for event in store.list_events(task_id="task-1", source="trajectory")
        if event["event_type"] == "verification_completed"
    ]


def test_two_recovery_controllers_claim_running_intervention_once(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    db_path = tmp_path / "state.db"
    stores = [SQLiteStateStore(db_path), SQLiteStateStore(db_path)]
    task = dict(_task(), status="working")
    stores[0].save_task(task)
    item = request_intervention(
        stores[0], task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    stores[0].claim_intervention(item["intervention_id"], lease_seconds=-1)
    calls = []
    calls_lock = threading.Lock()

    def handler(_task, _decision, **_kwargs):
        with calls_lock:
            calls.append(1)
        time.sleep(0.05)
        return {"action": ACTION_RETRY, "execution_evidence": True}

    with patch.object(controller, "_supervisor_retry", side_effect=handler):
        threads = [
            threading.Thread(
                target=controller.recover_pending_interventions,
                kwargs={"store": store, "run_id": "run-1"},
            )
            for store in stores
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert calls == [1]
    assert stores[0].get_intervention(item["intervention_id"])["status"] == "completed"


def test_recovery_does_not_repeat_retry_after_task_transition_evidence(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="agent_done")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    store.claim_intervention(item["intervention_id"], lease_seconds=-1)
    store.transition_task(
        task["task_id"], "rework", "supervisor retry",
        source="supervisor",
        metadata={
            "intervention_id": item["intervention_id"],
            "decision_id": item["decision_id"],
            "action": ACTION_RETRY,
            "attempt_count": 1,
        },
    )
    calls = []

    store.record_event(
        "retry_dispatched",
        {"intervention_id": item["intervention_id"], "decision_id": item["decision_id"],
         "action": ACTION_RETRY, "attempt_count": 1},
        task_id=task["task_id"], workflow_id=task["workflow_id"],
        source="supervisor", run_id=task["run_id"],
    )

    def should_not_execute(_task, _decision, **_kwargs):
        calls.append(1)
        raise AssertionError("retry was applied twice")

    with patch.object(controller, "_supervisor_retry", side_effect=should_not_execute):
        result = controller._execute_supervisor_intervention(
            store.get_task(task["task_id"]),
            {"intervention": item, "_recovery": True},
            controller._supervisor_retry,
            store=store,
        )

    assert calls == []
    assert result["execution_evidence"] is True
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"


def test_fresh_retry_while_task_is_rework_has_new_execution_evidence(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="rework", attempt_count=1)
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )

    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller.subprocess, "run", return_value=type("Result", (), {
             "returncode": 0, "stdout": "", "stderr": "",
         })()):
        result = controller._execute_supervisor_intervention(
            task, {"intervention": item}, controller._supervisor_retry, store=store,
        )

    assert result.get("already_applied") is not True
    assert result["new_status"] == "working"
    assert result["attempt_count"] == 2
    events = store.list_events(task_id=task["task_id"], event_type="task_transition")
    assert any(
        event["payload"].get("intervention_id") == item["intervention_id"]
        and event["payload"].get("action") == ACTION_RETRY
        for event in events
    )


def test_verify_result_reflects_fresh_working_task_state(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="working")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    with patch.object(
        controller.subprocess, "run",
        return_value=type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    ):
        result = controller._execute_supervisor_intervention(
            task, {"intervention": item}, controller._supervisor_verify, store=store,
        )
    fresh = store.get_task(task["task_id"])

    assert result["verification_pending"] is True
    assert result["new_status"] == fresh["status"]
    assert result["new_status"] == "rework"
    assert result["verification_requested"] is True


def test_verify_dispatches_real_agent_execution_with_provenance(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="working", pane_id="w1:p1")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    calls = []

    def prompt(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(controller.subprocess, "run", side_effect=prompt):
        result = controller._execute_supervisor_intervention(
            task, {"intervention": item}, controller._supervisor_verify, store=store,
        )

    assert calls[0][0][:3] == ["herdr", "agent", "prompt"]
    assert calls[0][0][3] == "w1:p1"
    assert item["intervention_id"] in calls[0][0][4]
    assert "decision-1" in calls[0][0][4]
    assert result["verification_dispatched"] is True
    dispatches = store.list_events(task_id="task-1", event_type="verification_dispatched")
    assert dispatches[0]["payload"]["intervention_id"] == item["intervention_id"]
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"


def test_verify_dispatch_evidence_prevents_recovery_redispatch(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="working", pane_id="w1:p1")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    store.claim_intervention(item["intervention_id"], lease_seconds=-1)
    calls = []

    def prompt(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(controller.subprocess, "run", side_effect=prompt):
        controller._supervisor_verify(task, {"intervention": item}, store=store)
        result = controller._execute_supervisor_intervention(
            task, {"intervention": item, "_recovery": True},
            controller._supervisor_verify, store=store,
        )

    assert len(calls) == 1
    assert result["execution_evidence"] is True
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"


def test_old_deliverables_cannot_bypass_pending_verify_in_watchdog(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="rework", pane_id="w1:p1")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    store.record_event(
        "verification_dispatched",
        {"intervention_id": item["intervention_id"], "action": ACTION_VERIFY},
        task_id=task["task_id"], workflow_id=task["workflow_id"],
        source="supervisor", run_id=task["run_id"],
    )
    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "get_task", return_value=task), \
         patch.object(controller, "check_task_deliverables_ready", return_value=True), \
         patch.object(controller, "set_task_status") as set_status, \
         patch.object(controller.subprocess, "run", return_value=type(
             "Result", (), {"returncode": 0, "stdout": "", "stderr": ""}
         )()):
        controller.handle_event(task["task_id"], "idle")
    set_status.assert_not_called()

    store.record_event(
        "verification_completed",
        {"verification": {"intervention_id": item["intervention_id"]}},
        task_id=task["task_id"], workflow_id=task["workflow_id"],
        source="trajectory", run_id=task["run_id"],
    )
    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "get_task", return_value=task), \
         patch.object(controller, "check_task_deliverables_ready", return_value=True), \
         patch.object(controller, "set_task_status", return_value=False) as set_status, \
         patch.object(controller.subprocess, "run", return_value=type(
             "Result", (), {"returncode": 0, "stdout": "", "stderr": ""}
         )()):
        controller.handle_event(task["task_id"], "idle")
    set_status.assert_called_once_with(task["task_id"], "agent_done")


def test_old_eval_done_is_not_new_verify_evidence(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    loop_dir = tmp_path / ".herdr-loop"
    loop_dir.mkdir()
    eval_done = loop_dir / "EVAL_DONE.json"
    eval_done.write_text(
        '{"iteration": 4, "completed_at": 100.0, "total_tests": 10, '
        '"passed_tests": 10, "converged": true}',
        encoding="utf-8",
    )
    task = dict(_task(), clone_path=str(tmp_path))
    baseline = controller._verification_snapshot(str(tmp_path))
    dispatch = {
        "timestamp": 200.0,
        "payload": {
            "dispatch_started_at": 200.0,
            "evidence_baseline": baseline,
        },
    }
    assert controller._verification_evidence_is_new(
        dispatch, {"iteration": 4, "snapshot_sha256": baseline["sha256"],
                   "snapshot_completed_at": 100.0}
    ) is False


def test_failed_verify_remains_a_blocking_rework_condition(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="rework")
    store.save_task(task)
    failed = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 0}},
    )
    assert failed["status"] == "failed"
    assert controller._verification_execution_complete_for_rework(task, store) is False


def test_verify_dispatch_intent_recovery_does_not_prompt_twice(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="working", pane_id="w1:p1")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    store.claim_intervention(item["intervention_id"], lease_seconds=-1)
    store.record_event(
        "verification_dispatch_intent",
        {"intervention_id": item["intervention_id"], "decision_id": "decision-1",
         "action": ACTION_VERIFY, "pane_id": "w1:p1", "dispatch_started_at": 10.0},
        task_id=task["task_id"], workflow_id=task["workflow_id"],
        source="supervisor", run_id=task["run_id"],
    )
    calls = []
    with patch.object(controller.subprocess, "run", side_effect=lambda *args, **kwargs: calls.append(args)):
        result = controller._execute_supervisor_intervention(
            task, {"intervention": item, "_recovery": True},
            controller._supervisor_verify, store=store,
        )
    assert calls == []
    assert result["dispatch_recovered"] is True
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"


def test_verify_prompt_success_before_receipt_crash_does_not_prompt_again(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="working", pane_id="w1:p1")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    store.claim_intervention(
        item["intervention_id"], execution_owner="crashed-controller",
        lease_seconds=-1,
    )
    original_record = store.record_event
    calls = []

    def crash_after_prompt(event_type, payload, **kwargs):
        if event_type == "verification_dispatched":
            raise SystemExit("controller crash after prompt acceptance")
        return original_record(event_type, payload, **kwargs)

    def prompt(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch.object(controller.subprocess, "run", side_effect=prompt), \
         patch.object(store, "record_event", side_effect=crash_after_prompt), \
         pytest.raises(SystemExit):
        controller._supervisor_verify(task, {"intervention": item}, store=store)
    recovered = controller.recover_pending_interventions(store=store, run_id=task["run_id"])
    assert len(calls) == 1
    assert recovered[0]["result"]["dispatch_recovered"] is True
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"


def test_verify_dispatch_failure_blocks_rework_healing(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="working", pane_id="w1:p1")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    failed_result = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "send failed"})()
    with patch.object(controller.subprocess, "run", return_value=failed_result), \
         pytest.raises(RuntimeError, match="dispatch failed"):
        controller._execute_supervisor_intervention(
            task, {"intervention": item}, controller._supervisor_verify, store=store,
        )
    assert store.get_intervention(item["intervention_id"])["status"] == "failed"
    assert controller._verification_execution_complete_for_rework(
        dict(task, status="rework"), store,
    ) is False


def test_durable_ledger_read_failure_blocks_done(tmp_path):
    controller = importlib.import_module("services.herdr-controller")

    class BrokenLedger:
        def list_interventions(self, **_kwargs):
            raise RuntimeError("ledger unavailable")

    task = dict(_task(), status="agent_done")
    with patch.object(controller, "_get_store", return_value=BrokenLedger()), \
         patch.object(controller, "enqueue_coordinator_event") as enqueue, \
         patch.object(controller, "_supervisor_action_recovery_enabled", return_value=True):
        assert controller.emit_done_if_allowed(task) is False
    enqueue.assert_not_called()


def test_pending_intervention_raises_when_durable_ledger_is_unknown(tmp_path):
    from herdr.supervisor.config import load_config
    from herdr.supervisor.harness import pending_intervention

    class BrokenLedger:
        def list_interventions(self, **_kwargs):
            raise RuntimeError("ledger unavailable")

    config = load_config(path="/nonexistent-supervisor.json")
    config.update({"provider": "rule", "enforce": True})
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        pending_intervention(_task(), BrokenLedger(), config)


def test_verify_budget_is_durable_and_rejects_third_request(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = _task()
    config = {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}}
    for index in range(2):
        item = request_intervention(
            store, task, {"evaluation_id": f"eval-{index}"},
            {"decision_id": f"decision-{index}", "action": ACTION_VERIFY}, config,
        )
        assert item["status"] == STATUS_REQUESTED

    rejected = request_intervention(
        SQLiteStateStore(tmp_path / "state.db"), task,
        {"evaluation_id": "eval-2"},
        {"decision_id": "decision-2", "action": ACTION_VERIFY}, config,
    )
    assert rejected["status"] == "failed"
    assert rejected["error"]["code"] == "verification_budget_exhausted"
    assert rejected["error"]["verification_count"] == 2


def test_verify_budget_is_atomic_across_two_sqlite_connections(tmp_path):
    db_path = tmp_path / "state.db"
    seed = SQLiteStateStore(db_path)
    task = _task()
    config = {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}}
    seed_item = request_intervention(
        seed, task, {"evaluation_id": "eval-seed"},
        {"decision_id": "decision-seed", "action": ACTION_VERIFY}, config,
    )
    assert seed_item["status"] == STATUS_REQUESTED

    barrier = threading.Barrier(2)

    class CoordinatedStore(SQLiteStateStore):
        def list_interventions(self, *args, **kwargs):
            rows = super().list_interventions(*args, **kwargs)
            barrier.wait(timeout=5)
            return rows

    stores = [CoordinatedStore(db_path), CoordinatedStore(db_path)]
    results = []

    def request(store, index):
        results.append(request_intervention(
            store, task, {"evaluation_id": f"eval-{index}"},
            {"decision_id": f"decision-{index}", "action": ACTION_VERIFY}, config,
        ))

    threads = [threading.Thread(target=request, args=(store, index))
               for index, store in enumerate(stores)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(item["status"] for item in results) == ["failed", STATUS_REQUESTED]
    assert sum(item["status"] == STATUS_REQUESTED for item in results) == 1
    accepted = [item for item in SQLiteStateStore(db_path).list_interventions()
                if item["status"] in {"requested", "running", "completed"}]
    assert len(accepted) == 2


def test_request_failure_does_not_leave_legacy_pending(tmp_path):
    from herdr.supervisor.config import load_config
    from herdr.supervisor.harness import pending_intervention

    class BrokenStore:
        def __init__(self):
            self.events = []

        def list_events(self, **_kwargs):
            return self.events

        def record_event(self, event_type, payload, **kwargs):
            self.events.append({"event_type": event_type, "payload": payload, **kwargs})

        def create_intervention(self, _item):
            raise RuntimeError("state store unavailable")

        def list_interventions(self, **_kwargs):
            return []

    config = load_config(path="/nonexistent-supervisor.json")
    config.update({"provider": "rule", "enforce": True, "interval": 0, "cooldown": 0})
    supervisor = SemanticSupervisor(
        config, StubProvider(signals=dict(ALL_SIGNALS, worker_stuck=0.95, meaningful_progress=0.03)),
    )
    store = BrokenStore()
    result = run_checkpoint(
        task=dict(_task(), runtime={"status": "running", "started_at": time.time() - 10}),
        trigger="agent_done", store=store, config=config, supervisor=supervisor,
        actions={"RETRY": lambda *_args: pytest.fail("handler must not run")},
        log=lambda _message: None,
    )

    assert result["continue_flow"] is True
    assert result["intercepted"] is False
    assert pending_intervention(_task(), store, config) is None
    policy_events = [event for event in store.events if event["event_type"] == "supervisor_policy"]
    assert policy_events[-1]["payload"]["enforced"] is False


def test_request_persistence_failure_is_fail_safe(tmp_path):
    class BrokenStore:
        def __init__(self):
            self.events = []

        def list_events(self, **_kwargs):
            return self.events

        def record_event(self, event_type, payload, **kwargs):
            self.events.append({"event_type": event_type, "payload": payload, **kwargs})

        def create_intervention(self, _item):
            raise RuntimeError("state store unavailable")

    config = load_config(path="/nonexistent-supervisor.json")
    config.update({"provider": "rule", "enforce": True, "interval": 0, "cooldown": 0})
    signals = dict(ALL_SIGNALS, worker_stuck=0.95, meaningful_progress=0.03)
    supervisor = SemanticSupervisor(config, StubProvider(signals=signals))
    executed = []

    result = run_checkpoint(
        task=_task(), trigger="agent_done", store=BrokenStore(), config=config,
        supervisor=supervisor,
        actions={"RETRY": lambda *_args: executed.append(1)}, log=lambda _message: None,
    )

    assert result["intercepted"] is False
    assert result["handled"] is False
    assert result["continue_flow"] is True
    assert executed == []


def test_durable_handler_failure_remains_intercepted(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    from herdr.supervisor.engine import SemanticSupervisor

    store = SQLiteStateStore(tmp_path / "state.db")
    config = load_config(path="/nonexistent-supervisor.json")
    config.update({"provider": "rule", "enforce": True, "interval": 0, "cooldown": 0})
    supervisor = SemanticSupervisor(
        config, StubProvider(signals=dict(ALL_SIGNALS, worker_stuck=0.95, meaningful_progress=0.03)),
    )
    task = dict(_task(), runtime={"status": "running", "started_at": time.time() - 10})

    def broken(_task, _decision):
        raise RuntimeError("boom")

    result = run_checkpoint(
        task=task, trigger="agent_done", store=store, config=config,
        supervisor=supervisor,
        actions={
            "RETRY": lambda task, decision: controller._execute_supervisor_intervention(
                task, decision, broken, store=store,
            ),
        },
        log=lambda _message: None,
    )

    assert result["intercepted"] is True
    assert result["continue_flow"] is False
    assert store.list_interventions(statuses=["failed"])[0]["error"]["type"] == "RuntimeError"


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
         patch.object(controller, "set_task_status", return_value=True), \
         patch.object(
             controller.subprocess, "run",
             return_value=type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
         ):
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
    task = dict(_task(), status="agent_done")
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    store.claim_intervention(item["intervention_id"], lease_seconds=-1)
    store.transition_task(
        task["task_id"], "rework", "supervisor retry", source="supervisor",
        metadata={
            "intervention_id": item["intervention_id"],
            "decision_id": item["decision_id"],
            "action": ACTION_RETRY,
            "attempt_count": 1,
        },
    )
    store.record_event(
        "retry_dispatched",
        {"intervention_id": item["intervention_id"], "decision_id": item["decision_id"],
         "action": ACTION_RETRY, "attempt_count": 1},
        task_id=task["task_id"], workflow_id=task["workflow_id"],
        source="supervisor", run_id=task["run_id"],
    )

    recovered = controller.recover_pending_interventions(store=store, run_id="run-1")

    assert len(recovered) == 1
    assert store.get_intervention(item["intervention_id"])["status"] == "completed"
    assert store.get_intervention(item["intervention_id"])["result"]["execution_evidence"] is True


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
         patch.object(controller, "set_task_status", return_value=True), \
         patch.object(controller.subprocess, "run", return_value=type("Result", (), {
             "returncode": 0, "stdout": "", "stderr": "",
         })()):
        result = controller.supervisor_checkpoint(task, "agent_done")

    assert result["decision"]["action"] == ACTION_RETRY
    assert result["handled"] is True
    interventions = store.list_interventions(run_id="run-1", task_id="task-1")
    assert len(interventions) == 1
    assert interventions[0]["status"] == "completed"
    assert store.list_events(task_id="task-1", event_type="intervention_completed")


def test_recovery_done_gateway_is_task_scoped(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task_a = dict(_task(task_id="task-a"), status="agent_done")
    task_b = dict(_task(task_id="task-b"), status="agent_done")
    store.save_task(task_a)
    store.save_task(task_b)
    item_b = request_intervention(
        store, task_b, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    with patch.object(controller, "_supervisor_retry", side_effect=AssertionError("foreign task recovered")):
        recovered = controller.recover_pending_interventions(
            store=store, run_id="run-1", task_id="task-a",
        )
    assert recovered == []
    assert store.get_intervention(item_b["intervention_id"])["status"] == "requested"


def test_failed_verify_from_prior_agent_done_episode_does_not_block_rework(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="rework", status_history=[
        {"from": "rework", "to": "agent_done", "timestamp": 9999999999.0},
    ])
    store.save_task(task)
    item = request_intervention(
        store, task, _evaluation(), _decision(ACTION_VERIFY),
        {"enabled": True, "enforce": True, "policy": {"max_verifications": 2}},
    )
    store.fail_intervention(item["intervention_id"], {"code": "dispatch_failed"})
    assert controller._verification_execution_complete_for_rework(task, store) is True


def test_retry_without_dispatch_cannot_be_healed_by_old_deliverables(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="rework")
    store.save_task(task)
    request_intervention(
        store, task, _evaluation(), _decision(ACTION_RETRY),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    assert controller._retry_execution_complete_for_rework(task, store) is False


def test_supervisor_kill_switch_skips_existing_intervention_recovery(tmp_path):
    controller = importlib.import_module("services.herdr-controller")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = dict(_task(), status="agent_done")
    store.save_task(task)
    request_intervention(
        store, task, _evaluation(), _decision(),
        {"enabled": True, "enforce": True, "policy": {"max_attempts": 2}},
    )
    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "_supervisor_action_recovery_enabled", return_value=False), \
         patch.object(controller, "recover_pending_interventions", side_effect=AssertionError("recovery ran")), \
         patch.object(controller, "supervisor_checkpoint", return_value=None), \
         patch.object(controller, "_observer_terminal_checkpoint"), \
         patch.object(controller, "_schedule_context_compact"), \
         patch.object(controller, "enqueue_coordinator_event") as enqueue:
        assert controller.emit_done_if_allowed(task) is True
    enqueue.assert_called_once_with(task, "done")


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
