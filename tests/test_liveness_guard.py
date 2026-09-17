#!/opt/homebrew/bin/python3
"""Tests for the control-plane Liveness Guard (SLA / backoff / hygiene / stalls).

Covers the systemic anti-stall contract:
- bounded actor waits (coordinator delivery / stage advance);
- exponential backoff with cap (listener subscriptions, delivery retries);
- fixture/temp workflow isolation from the runtime sweep;
- attention episode bookkeeping (record once, throttle retries, clear on recovery);
- sentinel stall detection for tasks that stop progressing.
"""

import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import liveness  # noqa: E402


class BackoffPolicyTest(unittest.TestCase):
    def test_exponential_backoff_doubles_and_caps(self):
        self.assertEqual(liveness.backoff_delay(1, base=2, cap=300), 2)
        self.assertEqual(liveness.backoff_delay(2, base=2, cap=300), 4)
        self.assertEqual(liveness.backoff_delay(3, base=2, cap=300), 8)
        self.assertEqual(liveness.backoff_delay(99, base=2, cap=300), 300)

    def test_backoff_attempt_zero_treated_as_first(self):
        self.assertEqual(liveness.backoff_delay(0, base=5, cap=60), 5)


class RegistryHygieneTest(unittest.TestCase):
    def test_foreign_workflow_file_detection(self):
        self.assertTrue(liveness.is_foreign_workflow_file(None))
        self.assertTrue(liveness.is_foreign_workflow_file(""))
        self.assertTrue(
            liveness.is_foreign_workflow_file(
                "/private/var/folders/hb/pytest-of-user/pytest-191/test_x/dummy_workflow.json"
            )
        )
        self.assertTrue(
            liveness.is_foreign_workflow_file("/tmp/e2e-root/checkpoints/dummy_workflow.json")
        )
        self.assertTrue(liveness.is_foreign_workflow_file("/nonexistent/live/workflow.json"))

    def test_real_runtime_workflow_is_not_foreign(self):
        with tempfile.TemporaryDirectory(prefix="herdr-live-test-") as tmp:
            path = Path(tmp) / "workflow.json"
            path.write_text("{}", encoding="utf-8")
            self.assertFalse(liveness.is_foreign_workflow_file(str(path)))

    def test_workflow_is_foreign_uses_workflow_file_field(self):
        # 无记录 / 有项目上下文的遗留记录:不判外来(避免误杀)。
        self.assertFalse(liveness.workflow_is_foreign({}))
        self.assertFalse(liveness.workflow_is_foreign({"project_id": "p1"}))
        # 无定义文件且无项目上下文的空壳 = 夹具残留。
        self.assertTrue(liveness.workflow_is_foreign({"status": "running"}))
        self.assertTrue(
            liveness.workflow_is_foreign(
                {"workflow_file": "/private/var/folders/x/pytest-1/wf.json"}
            )
        )


