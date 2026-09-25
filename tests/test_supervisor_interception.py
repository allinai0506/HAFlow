"""Policy intervention interception semantics.

Regression coverage for the P1 conflict between Semantic Supervisor
interventions and the controller's default ``agent_done -> done`` flow:

- an enforced intervention (RETRY/REROUTE/PAUSE/VERIFY/ESCALATE) must set
  ``continue_flow=False`` so the caller never emits the normal done event;
- observe mode (enforce=false) and pass-through actions (CONTINUE/FINISH)
  keep the original flow;
- an unmapped intervention is intercepted too - it never silently continues;
- the controller honors ``continue_flow`` at every done call site.
"""

import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr.supervisor.engine import SemanticSupervisor
from herdr.supervisor.harness import run_checkpoint
from herdr.supervisor.policy import INTERVENTION_ACTIONS, PASS_THROUGH_ACTIONS
from herdr.supervisor.config import load_config

from tests.test_semantic_supervisor import (  # reuse doubles
    ALL_SIGNALS,
    FakeStore,
    StubProvider,
    _task,
)


def _config(enforce):
    config = load_config(path="/nonexistent-supervisor.json")
    config["enforce"] = enforce
    config["interval"] = 0
    config["cooldown"] = 0
    return config


def _run(enforce, signals=None, actions=None):
    provider = StubProvider(signals=signals or dict(
        ALL_SIGNALS, worker_stuck=0.95, meaningful_progress=0.03))
    config = _config(enforce)
    executed = []

    def handler(task, decision):
        executed.append((task.get("task_id"), decision.get("action")))

    result = run_checkpoint(
        task=_task(), trigger="agent_done", store=FakeStore(), config=config,
        supervisor=SemanticSupervisor(config, provider),
        actions=actions if actions is not None else {"RETRY": handler},
        log=lambda _m: None,
    )
    return result, executed


class ActionClassTests(unittest.TestCase):
    def test_action_classes_are_exhaustive_and_disjoint(self):
        self.assertEqual(
            INTERVENTION_ACTIONS | PASS_THROUGH_ACTIONS,
            {"CONTINUE", "FINISH", "VERIFY", "RETRY", "REROUTE", "PAUSE", "ESCALATE"},
        )
        self.assertEqual(INTERVENTION_ACTIONS & PASS_THROUGH_ACTIONS, set())
        self.assertEqual(PASS_THROUGH_ACTIONS, {"CONTINUE", "FINISH"})


class HarnessInterceptionTests(unittest.TestCase):
    def test_enforced_retry_intercepts_and_handles(self):
        result, executed = _run(enforce=True)
        self.assertEqual(result["decision"]["action"], "RETRY")
        self.assertTrue(result["intercepted"])
        self.assertTrue(result["handled"])
        self.assertFalse(result["continue_flow"], "RETRY must block the done flow")
        self.assertEqual(executed, [("t-1", "RETRY")])

    def test_observe_mode_does_not_intercept(self):
        result, executed = _run(enforce=False)
        self.assertEqual(result["decision"]["action"], "RETRY")
        self.assertFalse(result["intercepted"])
        self.assertFalse(result["handled"])
        self.assertTrue(result["continue_flow"])
        self.assertEqual(executed, [], "observe mode executes no handler")

    def test_enforced_unmapped_intervention_never_silently_continues(self):
        result, _ = _run(enforce=True, actions={})
        self.assertEqual(result["decision"]["action"], "RETRY")
        self.assertTrue(result["intercepted"])
        self.assertFalse(result["handled"])
        self.assertFalse(result["continue_flow"],
                         "unmapped intervention must not fall through to done")

    def test_handler_crash_still_blocks_default_flow(self):
        def bad_handler(task, decision):
            raise RuntimeError("rework unavailable")

        result, _ = _run(enforce=True, actions={"RETRY": bad_handler})
        self.assertTrue(result["intercepted"])
        self.assertFalse(result["handled"])
        self.assertFalse(result["continue_flow"])

    def test_finish_passes_through_to_done(self):
        signals = dict.fromkeys(ALL_SIGNALS, 0.05)
        signals.update({"ready_to_finish": 0.95, "requirements_satisfied": 0.95,
                        "tests_sufficient": 0.95, "meaningful_progress": 0.9})
        result, _ = _run(enforce=True, signals=signals, actions={})
        self.assertEqual(result["decision"]["action"], "FINISH")
        self.assertFalse(result["intercepted"])
        self.assertTrue(result["continue_flow"])

    def test_continue_passes_through_to_done(self):
        result, _ = _run(enforce=True, signals=dict.fromkeys(ALL_SIGNALS, 0.05),
                         actions={})
        self.assertEqual(result["decision"]["action"], "CONTINUE")
        self.assertFalse(result["intercepted"])
        self.assertTrue(result["continue_flow"])


