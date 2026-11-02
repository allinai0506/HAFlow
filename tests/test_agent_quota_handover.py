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

        with unittest.mock.patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
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

        with unittest.mock.patch("herdr.task_resources.owned_live_pane", return_value=(True, "identity_match")):
            decision = process_blocked_sla_task(ports, self.store.get_task("wf-sla-01-impl"))
        self.assertEqual(decision["action"], "token_exhausted")
        task = self.store.get_task("wf-sla-01-impl")
        self.assertEqual(task["status"], "failed")
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"].get("opencode"), "TOKEN_EXHAUSTED")

    def test_mismatched_pane_session_leaves_task_status_and_workflow_health_unchanged(self):
        """Invariant 11: Mismatched live session rejects quota evidence, preserving status and health."""
        from services.controller.runtime_reconciliation import reconcile_task_state

        workflow_id = "wf-mismatch-01"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "status": "running",
            "healthy_agents": ["opencode", "codex"],
            "unhealthy_agents": {},
        })
        self.store.save_task({
            "task_id": "wf-mismatch-01-impl",
            "workflow_id": workflow_id,
            "node": "impl",
            "agent": "opencode",
            "status": "working",
            "pane_id": "reused-pane-999",
            "version": 1,
        })

        # The pane text contains genuine quota error from another session/agent
        foreign_screen = "You've used today's free messages. They reset at 8:58 AM.\n"

        ports = SimpleNamespace(
            get_task=self.store.get_task,
            get_agent_runtime_status=lambda pane_id: "blocked",
            _completion_marker_snapshot=lambda task: (False, foreign_screen),
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
            enqueue_coordinator_event=lambda *a: None,
            emit_done_if_allowed=lambda *a: None,
            _record_completion_sample=lambda *a, **k: None,
        )

        # Session mismatch: owned_live_pane returns False
        with unittest.mock.patch("herdr.task_resources.owned_live_pane", return_value=(False, "session_mismatch")):
            reconcile_task_state(ports, "wf-mismatch-01-impl")

        task = self.store.get_task("wf-mismatch-01-impl")
        self.assertEqual(task["status"], "working")
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"], {})
        self.assertIn("opencode", wf["healthy_agents"])

    def test_sentinel_rejects_quota_on_unowned_pane_or_working_agent(self):
        """Sentinel must not fail a working agent or an unowned pane on quota evidence."""
        sentinel_mod = importlib.import_module("services.herdr-sentinel")

        workflow_id = "wf-sentinel-guard"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "status": "running",
            "healthy_agents": ["opencode"],
            "unhealthy_agents": {},
        })
        self.store.save_task({
            "task_id": "wf-sentinel-guard-impl",
            "workflow_id": workflow_id,
            "node": "impl",
            "agent": "opencode",
            "status": "working",
            "pane_id": "pane-guard",
            "version": 1,
        })

        task = self.store.get_task("wf-sentinel-guard-impl")
        changes = {}
        expected = {}
        versions = {}

        # 1. 模拟 agent 正在 working，即使屏幕包含真正的 quota 文本，Sentinel 也不应误判
        with unittest.mock.patch.object(sentinel_mod, "agent_status", return_value="working"), \
             unittest.mock.patch.object(sentinel_mod, "_owned_pane", return_value=(True, "identity_match")):
            # 屏幕包含 quota 耗尽文本
            screen = "You've used today's free messages. They reset at 8:58 AM.\n"
            from herdr.deep_preflight import classify_runtime_token_exhausted
            if sentinel_mod.agent_status("pane-guard") != "working":
                owned, _ = sentinel_mod._owned_pane(task)
                if owned and classify_runtime_token_exhausted(screen, "opencode") == "TOKEN_EXHAUSTED":
                    sentinel_mod.observe_token_exhausted_pattern(task, changes=changes, expected=expected, versions=versions)
            self.assertEqual(changes, {})

        # 2. 模拟 pane 所有权不匹配 (session_mismatch)，Sentinel 拒绝采信
        with unittest.mock.patch.object(sentinel_mod, "agent_status", return_value="blocked"), \
             unittest.mock.patch.object(sentinel_mod, "_owned_pane", return_value=(False, "session_mismatch")):
            screen = "You've used today's free messages. They reset at 8:58 AM.\n"
            from herdr.deep_preflight import classify_runtime_token_exhausted
            if sentinel_mod.agent_status("pane-guard") != "working":
                owned, _ = sentinel_mod._owned_pane(task)
                if owned and classify_runtime_token_exhausted(screen, "opencode") == "TOKEN_EXHAUSTED":
                    sentinel_mod.observe_token_exhausted_pattern(task, changes=changes, expected=expected, versions=versions)
            self.assertEqual(changes, {})

        # 3. 模拟普通任务 prompt 包含 rate-limit，Sentinel 不应匹配
        with unittest.mock.patch.object(sentinel_mod, "agent_status", return_value="blocked"), \
             unittest.mock.patch.object(sentinel_mod, "_owned_pane", return_value=(True, "identity_match")):
            prompt_screen = "Working: Implement a rate-limit middleware and add tests.\n"
            from herdr.deep_preflight import classify_runtime_token_exhausted
            if sentinel_mod.agent_status("pane-guard") != "working":
                owned, _ = sentinel_mod._owned_pane(task)
                if owned and classify_runtime_token_exhausted(prompt_screen, "opencode") == "TOKEN_EXHAUSTED":
                    sentinel_mod.observe_token_exhausted_pattern(task, changes=changes, expected=expected, versions=versions)
            self.assertEqual(changes, {})

        # 确保 workflow 和 task 均未被标记或修改
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"], {})
        self.assertEqual(self.store.get_task("wf-sentinel-guard-impl")["status"], "working")

    def test_runtime_token_exhausted_negative_cases_for_prompts_and_test_output(self):
        """Negative cases: user prompts and successful test outputs must NOT match runtime quota exhaustion."""
        from herdr.deep_preflight import classify_runtime_token_exhausted

        negative_screens = [
            "Working: Implement a rate-limit middleware and add tests.",
            "Tests: 402 passed in 12s",
            "def check_token_limit(token, limit):\n    return token <= limit\n",
            "class RateLimiter:\n    def __init__(self, rate_limit=100):\n        self.rate_limit = rate_limit",
            "pytest output: 402 passed, 0 failed in 3.5s",
        ]
        for screen in negative_screens:
            self.assertIsNone(
                classify_runtime_token_exhausted(screen, "opencode"),
                msg=f"Should not classify as TOKEN_EXHAUSTED: {screen}"
            )

        positive_screens = [
            "You've used today's free messages. They reset at 8:58 AM.\nUpgrade to OpenCode Go",
            "You exceeded your current quota, please check your plan and billing details",
            "insufficient_quota",
            "Rate limit reached for requests",
            "status code: 402 Payment Required",
            "Out of credits: payment required 402",
        ]
        for screen in positive_screens:
            self.assertEqual(
                classify_runtime_token_exhausted(screen, "opencode"),
                "TOKEN_EXHAUSTED",
                msg=f"Should classify as TOKEN_EXHAUSTED: {screen}"
            )

    def test_controller_set_observed_status_populates_failure_reason_for_recovery(self):
        """Verify Controller's _set_observed_status preserves failure_reason so select_infra_failures_for_recovery matches it."""
        from services.controller.task_state import _set_observed_status
        from herdr.liveness import select_infra_failures_for_recovery

        task_id = "wf-ctrl-fail-impl"
        self.store.save_task({
            "task_id": task_id,
            "workflow_id": "wf-ctrl-fail",
            "node": "impl",
            "agent": "opencode",
            "status": "working",
            "pane_id": "p-500",
            "version": 1,
        })
        task = self.store.get_task(task_id)

        ports = SimpleNamespace(
            os=__import__("os"),
            _task_version=lambda t: t.get("version"),
            set_task_status=lambda tid, status, **kw: self.store.compare_and_set_task_transition(
                task_id=tid,
                to_status=status,
                reason=kw.get("metadata", {}).get("failure_reason") or kw.get("metadata", {}).get("controller_reason"),
                source=kw.get("source", "test"),
                metadata=kw.get("metadata"),
                expected_status=kw.get("expected_status"),
                expected_version=kw.get("expected_version"),
            ),
        )

        success = _set_observed_status(ports, task, "failed", "token_exhausted")
        self.assertTrue(success)

        updated_task = self.store.get_task(task_id)
        self.assertEqual(updated_task["status"], "failed")

        candidates = select_infra_failures_for_recovery(
            [updated_task],
            {"token_exhausted", "dispatch_delivery_fuse"},
            max_attempts=2,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["task_id"], task_id)

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

    def test_concurrent_dual_agent_quota_exhaustion_on_real_sqlite(self):
        """Problem 1: Concurrent dual-agent quota exhaustion on real SQLite must preserve both records without lost updates."""
        from concurrent.futures import ThreadPoolExecutor
        from herdr.agent_router import choose_agent, RouterHealthRejection, RouterPolicyRejection
        import herdr.agent_router as router

        workflow_id = "wf-concurrent-01"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "project_id": "proj-1",
            "status": "running",
            "healthy_agents": ["opencode", "codex", "claude"],
            "unhealthy_agents": {},
        })

        # 并发执行两路独立的额度耗尽写入
        def mark_unhealthy(agent_name):
            # 每个线程独立获取 StateStore 连接，访问同一真实 SQLite 文件
            store_instance = SQLiteStateStore(self.db_path)
            return store_instance.record_agent_unhealthy(workflow_id, agent_name, status="TOKEN_EXHAUSTED")

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(mark_unhealthy, "opencode")
            fut2 = executor.submit(mark_unhealthy, "codex")
            fut1.result()
            fut2.result()

        # 验证两个失败记录全部持久化在 SQLite 中，没有被覆盖
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"].get("opencode"), "TOKEN_EXHAUSTED")
        self.assertEqual(wf["unhealthy_agents"].get("codex"), "TOKEN_EXHAUSTED")
        self.assertNotIn("opencode", wf.get("healthy_agents", []))
        self.assertNotIn("codex", wf.get("healthy_agents", []))
        self.assertIn("claude", wf.get("healthy_agents", []))

        # 验证 Router 不会重新选择任何一个耗尽额度的 Agent，只能选 claude
        orig_wf_config = router.workflow_config_for
        orig_get_store_router = router._get_store
        orig_ensure_pool = router.ensure_pool_for_project
        router.workflow_config_for = lambda wid: {
            "workflow_id": wid,
            "project_id": "proj-1",
            "nodes": [{"id": "impl", "agent_policy": {"pool": ["opencode", "codex", "claude"]}}],
        }
        router.ensure_pool_for_project = lambda pid: {"allowed_agents": ["opencode", "codex", "claude"]}
        router._get_store = lambda: self.store
        try:
            chosen = choose_agent(workflow_id, "impl", "code", "auto")
            self.assertEqual(chosen, "claude")

            # 若 claude 也耗尽，Router 必须触发门禁拒绝，不可复活 opencode 或 codex
            self.store.record_agent_unhealthy(workflow_id, "claude", status="TOKEN_EXHAUSTED")
            with self.assertRaises(RouterPolicyRejection):
                choose_agent(workflow_id, "impl", "code", "auto")
            with self.assertRaises(RouterHealthRejection):
                choose_agent(workflow_id, "impl", "code", "claude")
        finally:
            router.workflow_config_for = orig_wf_config
            router._get_store = orig_get_store_router
            router.ensure_pool_for_project = orig_ensure_pool

    def test_full_chain_quota_exhaustion_detection_to_handover_launch(self):
        """End-to-end wiring: detection -> failure persistence -> auto-recovery -> safe supersede -> router exclusion -> new agent launch."""
        from services.controller.infra_recovery import recover_infra_failed_tasks
        from herdr.agent_router import choose_agent
        import herdr.agent_router as router
        from herdr.node_capacity import node_usage

        workflow_id = "wf-fullchain-01"
        self.store.save_workflow({
            "workflow_id": workflow_id,
            "project_id": "proj-chain",
            "status": "running",
            "healthy_agents": ["opencode", "codex"],
            "unhealthy_agents": {},
        })

        node_cfg = {"id": "impl", "max_tasks_per_node": 2, "agent_policy": {"pool": ["opencode", "codex"]}}
        task_id = "wf-fullchain-impl"
        self.store.save_task({
            "task_id": task_id,
            "workflow_id": workflow_id,
            "node": "impl",
            "agent": "opencode",
            "status": "working",
            "pane_id": "p-chain-1",
            "version": 1,
            "dispatch_round": 1,
        })

        # 1. 检测：现场屏幕出现真实配额耗尽证据，Sentinel 进行识别并持久化
        sentinel_mod = importlib.import_module("services.herdr-sentinel")
        orig_get_store = sentinel_mod._get_store
        sentinel_mod._get_store = lambda: self.store
        try:
            task = self.store.get_task(task_id)
            changes = {}
            expected = {}
            versions = {}
            sentinel_mod.observe_token_exhausted_pattern(task, changes=changes, expected=expected, versions=versions)
            self.assertEqual(changes.get(task_id), ("failed", "token_exhausted"))

            # 持久化失败状态与原因
            self.store.compare_and_set_task_transition(
                task_id=task_id,
                to_status="failed",
                reason="token_exhausted",
                source="herdr-sentinel",
                metadata={"failure_reason": "token_exhausted"},
                expected_status="working",
                expected_version=1,
            )
        finally:
            sentinel_mod._get_store = orig_get_store

        # 验证失败原因与 Agent 不健康状态已稳固持久化
        failed_task = self.store.get_task(task_id)
        self.assertEqual(failed_task["status"], "failed")
        self.assertEqual(failed_task["failure_reason"], "token_exhausted")
        wf = self.store.get_workflow(workflow_id)
        self.assertEqual(wf["unhealthy_agents"].get("opencode"), "TOKEN_EXHAUSTED")

        # 2. 自动恢复：Controller 发现 infra 故障任务，安全将其 supersede
        controller_ports = SimpleNamespace(
            TASK_MANAGER="bin/herdr-task",
            AUTO_RECOVER_REASONS={"token_exhausted", "agent_process_crash", "hard_timeout_watchdog"},
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
                    # 真实模拟 herdr-task supersede 将旧任务转为 superseded
                    self.store.compare_and_set_task_transition(
                        task_id=task_id,
                        to_status="superseded",
                        reason="auto-recover: infrastructure failure",
                        source="herdr-task:supersede",
                        expected_status=self.store.get_task(task_id)["status"],
                        expected_version=self.store.get_task(task_id)["version"],
                    ),
                    SimpleNamespace(returncode=0, stdout="", stderr="")
                )[1]
            ),
        )

        recovered = recover_infra_failed_tasks(controller_ports, workflow_id, tasks=self.store.list_tasks())
        self.assertTrue(recovered)
        superseded_task = self.store.get_task(task_id)
        self.assertEqual(superseded_task["status"], "superseded")

        # 3. Router 排除不可用 Agent 并交接给 codex
        orig_wf_config = router.workflow_config_for
        orig_get_store_router = router._get_store
        router.workflow_config_for = lambda wid: {
            "workflow_id": wid,
            "project_id": "proj-chain",
            "nodes": [node_cfg],
        }
        router._get_store = lambda: self.store
        try:
            chosen = choose_agent(workflow_id, "impl", "code", "auto")
            self.assertEqual(chosen, "codex")
        finally:
            router.workflow_config_for = orig_wf_config
            router._get_store = orig_get_store_router

        # 4. 新 Agent (codex) 实际启动并登记任务
        replacement_task_id = "wf-fullchain-impl-r2"
        self.store.save_task({
            "task_id": replacement_task_id,
            "workflow_id": workflow_id,
            "node": "impl",
            "agent": chosen,
            "status": "working",
            "supersedes": task_id,
            "dispatch_round": 2,
            "version": 1,
        })

        # 5. 验证容量契约：旧任务因已被 supersede 成功退役，新任务占槽，总预算符合 max_tasks_per_node
        usage = node_usage(node_cfg, self.store.list_tasks(), workflow_id)
        self.assertEqual(usage["registered_task_count"], 2)
        self.assertEqual(usage["retired_task_count"], 1)  # 旧任务 superseded 退役
        self.assertEqual(usage["budget_task_count"], 1)   # 当前仅新任务计入在役预算
        self.assertEqual(usage["active_task_count"], 1)
        self.assertFalse(usage["overflow"])


if __name__ == "__main__":
    unittest.main()