class IntegrationHealthTest(unittest.TestCase):
    SAMPLE = (
        "pi: current (v8) (/Users/user/.pi/agent/extensions/herdr-agent-state.ts)\n"
        "claude: current (v9) (/Users/user/.claude/hooks/herdr-agent-state.sh)\n"
        "opencode: not installed (/Users/user/.config/opencode/plugins/herdr-agent-state.js)\n"
        "antigravity-cli: current (v3) (/Users/user/.gemini/config/hooks/herdr-agent-state.sh)\n"
        "qodercli: current (v3) (/Users/user/.qoder/hooks/herdr-agent-state.sh)\n"
    )

    def test_parse_integration_status(self):
        statuses = liveness.parse_integration_status(self.SAMPLE)
        self.assertIn("opencode", statuses)
        self.assertIn("not installed", statuses["opencode"])

    def test_integration_gaps_report_only_used_kinds(self):
        gaps = liveness.integration_gaps(self.SAMPLE, ["codex", "opencode", "claude"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["integration"], "opencode")

    def test_integration_gaps_resolve_agy_alias(self):
        gaps = liveness.integration_gaps(self.SAMPLE, ["agy"])
        self.assertEqual(gaps, [])

        missing = liveness.integration_gaps(
            "antigravity-cli: not installed (/x)\n", ["agy"]
        )
        self.assertEqual(missing[0]["integration"], "antigravity-cli")


class BoundedWaitTest(unittest.TestCase):
    def test_bounded_wait_expires_after_sla(self):
        clock = {"now": 100.0}
        wait = liveness.BoundedWait(sla=900, clock=lambda: clock["now"])
        self.assertFalse(wait.expired())
        clock["now"] = 999.0
        self.assertFalse(wait.expired())
        clock["now"] = 1000.0
        self.assertTrue(wait.expired())
        self.assertEqual(wait.elapsed(), 900.0)


class EpisodeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-liveness-test-")
        self.path = Path(self.tmp.name) / "attention.json"
        self.store = liveness.EpisodeStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_get_clear_roundtrip(self):
        self.assertIsNone(self.store.get("k1"))
        self.store.upsert("k1", {"reason": "coordinator_stalled", "attempts": 1})
        episode = self.store.get("k1")
        self.assertEqual(episode["reason"], "coordinator_stalled")

        self.store.upsert("k1", {"attempts": 2})
        self.assertEqual(self.store.get("k1")["attempts"], 2)
        self.assertEqual(self.store.get("k1")["reason"], "coordinator_stalled")

        self.assertTrue(self.store.clear("k1"))
        self.assertIsNone(self.store.get("k1"))
        self.assertFalse(self.store.clear("k1"))

    def test_blocks_retry_and_throttle(self):
        self.assertFalse(liveness.blocks_retry(self.store, "k1"))

        self.store.upsert("k1", {"next_retry_at": time.time() + 600})
        self.assertTrue(liveness.blocks_retry(self.store, "k1"))

        self.store.upsert("k1", {"next_retry_at": time.time() - 1})
        self.assertFalse(liveness.blocks_retry(self.store, "k1"))

        liveness.throttle_retry(self.store, "k1", interval=50)
        self.assertTrue(liveness.blocks_retry(self.store, "k1"))

    def test_store_persists_across_instances(self):
        self.store.upsert("k1", {"reason": "x"})
        reloaded = liveness.EpisodeStore(self.path)
        self.assertEqual(reloaded.get("k1")["reason"], "x")


class TaskStallEvaluationTest(unittest.TestCase):
    def _task(self, task_id, status, updated_at, workflow_id="wf-1"):
        return {
            "task_id": task_id,
            "status": status,
            "updated_at": updated_at,
            "workflow_id": workflow_id,
        }

    def test_stall_alert_raised_once_per_episode(self):
        now = 10_000.0
        tasks = [self._task("t1", "interrupted", now - 3600)]

        alerts, episodes = liveness.evaluate_task_stalls(tasks, {}, now, stall_after=1800)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["task_id"], "t1")
        self.assertEqual(alerts[0]["status"], "interrupted")

        alerts_again, episodes = liveness.evaluate_task_stalls(tasks, episodes, now + 5, stall_after=1800)
        self.assertEqual(alerts_again, [])

    def test_state_transition_resets_episode(self):
        now = 10_000.0
        tasks = [self._task("t1", "working", now - 3600)]
        _, episodes = liveness.evaluate_task_stalls(tasks, {}, now, stall_after=1800)

        tasks = [self._task("t1", "working", now - 10)]
        alerts, episodes = liveness.evaluate_task_stalls(tasks, episodes, now, stall_after=1800)
        self.assertEqual(alerts, [])
        self.assertNotIn("t1", episodes)

        tasks = [self._task("t1", "working", now - 2000)]
        alerts, episodes = liveness.evaluate_task_stalls(tasks, episodes, now, stall_after=1800)
        self.assertEqual(len(alerts), 1)

    def test_terminal_and_fresh_tasks_never_alert(self):
        now = 10_000.0
        tasks = [
            self._task("done-task", "cleaned", now - 99999),
            self._task("fresh-task", "working", now - 5),
        ]
        alerts, episodes = liveness.evaluate_task_stalls(tasks, {}, now, stall_after=1800)
        self.assertEqual(alerts, [])
        self.assertEqual(episodes, {})

    def test_missing_tasks_pruned_from_episodes(self):
        now = 10_000.0
        episodes = {"ghost": {"task_id": "ghost", "updated_at": now - 9999}}
        alerts, episodes = liveness.evaluate_task_stalls([], episodes, now, stall_after=1800)
        self.assertEqual(alerts, [])
        self.assertEqual(episodes, {})


class ControllerLivenessWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-controller-liveness-")
        self.store = liveness.EpisodeStore(Path(self.tmp.name) / "attention.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_attention_event_message_contract(self):
        controller = importlib.import_module("services.herdr-controller")
        task = {
            "task_id": "t-att",
            "workflow_id": "wf-att",
            "stage": "implementation",
            "pane_id": "w1:p2",
            "agent": "codex",
            "goal": "goal",
            "acceptance_criteria": ["a"],
            "status": "interrupted",
        }
        message = controller.build_coordinator_message(task, "attention")
        self.assertIn("HERDR_CONTROLLER_ATTENTION_EVENT", message)
        self.assertIn("interrupted", message)
        self.assertIn("t-att", message)

    def test_efficiency_discipline_in_coordinator_messages(self):
        # 2026-09-17 复盘:总指挥决策后空转/越权重活是长尾主因,
        # 所有事件模板必须携带效率纪律(落盘即停 / 禁止重操作 / /compact)。
        controller = importlib.import_module("services.herdr-controller")
        task = {
            "task_id": "t-eff",
            "workflow_id": "wf-eff",
            "stage": "implementation",
            "pane_id": "w1:p2",
            "agent": "opencode",
            "goal": "goal",
            "acceptance_criteria": ["a"],
            "status": "agent_done",
        }
        for event_type in ("done", "blocked", "attention"):
            message = controller.build_coordinator_message(task, event_type)
            self.assertIn("效率纪律", message)
            self.assertIn("立即结束本回合", message)
            self.assertIn("/compact", message)
            self.assertIn("禁止运行重操作", message)

    def test_coordinator_stall_records_attention_and_notifies(self):
        controller = importlib.import_module("services.herdr-controller")
        item = {"task_id": "t1", "event_type": "done", "key": "t1:done"}
        task = {"task_id": "t1", "workflow_id": "wf-1", "pane_id": "w1:p1"}

        with patch.object(controller, "_attention_store", self.store), \
             patch.object(controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
             patch.object(controller, "notify_attention") as notify:
            controller.handle_coordinator_delivery_stall(item, task, 901)

        episode = self.store.get("t1:done")
        self.assertIsNotNone(episode)
        self.assertEqual(episode["reason"], "coordinator_stalled")
        self.assertEqual(episode["attempts"], 1)
        self.assertGreater(episode["next_retry_at"], time.time())
        notify.assert_called_once()

    def test_foreign_workflow_skipped_by_sweep(self):
        controller = importlib.import_module("services.herdr-controller")

        with tempfile.TemporaryDirectory(prefix="herdr-live-wf-") as live_dir:
            live_file = Path(live_dir) / "workflow.json"
            live_file.write_text("{}", encoding="utf-8")

            class FakeStore:
                def list_workflows(self):
                    return [
                        {
                            "workflow_id": "wf-live",
                            "status": "running",
                            "workflow_file": str(live_file),
                        },
                        {
                            "workflow_id": "wf-fixture",
                            "status": "running",
                            "workflow_file": "/private/var/folders/x/pytest-of-user/pytest-1/wf.json",
                        },
                        {
                            "workflow_id": "wf-missing",
                            "status": "running",
                            "workflow_file": None,
                        },
                    ]

            with patch.object(controller, "_get_store", return_value=FakeStore()), \
                 patch.object(controller, "_foreign_workflows_logged", set()):
                active = controller.active_registered_workflows()

        self.assertEqual(active, {"wf-live"})

    def test_foreign_workflow_never_close_dispatched(self):
        controller = importlib.import_module("services.herdr-controller")

        class FakeStore:
            def get_workflow(self, workflow_id):
                return {
                    "workflow_id": workflow_id,
                    "status": "running",
                    "workflow_file": "/private/var/folders/x/pytest-of-user/pytest-1/wf.json",
                }

        with patch.object(controller, "_get_store", return_value=FakeStore()), \
             patch.object(controller, "_workflow_close_inflight", set()), \
             patch.object(controller.subprocess, "run") as run_mock:
            controller.maybe_close_completed_workflow("wf-fixture")
            time.sleep(0.2)
            run_mock.assert_not_called()


class CoordinatorCompactTest(unittest.TestCase):
    """阶段/fix-loop 边界的总指挥 /compact 注入(2026-09-17 效率优化)。

    回归背景:wf-nexusarchive-0917-01 总指挥上下文 94K->684K,后期单回合
    纯 LLM 生成 27-58min;阶段边界压缩上下文把后续回合耗时拉回分钟级。
    """

    def setUp(self):
        self.controller = importlib.import_module("services.herdr-controller")

    def _resp(self, returncode=0, stdout="", stderr=""):
        import subprocess
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_disabled_by_env_never_calls_herdr(self):
        with patch.dict("os.environ", {"HERDR_COORDINATOR_COMPACT": "0"}), \
             patch.object(self.controller.subprocess, "run") as run_mock, \
             patch.object(self.controller, "coordinator_pane_for_workflow",
                          return_value="w1:p1"):
            self.assertFalse(
                self.controller.maybe_compact_coordinator("wf-1")
            )
            run_mock.assert_not_called()

    def test_skips_unsupported_agent_kind(self):
        with patch.object(self.controller, "coordinator_pane_for_workflow",
                          return_value="w1:p1"), \
             patch.object(self.controller, "coordinator_agent_kind",
                          return_value="codex"), \
             patch.object(self.controller.subprocess, "run") as run_mock:
            self.assertFalse(
                self.controller.maybe_compact_coordinator("wf-1")
            )
            run_mock.assert_not_called()

    def test_skips_when_coordinator_busy(self):
        with patch.object(self.controller, "coordinator_pane_for_workflow",
                          return_value="w1:p1"), \
             patch.object(self.controller, "coordinator_agent_kind",
                          return_value="opencode"), \
             patch.object(self.controller, "coordinator_status",
                          return_value="working"), \
             patch.object(self.controller.subprocess, "run") as run_mock:
            self.assertFalse(
                self.controller.maybe_compact_coordinator("wf-1")
            )
            run_mock.assert_not_called()

    def test_sends_compact_when_idle_and_supported(self):
        with patch.object(self.controller, "coordinator_pane_for_workflow",
                          return_value="w1:p1"), \
             patch.object(self.controller, "coordinator_agent_kind",
                          return_value="opencode"), \
             patch.object(self.controller, "coordinator_status",
                          return_value="idle"), \
             patch.object(
                 self.controller.subprocess, "run",
                 side_effect=lambda *a, **k: self._resp(0),
             ) as run_mock:
            self.assertTrue(
                self.controller.maybe_compact_coordinator(
                    "wf-1", reason="stage_advance:plan"
                )
            )

        cmd = run_mock.call_args.args[0]
        self.assertEqual(
            cmd[:5],
            ["herdr", "agent", "prompt", "w1:p1", "/compact"],
        )
        self.assertIn("--wait", cmd)

    def test_compaction_failure_is_non_blocking(self):
        with patch.object(self.controller, "coordinator_pane_for_workflow",
                          return_value="w1:p1"), \
             patch.object(self.controller, "coordinator_agent_kind",
                          return_value="claude"), \
             patch.object(self.controller, "coordinator_status",
                          return_value="done"), \
             patch.object(
                 self.controller.subprocess, "run",
                 side_effect=lambda *a, **k: self._resp(1, stderr="boom"),
             ):
            self.assertFalse(
                self.controller.maybe_compact_coordinator("wf-1")
            )


class SentinelStallGuardTest(unittest.TestCase):
    def test_check_task_stalls_alerts_and_dedupes(self):
        sentinel = importlib.import_module("services.herdr-sentinel")

        now = time.time()
        tasks = [
            {
                "task_id": "stalled-1",
                "status": "interrupted",
                "updated_at": now - 3600,
                "workflow_id": "wf-1",
            }
        ]
        state = {}

        with patch.object(sentinel.liveness, "task_stall_after", return_value=1800), \
             patch.object(sentinel, "notify_stall") as notify:
            sentinel.check_task_stalls(tasks, state)
            self.assertEqual(notify.call_count, 1)

            sentinel.check_task_stalls(tasks, state)
            self.assertEqual(notify.call_count, 1)

        self.assertIn("stalled-1", state["stalls"])

    def test_stall_episode_cleared_when_task_recovers(self):
        sentinel = importlib.import_module("services.herdr-sentinel")

        now = time.time()
        stalled = [
            {
                "task_id": "stalled-1",
                "status": "working",
                "updated_at": now - 3600,
                "workflow_id": "wf-1",
            }
        ]
        state = {}

        with patch.object(sentinel.liveness, "task_stall_after", return_value=1800), \
             patch.object(sentinel, "notify_stall"):
            sentinel.check_task_stalls(stalled, state)
            self.assertIn("stalled-1", state["stalls"])

            recovered = [
                {
                    "task_id": "stalled-1",
                    "status": "cleaned",
                    "updated_at": now - 1,
                    "workflow_id": "wf-1",
                }
            ]
            sentinel.check_task_stalls(recovered, state)

        self.assertNotIn("stalled-1", state["stalls"])


if __name__ == "__main__":
    unittest.main()
