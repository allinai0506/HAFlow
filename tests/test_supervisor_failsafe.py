"""End-to-end fail-safe guarantees for the Semantic Supervisor:

- no JEV_API_KEY        -> HAFlow runs normally, zero supervision traffic
- Jev timeout/crash     -> task flow untouched, failed evaluation recorded
- supervisor disabled   -> checkpoints are complete no-ops
- enforcement off (V1)  -> policy actions are recorded, never executed
- enforcement on        -> actions run ONLY through orchestrator-provided
                           existing-flow handlers (retry==rework etc.)
"""

import os
import unittest

from herdr.decision.models import DecisionProviderError
from herdr.supervisor import harness
from herdr.supervisor.config import load_config, supervisor_enabled
from herdr.supervisor.engine import SemanticSupervisor

from tests.test_semantic_supervisor import (  # reuse doubles
    ALL_SIGNALS,
    FakeStore,
    StubProvider,
    _task,
)


def _clean_jev_env(test):
    saved = {}
    for key in ("JEV_API_KEY", "TYPESAFE_API_KEY"):
        saved[key] = os.environ.pop(key, None)
    test.addCleanup(lambda: [os.environ.update({k: v}) for k, v in saved.items() if v is not None])


class NoApiKeyTests(unittest.TestCase):
    def test_no_key_disables_supervision_and_changes_nothing(self):
        _clean_jev_env(self)
        config = load_config(path="/nonexistent-supervisor.json")
        self.assertFalse(supervisor_enabled(config))
        store = FakeStore()
        result = harness.run_checkpoint(
            task=_task(), trigger="agent_done", store=store,
            config=config, log=lambda _m: None)
        self.assertIsNone(result)
        self.assertEqual(store.events, [], "no provider, no events, flow intact")


class DisabledTests(unittest.TestCase):
    def test_disabled_is_total_noop(self):
        config = load_config(path="/nonexistent-supervisor.json")
        config["enabled"] = False
        provider = StubProvider()
        store = FakeStore()
        result = harness.run_checkpoint(
            task=_task(), trigger="agent_done", store=store, config=config,
            supervisor=SemanticSupervisor(config, provider), log=lambda _m: None)
        self.assertIsNone(result)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(store.events, [])


class JevDownTests(unittest.TestCase):
    def test_provider_outage_records_failed_evaluation_task_unaffected(self):
        config = load_config(path="/nonexistent-supervisor.json")
        config["interval"] = 0
        provider = StubProvider(raise_error=DecisionProviderError("jev down", kind="timeout"))
        store = FakeStore()
        task = _task()
        result = harness.run_checkpoint(
            task=task, trigger="agent_done", store=store, config=config,
            supervisor=SemanticSupervisor(config, provider), log=lambda _m: None)
        self.assertIsNotNone(result)
        self.assertEqual(result["evaluation"]["status"], "failed")
        self.assertEqual(result["decision"]["action"], "CONTINUE")
        # The task record itself was never touched by supervision.
        self.assertEqual(task["status"], "agent_done")

    def test_broken_store_never_raises_into_task_flow(self):
        class ExplodingStore(FakeStore):
            def list_events(self, **kwargs):
                raise RuntimeError("db locked")

            def record_event(self, *a, **k):
                raise RuntimeError("db locked")

        config = load_config(path="/nonexistent-supervisor.json")
        provider = StubProvider()
        result = harness.run_checkpoint(
            task=_task(), trigger="agent_done", store=ExplodingStore(),
            config=config, supervisor=SemanticSupervisor(config, provider),
            log=lambda _m: None)
        self.assertIsNotNone(result, "evaluation still produced; store errors swallowed")


class EnforcementTests(unittest.TestCase):
    def _stuck_config(self, enforce):
        config = load_config(path="/nonexistent-supervisor.json")
        config["enforce"] = enforce
        config["interval"] = 0
        return config

    def _run(self, enforce, actions=None):
        provider = StubProvider(signals=dict(ALL_SIGNALS, worker_stuck=0.95,
                                             meaningful_progress=0.03))
        store = FakeStore()
        executed = []

        def retry_handler(task, decision):
            executed.append((task["task_id"], decision["action"]))

        result = harness.run_checkpoint(
            task=_task(), trigger="agent_done", store=store,
            config=self._stuck_config(enforce),
            supervisor=SemanticSupervisor(self._stuck_config(enforce), provider),
            actions=actions if actions is not None else {"RETRY": retry_handler},
            log=lambda _m: None)
        return result, executed

    def test_observe_mode_records_but_does_not_execute(self):
        result, executed = self._run(enforce=False)
        self.assertEqual(result["decision"]["action"], "RETRY")
        self.assertEqual(executed, [])

    def test_enforce_mode_executes_via_existing_flow_handler(self):
        result, executed = self._run(enforce=True)
        self.assertEqual(result["decision"]["action"], "RETRY")
        self.assertEqual(executed, [("t-1", "RETRY")],
                         "action runs only through the orchestrator's own handler")

    def test_handler_crash_is_contained(self):
        def bad_handler(task, decision):
            raise RuntimeError("rework unavailable")
        result, _ = self._run(enforce=True, actions={"RETRY": bad_handler})
        self.assertIsNotNone(result)


class ControllerWiringTests(unittest.TestCase):
    def test_controller_supervisor_checkpoint_survives_dead_harness(self):
        """Even if the whole supervisor package were broken, the controller's
        checkpoint helper must degrade to a log line."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "herdr_controller_under_test", "services/herdr-controller.py")
        controller = importlib.util.module_from_spec(spec)
        os.environ["HERDR_CONTROLLER_TEST"] = "1"
        self.addCleanup(os.environ.pop, "HERDR_CONTROLLER_TEST", None)
        spec.loader.exec_module(controller)
        controller.supervisor_harness = None
        controller.supervisor_checkpoint({"task_id": "t-x"}, "agent_done")  # no raise
        self.assertTrue(callable(controller.supervisor_checkpoint))


if __name__ == "__main__":
    unittest.main()
