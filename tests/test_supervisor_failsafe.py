"""End-to-end fail-safe guarantees for the Semantic Supervisor:

- no JEV_API_KEY        -> HAFlow runs normally, zero supervision traffic
- Jev timeout/crash     -> task flow untouched, failed evaluation recorded
- supervisor disabled   -> checkpoints are complete no-ops
- enforcement off (V1)  -> policy actions are recorded, never executed
- enforcement on        -> actions run ONLY through orchestrator-provided
                           existing-flow handlers (retry==rework etc.)
"""

import json
import os
import unittest
from unittest.mock import patch

from herdr.decision.models import DecisionProviderError
from herdr.decision.providers.jev import JevDecisionProvider
from herdr.supervisor import harness
from herdr.supervisor.config import (
    jev_provider_config,
    load_config,
    provider_enabled,
    supervisor_enabled,
)
from herdr.supervisor.engine import SemanticSupervisor
from herdr.supervisor.signals import SIGNAL_NAMES

from tests.test_decision_providers import _jev_transport
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


class JevKillSwitchTests(unittest.TestCase):
    """HERDR_SUPERVISOR_JEV_ENABLED=false and live key revocation must stop
    Jev traffic immediately, even in an already-running controller process."""

    def setUp(self):
        _clean_jev_env(self)
        os.environ.pop("HERDR_SUPERVISOR_JEV_ENABLED", None)
        self.addCleanup(os.environ.pop, "HERDR_SUPERVISOR_JEV_ENABLED", None)

    def _spy(self):
        calls = []

        def post(url, headers, payload, timeout):
            calls.append(payload)
            return 200, json.dumps({
                "answers": {name: {"type": "noul", "noul": 0.2}
                            for name in SIGNAL_NAMES},
            })

        return post, calls

    def test_jev_disabled_flag_blocks_all_requests(self):
        os.environ["JEV_API_KEY"] = "test-key"
        config = load_config(
            path="/nonexistent-supervisor.json",
            env={"HERDR_SUPERVISOR_JEV_ENABLED": "false"},
        )
        self.assertFalse(provider_enabled(config))
        self.assertFalse(supervisor_enabled(config))
        self.assertIsNone(harness.get_supervisor(config))

        post, calls = self._spy()
        with patch("herdr.decision.providers.jev._http_post_json",
                   side_effect=post) as transport:
            result = harness.run_checkpoint(
                task=_task(), trigger="agent_done", store=FakeStore(),
                config=config, log=lambda _m: None)
        self.assertIsNone(result)
        transport.assert_not_called()
        self.assertEqual(calls, [])

    def test_disabled_provider_rejects_requests_even_with_key(self):
        os.environ["JEV_API_KEY"] = "test-key"
        post, calls = self._spy()
        provider = JevDecisionProvider({"enabled": False}, transport=post)
        self.assertFalse(provider.available())
        with self.assertRaises(DecisionProviderError) as ctx:
            provider.judge("question", "state")
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(calls, [])

    def test_engine_reports_provider_disabled(self):
        config = load_config(
            path="/nonexistent-supervisor.json",
            env={"HERDR_SUPERVISOR_JEV_ENABLED": "false"},
        )
        post, calls = self._spy()
        provider = JevDecisionProvider({}, transport=post)
        supervisor = SemanticSupervisor(config, provider)
        self.assertEqual(
            supervisor.should_evaluate("t-1", "agent_done"), "provider_disabled")

    def test_running_controller_stops_requests_after_key_removal(self):
        os.environ["JEV_API_KEY"] = "test-key"
        post, calls = self._spy()
        provider = JevDecisionProvider({}, transport=post)
        self.assertTrue(provider.available(), "provider cached while key exists")

        config = load_config(path="/nonexistent-supervisor.json")
        config["interval"] = 0
        config["cooldown"] = 0
        supervisor = SemanticSupervisor(config, provider)

        first = harness.run_checkpoint(
            task=_task(), trigger="agent_done", store=FakeStore(),
            config=config, supervisor=supervisor, log=lambda _m: None)
        self.assertIsNotNone(first)
        self.assertEqual(len(calls), 1)

        os.environ.pop("JEV_API_KEY", None)
        self.assertFalse(provider.available(),
                         "revoked key must be observed immediately")

        second = harness.run_checkpoint(
            task=_task(), trigger="agent_done", store=FakeStore(),
            config=config, supervisor=supervisor, log=lambda _m: None)
        self.assertIsNone(second)
        self.assertEqual(len(calls), 1,
                         "no Jev request may happen after key removal")

    def test_api_key_never_appears_in_config_or_events(self):
        os.environ["JEV_API_KEY"] = "super-secret-key-value"
        post, calls = self._spy()
        provider = JevDecisionProvider({}, transport=post)
        config = load_config(path="/nonexistent-supervisor.json")
        config["interval"] = 0
        config["cooldown"] = 0
        supervisor = SemanticSupervisor(config, provider)
        store = FakeStore()
        result = harness.run_checkpoint(
            task=_task(), trigger="agent_done", store=store,
            config=config, supervisor=supervisor, log=lambda _m: None)
        self.assertIsNotNone(result)
        blob = json.dumps({"config": config, "events": store.events})
        self.assertNotIn("super-secret-key-value", blob)
        self.assertNotIn("super-secret-key-value", json.dumps(result))


class JevConfigTests(unittest.TestCase):
    def test_shorthand_jev_false_disables_provider(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "supervisor.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"supervisor": {"jev": False}}, handle)
            config = load_config(path=path, env={})
        self.assertEqual(config["jev"], {"enabled": False})
        self.assertFalse(provider_enabled(config))
        self.assertFalse(supervisor_enabled(config))

    def test_api_key_env_passthrough_to_provider(self):
        config = load_config(path="/nonexistent-supervisor.json", env={})
        config["jev"]["api_key_env"] = "MY_JEV_KEY"
        self.assertEqual(
            jev_provider_config(config)["api_key_env"], "MY_JEV_KEY")

    def test_supervisor_is_rebuilt_when_provider_settings_change(self):
        base = load_config(path="/nonexistent-supervisor.json", env={})
        updated = load_config(path="/nonexistent-supervisor.json", env={})
        updated["jev"]["model"] = "jev-other"
        harness.reset_process_state()
        self.addCleanup(harness.reset_process_state)
        first = harness.get_supervisor(base)
        self.assertIsNotNone(first)
        self.assertIs(first, harness.get_supervisor(base),
                      "identical config stays memoized")
        self.assertIsNot(
            first, harness.get_supervisor(updated),
            "provider setting changes must rebuild the supervisor")


if __name__ == "__main__":
    unittest.main()
