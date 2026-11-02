"""Tests for agent quota exhaustion detection, automatic handover and verifier budget safety."""
import importlib
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from herdr.state_store import SQLiteStateStore
from herdr.deep_preflight import classify_text, strip_known_status_chrome
from herdr.node_capacity import node_usage, launch_capacity_error


class TestAgentQuotaHandover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db_path = str(self.tmp_path / "herdr.db")
        self.store = SQLiteStateStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_mid_execution_token_exhausted_sentinel_recovery_and_handover(self):
        """Case 1: Mid-execution quota exhaustion triggers Sentinel failure & router handover."""
        sentinel_mod = importlib.import_module("services.herdr-sentinel")
        observe_token_exhausted_pattern = sentinel_mod.observe_token_exhausted_pattern
        from services.controller.infra_recovery import recover_infra_failed_tasks
        from herdr.agent_router import choose_agent

        workflow_id = "wf-mid-quota-01"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "project_id": "proj-1",
            "status": "running",
            "healthy_agents": ["opencode", "codex"],
            "unhealthy_agents": {},
        })
        self.store.save_task({
            "task_id": "wf-mid-quota-01-impl-auto",
            "workflow_id": workflow_id,
            "node": "impl",
            "stage": "impl",
            "agent": "opencode",
            "status": "working",
            "pane_id": "p-100",
            "version": 1,
            "dispatch_round": 1,
        })

        screen_text = (
            "Resolving dependencies...\n"
            "You've used today's free messages. They reset at 8:58 AM.\n"
            "Upgrade to OpenCode Go → /connect · Switch model → /models\n"
        )
        # 1. 验证屏幕文本被 classify_text 准确识别为 TOKEN_EXHAUSTED
        cleaned = strip_known_status_chrome("opencode", screen_text)
        self.assertEqual(classify_text(cleaned), "TOKEN_EXHAUSTED")

        # 2. Sentinel 探测并应用 observe_token_exhausted_pattern
        task = self.store.get_task("wf-mid-quota-01-impl-auto")
        changes = {}
        expected = {}
        versions = {}

        # 拦截 _get_store
        orig_get_store = sentinel_mod._get_store
        sentinel_mod._get_store = lambda: self.store
        try:
            observed = observe_token_exhausted_pattern(
                task, changes=changes, expected=expected, versions=versions
            )
            self.assertTrue(observed)
            self.assertEqual(changes["wf-mid-quota-01-impl-auto"], ("failed", "token_exhausted"))
            self.assertEqual(expected["wf-mid-quota-01-impl-auto"], "working")

            # 模拟 Sentinel 提交 CAS 更新
            self.store.compare_and_set_task_transition(
                task_id="wf-mid-quota-01-impl-auto",
                to_status="failed",
                reason="token_exhausted",
                source="herdr-sentinel",
                expected_status="working",
                expected_version=1,
            )
        finally:
            sentinel_mod._get_store = orig_get_store

        # 验证 workflow 记录了 opencode 不健康
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"].get("opencode"), "TOKEN_EXHAUSTED")
        self.assertNotIn("opencode", wf["healthy_agents"])

        # 3. 验证 recover_infra_failed_tasks 能够选中该任务并自动 supersede
        controller_ports = SimpleNamespace(
            TASK_MANAGER="bin/herdr-task",
            AUTO_RECOVER_REASONS={"dispatch_delivery_fuse", "agent_process_crash", "hard_timeout_watchdog", "token_exhausted"},
            AUTO_RECOVER_MAX_ATTEMPTS=2,
            _get_store=lambda: self.store,
            load_tasks=self.store.list_tasks,
            get_task=self.store.get_task,
            workflow_closed=lambda _: False,
            _workflow_entry=lambda _: {"status": "running"},
            clear_stage_advance=lambda *a, **k: None,
            _attention_store=SimpleNamespace(
                transaction=lambda: unittest.mock.MagicMock(__enter__=lambda s: {}, __exit__=lambda *a: None)
            ),
            attention_note=lambda *a, **k: None,
            notify_attention=lambda *a, **k: True,
            time=SimpleNamespace(time=lambda: 1000.0),
            liveness=__import__("herdr.liveness", fromlist=["select_infra_failures_for_recovery"]),
            subprocess=SimpleNamespace(
                run=lambda cmd, **kw: (
                    self.store.compare_and_set_task_transition(
                        task_id="wf-mid-quota-01-impl-auto",
                        to_status="superseded",
                        reason="auto-recover: infrastructure failure",
                        source="test",
                        expected_status=self.store.get_task("wf-mid-quota-01-impl-auto")["status"],
                        expected_version=self.store.get_task("wf-mid-quota-01-impl-auto")["version"],
                    ),
                    SimpleNamespace(returncode=0, stdout="", stderr="")
                )[1]
            ),
        )

        recovered = recover_infra_failed_tasks(
            controller_ports, workflow_id, tasks=self.store.list_tasks()
        )
        self.assertTrue(recovered)
        superseded_task = self.store.get_task("wf-mid-quota-01-impl-auto")
        self.assertEqual(superseded_task["status"], "superseded")

        # 4. 验证直接补派路由排除了 opencode 并交接给 codex
        import herdr.agent_router as router
        orig_wf_config = router.workflow_config_for
        orig_get_store_router = router._get_store
        router.workflow_config_for = lambda wid: {
            "workflow_id": wid,
            "project_id": "proj-1",
            "nodes": [{"id": "impl", "agent_policy": {"pool": ["opencode", "codex"]}}],
        }
        router._get_store = lambda: self.store
        try:
            chosen = choose_agent(workflow_id, "impl", "code", "auto")
            self.assertEqual(chosen, "codex")
        finally:
            router.workflow_config_for = orig_wf_config
            router._get_store = orig_get_store_router

    def test_reconcile_task_state_token_exhausted_fails_and_records_unhealthy(self):
        """Case 1 (Reconciliation): Agent blocked due to token exhaustion is classified failed."""
        from services.controller.runtime_reconciliation import reconcile_task_state

        workflow_id = "wf-rec-01"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "status": "running",
            "healthy_agents": ["opencode", "codex"],
            "unhealthy_agents": {},
        })
        self.store.save_task({
            "task_id": "wf-rec-01-impl",
            "workflow_id": workflow_id,
            "node": "impl",
            "agent": "opencode",
            "status": "working",
            "pane_id": "p-200",
            "version": 1,
        })

        screen_text = "You've used today's free messages. They reset at 8:58 AM.\n"

        ports = SimpleNamespace(
            get_task=self.store.get_task,
            get_agent_runtime_status=lambda pane_id: "blocked",
            _completion_marker_snapshot=lambda task: (False, screen_text),
            _get_store=lambda: self.store,
            _set_observed_status=lambda task, status, reason: (
                self.store.compare_and_set_task_transition(
                    task_id=task["task_id"],
                    to_status=status,
                    reason=reason,
                    source="reconcile",
                    expected_status=task["status"],
                    expected_version=task["version"],
                ) is not None
            ),
            blocked_event_type=lambda task: "blocked",
            enqueue_coordinator_event=lambda *a: self.fail("Should not enqueue coordinator event"),
            emit_done_if_allowed=lambda *a: None,
            _record_completion_sample=lambda *a, **k: None,
        )

        reconcile_task_state(ports, "wf-rec-01-impl")

        task = self.store.get_task("wf-rec-01-impl")
        self.assertEqual(task["status"], "failed")
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"].get("opencode"), "TOKEN_EXHAUSTED")

    def test_blocked_sla_task_token_exhausted_avoids_escalation(self):
        """Case 1 (SLA): Blocked task with quota exhaustion fails instead of escalating to human."""
        from services.controller.blocked_actions import process_blocked_sla_task

        workflow_id = "wf-sla-01"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "status": "running",
            "healthy_agents": ["opencode", "codex"],
            "unhealthy_agents": {},
        })
        self.store.save_task({
            "task_id": "wf-sla-01-impl",
            "workflow_id": workflow_id,
            "node": "impl",
            "agent": "opencode",
            "status": "blocked",
            "pane_id": "p-300",
            "version": 1,
        })

        screen_text = "Out of credits: payment required 402\n"

        ports = SimpleNamespace(
            time=SimpleNamespace(time=lambda: 1000.0),
            _completion_marker_snapshot=lambda task: (False, screen_text),
            _get_store=lambda: self.store,
            _set_observed_status=lambda task, status, reason: (
                self.store.compare_and_set_task_transition(
                    task_id=task["task_id"],
                    to_status=status,
                    reason=reason,
                    source="sla",
                    expected_status=task["status"],
                    expected_version=task["version"],
                ) is not None
            ),
            _claim_blocked_sla_action=lambda *a, **k: self.fail("Should not claim SLA action"),
        )

        decision = process_blocked_sla_task(ports, self.store.get_task("wf-sla-01-impl"))
        self.assertEqual(decision["action"], "token_exhausted")
        task = self.store.get_task("wf-sla-01-impl")
        self.assertEqual(task["status"], "failed")
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"].get("opencode"), "TOKEN_EXHAUSTED")

    def test_startup_token_exhausted_handover_flow(self):
        """Case 2: Startup quota exhaustion raises WorkerStartupHealthFailure and allows orchestrator handover."""
        from herdr.task_cli_launch_worker import run_launch_worker, WorkerStartupHealthFailure
        from herdr.agent_router import choose_agent

        workflow_id = "wf-startup-01"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "project_id": "proj-1",
            "status": "running",
            "healthy_agents": ["opencode", "codex"],
            "unhealthy_agents": {},
        })

        failure_payload = {
            "task_id": "wf-startup-01-task",
            "run_id": "run-1",
            "recovery_required": False,
            "disposition": "rolled_back",
            "failure_type": "RuntimeError",
            "startup_status": "TOKEN_EXHAUSTED",
        }
        worker_output = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"Worker startup TOKEN_EXHAUSTED\nHERDR_WORKER_FAILURE={json.dumps(failure_payload)}\n",
        )

        args = SimpleNamespace(
            task_id="wf-startup-01-task",
            workflow_id=workflow_id,
            source=str(self.tmp_path),
            task_type="code",
            agent="auto",
        )

        # 启动尝试 1: opencode 失败并抛出 WorkerStartupHealthFailure
        with unittest.mock.patch("herdr.task_resources.abort_launch_intent") as mock_abort:
            with self.assertRaises(WorkerStartupHealthFailure) as exc_info:
                run_launch_worker(
                    args,
                    "impl",
                    "run-1",
                    self.store,
                    {"intent_id": "intent-1", "task_id": "wf-startup-01-task", "key": "k-1", "workflow_id": workflow_id, "node_id": "impl"},
                    self.tmp_path / "clone",
                    "opencode",
                    {"execution_mode": "git"},
                    "main",
                    None,
                    "anchor",
                    None,
                    Path("/herdr"),
                    run_worker=lambda *a, **kw: worker_output,
                    get_store=lambda: self.store,
                )
            self.assertEqual(exc_info.exception.agent, "opencode")
            self.assertEqual(exc_info.exception.status, "TOKEN_EXHAUSTED")
            mock_abort.assert_called_once()

        # 验证 workflow 记录了 opencode 不健康
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"].get("opencode"), "TOKEN_EXHAUSTED")

        # 启动尝试 2: 再次路由自动选择 codex
        import herdr.agent_router as router
        orig_wf_config = router.workflow_config_for
        orig_get_store_router = router._get_store
        router.workflow_config_for = lambda wid: {
            "workflow_id": wid,
            "project_id": "proj-1",
            "nodes": [{"id": "impl", "agent_policy": {"pool": ["opencode", "codex"]}}],
        }
        router._get_store = lambda: self.store
        try:
            chosen = choose_agent(workflow_id, "impl", "code", "auto")
            self.assertEqual(chosen, "codex")
        finally:
            router.workflow_config_for = orig_wf_config
            router._get_store = orig_get_store_router

        # 启动尝试 2 执行: codex 成功返回
        codex_worker_output = SimpleNamespace(
            returncode=0,
            stdout='HERDR_WORKER_RESULT={"clone":"/tmp/clone","branch":"agent/t","pane_id":"pane-codex"}\n',
            stderr="",
        )
        with unittest.mock.patch("herdr.task_resources.record_launch_resources", return_value={"intent_id": "intent-2"}):
            res, intent = run_launch_worker(
                args,
                "impl",
                "run-1",
                self.store,
                {"intent_id": "intent-2", "task_id": "wf-startup-01-task", "key": "k-2", "workflow_id": workflow_id, "node_id": "impl"},
                self.tmp_path / "clone",
                "codex",
                {"execution_mode": "git"},
                "main",
                None,
                "anchor",
                None,
                Path("/herdr"),
                run_worker=lambda *a, **kw: codex_worker_output,
                get_store=lambda: self.store,
            )
            self.assertEqual(res["pane_id"], "pane-codex")


if __name__ == "__main__":
    unittest.main()