class _FakeHarness:
    def __init__(self, result, pending=None):
        self.result = result
        self.pending = pending
        self.calls = []

    def run_checkpoint(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def pending_intervention(self, task, store, config=None):
        return self.pending


class ControllerDoneGatingTests(unittest.TestCase):
    """handle_event must not emit done when supervision intercepted it."""

    def setUp(self):
        self.controller = importlib.import_module("services.herdr-controller")
        self.addCleanup(setattr, self.controller, "supervisor_harness",
                        self.controller.supervisor_harness)
        os.environ["HERDR_CONTROLLER_TEST"] = "1"
        self.addCleanup(os.environ.pop, "HERDR_CONTROLLER_TEST", None)

    def _handle_done_event(self, harness_result, pending=None):
        task = {
            "task_id": "t-int-1",
            "workflow_id": "wf-int",
            "stage": "implementation",
            "status": "working",
            "pane_id": "w1:p1",
        }
        harness = _FakeHarness(harness_result, pending=pending)
        self.controller.supervisor_harness = harness
        def accept_durable_completion(*_args, **_kwargs):
            task["status"] = "agent_done"
            self.controller.emit_done_if_allowed(task)
            return True

        with patch.object(self.controller, "get_task", return_value=task), \
             patch.object(self.controller, "set_task_status", return_value=True), \
             patch.object(self.controller, "_get_store", return_value=None), \
             patch.object(self.controller, "attention_get", return_value={}), \
             patch.object(self.controller, "attention_note"), \
             patch.object(
                 self.controller,
                 "_record_completion_sample",
                 side_effect=accept_durable_completion,
             ), \
             patch.object(self.controller, "enqueue_coordinator_event") as enqueue:
            self.controller.handle_event("t-int-1", "done")
        return enqueue, harness

    def test_intervened_actions_never_emit_done(self):
        for action in sorted(INTERVENTION_ACTIONS):
            result = {
                "intercepted": True,
                "handled": True,
                "continue_flow": False,
                "decision": {"action": action, "reasons": ["test"]},
            }
            enqueue, _ = self._handle_done_event(result)
            self.assertFalse(
                enqueue.called,
                f"{action} intervention must not fall through to the done event",
            )

    def test_unmapped_intervention_never_emits_done(self):
        result = {"intercepted": True, "handled": False, "continue_flow": False,
                  "decision": {"action": "VERIFY", "reasons": ["needs verification"]}}
        enqueue, _ = self._handle_done_event(result)
        enqueue.assert_not_called()

    def test_continue_flow_still_emits_done(self):
        result = {"intercepted": False, "handled": False, "continue_flow": True,
                  "decision": {"action": "CONTINUE", "reasons": []}}
        enqueue, _ = self._handle_done_event(result)
        enqueue.assert_called_once()

    def test_missing_checkpoint_result_is_fail_safe_continue(self):
        enqueue, _ = self._handle_done_event(None)
        enqueue.assert_called_once()

    def test_pending_intervention_blocks_done_when_checkpoint_skipped(self):
        enqueue, _ = self._handle_done_event(None, pending="ESCALATE")
        enqueue.assert_not_called()

    def test_pending_intervention_allows_done_after_resolution(self):
        enqueue, _ = self._handle_done_event(None, pending=None)
        enqueue.assert_called_once()

    def test_supervisor_continue_flow_helper(self):
        self.assertTrue(self.controller.supervisor_continue_flow(None))
        self.assertTrue(self.controller.supervisor_continue_flow({"continue_flow": True}))
        self.assertFalse(self.controller.supervisor_continue_flow({"continue_flow": False}))

    def test_retry_handler_fails_loudly_when_rework_transition_fails(self):
        with patch.object(self.controller, "set_task_status", return_value=False):
            with self.assertRaises(RuntimeError):
                self.controller._supervisor_retry({"task_id": "t-x"}, {})

    def test_supervisor_attention_notifies_once_per_action(self):
        task = {"task_id": "t-att-1", "workflow_id": "wf-int"}
        episodes = {}

        def fake_get(key):
            return episodes.get(key)

        def fake_note(key, t, event_type, reason, **kwargs):
            episodes[key] = {"event_type": event_type, "reason": reason}
            return episodes[key]

        with patch.object(self.controller, "attention_get", side_effect=fake_get), \
             patch.object(self.controller, "attention_note", side_effect=fake_note), \
             patch.object(self.controller, "_supervisor_notify") as notify:
            self.controller._supervisor_attention(
                task, {"reasons": ["r1"]}, "supervisor_escalate")
            self.controller._supervisor_attention(
                task, {"reasons": ["r2"]}, "supervisor_escalate")
            self.controller._supervisor_attention(
                task, {"reasons": ["r3"]}, "supervisor_pause")
        self.assertEqual(notify.call_count, 2, "one notification per distinct action")

    def test_controller_checkpoint_returns_harness_result(self):
        result = {"intercepted": True, "handled": True, "continue_flow": False,
                  "decision": {"action": "RETRY", "reasons": ["stuck"]}}
        task = {"task_id": "t-int-2", "status": "agent_done", "pane_id": "w1:p1"}
        harness = _FakeHarness(result)
        self.controller.supervisor_harness = harness
        with patch.object(self.controller, "get_task", return_value=task), \
             patch.object(self.controller, "_get_store", return_value=None):
            returned = self.controller.supervisor_checkpoint(task, "agent_done")
        self.assertEqual(returned, result)
        self.assertEqual(len(harness.calls), 1)
        self.assertIn("report_reader", harness.calls[0])


class RegistryWatcherDoneGatingTests(unittest.TestCase):
    """redeliver_done_event is the production redelivery path; it is gated."""

    def setUp(self):
        self.controller = importlib.import_module("services.herdr-controller")
        self.addCleanup(setattr, self.controller, "supervisor_harness",
                        self.controller.supervisor_harness)
        self.addCleanup(setattr, self.controller, "queued_events",
                        self.controller.queued_events)
        self.controller.queued_events = set()
        os.environ["HERDR_CONTROLLER_TEST"] = "1"
        self.addCleanup(os.environ.pop, "HERDR_CONTROLLER_TEST", None)

    def _redeliver(self, harness_result, pending=None):
        task = {
            "task_id": "t-watch-1",
            "workflow_id": "wf-int",
            "stage": "implementation",
            "status": "agent_done",
            "pane_id": "w1:p1",
        }
        harness = _FakeHarness(harness_result, pending=pending)
        self.controller.supervisor_harness = harness
        with patch.object(self.controller, "get_task", return_value=task), \
             patch.object(self.controller, "_get_store", return_value=None), \
             patch.object(self.controller, "attention_blocks_retry", return_value=False), \
             patch.object(self.controller, "attention_get", return_value={}), \
             patch.object(self.controller, "attention_note"), \
             patch.object(self.controller, "attention_throttle"), \
             patch.object(self.controller, "enqueue_coordinator_event") as enqueue:
            delivered = self.controller.redeliver_done_event(task, now=100.0)
        return delivered, enqueue

    def test_intercepted_done_is_withheld_on_redelivery(self):
        result = {"intercepted": True, "handled": True, "continue_flow": False,
                  "decision": {"action": "PAUSE", "reasons": ["off track"]}}
        delivered, enqueue = self._redeliver(result)
        self.assertFalse(delivered)
        enqueue.assert_not_called()

    def test_unhandled_intervention_is_withheld_on_redelivery(self):
        result = {"intercepted": True, "handled": False, "continue_flow": False,
                  "decision": {"action": "VERIFY", "reasons": ["needs verification"]}}
        delivered, enqueue = self._redeliver(result)
        self.assertFalse(delivered)
        enqueue.assert_not_called()

    def test_pending_intervention_is_withheld_on_redelivery(self):
        delivered, enqueue = self._redeliver(None, pending="ESCALATE")
        self.assertFalse(delivered)
        enqueue.assert_not_called()

    def test_continue_flow_delivers_done(self):
        result = {"intercepted": False, "handled": False, "continue_flow": True,
                  "decision": {"action": "CONTINUE", "reasons": []}}
        delivered, enqueue = self._redeliver(result)
        self.assertTrue(delivered)
        enqueue.assert_called_once()


class PendingInterventionTests(unittest.TestCase):
    """Durable blocking across rate-gated redelivery/recovery paths."""

    def _escalated(self):
        signals = dict.fromkeys(ALL_SIGNALS, 0.05)
        signals["needs_human"] = 0.95
        provider = StubProvider(signals=signals)
        config = load_config(path="/nonexistent-supervisor.json")
        config["enforce"] = True
        config["provider"] = "rule"
        store = FakeStore()
        task = _task(attempt_count=2)
        supervisor = SemanticSupervisor(config, provider)
        result = run_checkpoint(
            task=task, trigger="agent_done", store=store, config=config,
            supervisor=supervisor, actions={}, log=lambda _m: None,
        )
        return task, store, config, supervisor, result

    def test_escalation_is_recorded_and_kept_pending(self):
        from herdr.supervisor.harness import pending_intervention

        task, store, config, supervisor, result = self._escalated()
        self.assertEqual(result["decision"]["action"], "ESCALATE")
        self.assertTrue(result["intercepted"])
        self.assertFalse(result["continue_flow"])
        second = run_checkpoint(
            task=task, trigger="agent_done", store=store, config=config,
            supervisor=supervisor, actions={}, log=lambda _m: None,
        )
        self.assertIsNone(second, "default RateGate skips the fresh evaluation")
        self.assertEqual(pending_intervention(task, store, config), "ESCALATE")

    def test_newer_agent_done_transition_supersedes_pending(self):
        from herdr.supervisor.harness import pending_intervention

        task, store, config, _, _ = self._escalated()
        newer = dict(task)
        newer["status_history"] = list(task.get("status_history") or []) + [
            {"from": "rework", "to": "agent_done", "timestamp": time.time() + 1},
        ]
        self.assertIsNone(pending_intervention(newer, store, config))

    def test_pending_survives_long_event_history(self):
        """list_events must fetch the NEWEST N; a buried intervention still blocks."""
        from herdr.state_store import SQLiteStateStore
        from herdr.supervisor.harness import POLICY_EVENT, pending_intervention

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteStateStore(
                db_path=Path(tmp) / "state.db", auto_migrate_json=False)
            task_id = "t-long-events"
            for index in range(150):
                store.record_event(
                    "task_transition", {"i": index},
                    task_id=task_id, timestamp=1000.0 + index)
            config = load_config(path="/nonexistent-supervisor.json")
            config["enforce"] = True
            config["provider"] = "rule"
            store.record_event(
                POLICY_EVENT, {"action": "PAUSE", "enforced": True},
                task_id=task_id, timestamp=2000.0)
            newest = store.list_events(task_id=task_id, limit=3, desc=True)
            self.assertEqual(newest[0]["payload"]["action"], "PAUSE")
            self.assertEqual(
                pending_intervention(_task(task_id=task_id), store, config),
                "PAUSE")

    def test_pending_is_none_when_supervision_or_enforcement_off(self):
        from herdr.supervisor.harness import pending_intervention

        task, store, config, _, _ = self._escalated()
        config["enforce"] = False
        self.assertIsNone(pending_intervention(task, store, config))
        config["enforce"] = True
        config["enabled"] = False
        self.assertIsNone(pending_intervention(task, store, config))

    def test_observe_mode_intervention_is_not_pending(self):
        from herdr.supervisor.harness import pending_intervention

        signals = dict.fromkeys(ALL_SIGNALS, 0.05)
        signals["needs_human"] = 0.95
        provider = StubProvider(signals=signals)
        config = load_config(path="/nonexistent-supervisor.json")
        config["provider"] = "rule"
        store = FakeStore()
        task = _task(attempt_count=2)
        result = run_checkpoint(
            task=task, trigger="agent_done", store=store, config=config,
            supervisor=SemanticSupervisor(config, provider), actions={},
            log=lambda _m: None,
        )
        self.assertEqual(result["decision"]["action"], "ESCALATE")
        self.assertTrue(result["continue_flow"], "observe mode never blocks")
        self.assertIsNone(pending_intervention(task, store, config))

    def test_pending_is_none_without_jev_provider_key(self):
        import os

        from herdr.supervisor.harness import pending_intervention

        task, store, config, _, _ = self._escalated()
        config["provider"] = "jev"
        saved = {k: os.environ.pop(k, None) for k in ("JEV_API_KEY", "TYPESAFE_API_KEY")}
        self.addCleanup(lambda: [os.environ.update({k: v}) for k, v in saved.items() if v])
        self.assertIsNone(pending_intervention(task, store, config),
                          "no provider -> fail-safe: original behavior restored")


if __name__ == "__main__":
    unittest.main()
