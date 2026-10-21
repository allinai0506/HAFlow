"""Unit and integration tests for test node execution and gate receipt convergence V1.

Covers:
- Gate recognition consistency across CLI, Sentinel, and Controller (Problem A)
- Sync error handling and projection recovery from authoritative DB events (Problem B)
- Reconciling working tasks with valid verdicts to agent_done
- Auto-adopting watchdog-failed tasks when a valid verdict exists
- Stale epoch / candidate rejection
- Preservation of blocked verdicts
- Idempotency
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

from herdr.task_cli_verification import _is_gate_task as cli_is_gate_task
from services.controller import auto_verdict, node_policy, verdict_evidence


class TestNodeGateReceiptConvergence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.verdicts_dir = Path(self.tmp_dir.name) / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(self.tmp_dir.name) / "state.db")
        self.patch_env = patch.dict(os.environ, {
            "HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir),
            "HERDR_STATE_DB": self.db_path,
        })
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)

    # =========================================================================
    # Problem A: Gate Recognition Consistency (门禁识别一致性)
    # =========================================================================

    def _get_sentinel_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sentinel", Path("services/herdr-sentinel.py").resolve())
        sentinel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sentinel)
        return sentinel

    def test_p1_test_node_agent_without_explicit_gate_recognized_by_all(self):
        """1. test 节点 node_type=agent，未显式声明 gate，必须被 CLI、Sentinel 和 Controller（含 auto_verdict）统一识别为门禁。"""
        sentinel = self._get_sentinel_module()

        wf_cfg = {
            "nodes": [
                {"id": "test", "node_type": "agent"},
            ]
        }

        task = {
            "task_id": "t-test-1",
            "workflow_id": "wf-1",
            "node": "test",
            "status": "working",
            "pane_id": "p1",
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {
                "test": {"retry_node": "implementation"},
            }
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))
            node_is_gate = staticmethod(lambda wf_id, nid: node_policy.node_is_gate(MockPorts, wf_id, nid))
            load_tasks = staticmethod(lambda: [task])
            get_task = staticmethod(lambda tid: task)
            _gate_verdict_is_fresh = staticmethod(lambda t: True)
            gate_verdict_ready = staticmethod(lambda t: True)
            get_agent_runtime_status = staticmethod(lambda pane: "idle")
            _set_observed_status = MagicMock(return_value=True)
            emit_done_if_allowed = MagicMock()

        with patch("herdr.projects.workflow_config_for", return_value=wf_cfg):
            # CLI check
            self.assertTrue(cli_is_gate_task(task), "CLI must recognize test node_type=agent as gate")
            # Sentinel check
            self.assertTrue(sentinel._is_gate_task(task), "Sentinel must recognize test node_type=agent as gate")
            # Controller node_policy check
            self.assertTrue(node_policy.node_is_gate(MockPorts, "wf-1", "test"), "node_policy must recognize test as gate")

            # Controller auto_verdict reconciliation check
            reconciled = auto_verdict.process_gate_verdict_reconciliations(MockPorts)
            self.assertEqual(reconciled, 1, "auto_verdict must reconcile test node_type=agent when verdict is fresh")

    def test_p2_test_node_with_empty_gate_dict_recognized_by_all(self):
        """2. test 节点配置 gate: {}，必须被 CLI、Sentinel 和 Controller（含 auto_verdict）统一识别为门禁。"""
        sentinel = self._get_sentinel_module()

        wf_cfg = {
            "nodes": [
                {"id": "test", "node_type": "agent", "gate": {}},
            ]
        }

        task = {
            "task_id": "t-test-2",
            "workflow_id": "wf-1",
            "node": "test",
            "status": "working",
            "pane_id": "p1",
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {
                "test": {"retry_node": "implementation"},
            }
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))
            node_is_gate = staticmethod(lambda wf_id, nid: node_policy.node_is_gate(MockPorts, wf_id, nid))
            load_tasks = staticmethod(lambda: [task])
            get_task = staticmethod(lambda tid: task)
            _gate_verdict_is_fresh = staticmethod(lambda t: True)
            gate_verdict_ready = staticmethod(lambda t: True)
            get_agent_runtime_status = staticmethod(lambda pane: "idle")
            _set_observed_status = MagicMock(return_value=True)
            emit_done_if_allowed = MagicMock()

        with patch("herdr.projects.workflow_config_for", return_value=wf_cfg):
            self.assertTrue(cli_is_gate_task(task))
            self.assertTrue(sentinel._is_gate_task(task))
            self.assertTrue(node_policy.node_is_gate(MockPorts, "wf-1", "test"))

            reconciled = auto_verdict.process_gate_verdict_reconciliations(MockPorts)
            self.assertEqual(reconciled, 1, "auto_verdict must reconcile test node with gate: {}")

    def test_p3_explicit_gate_false_disabled_by_all(self):
        """3. test 节点显式 gate: false，必须被全部组件统一判定为非门禁。"""
        sentinel = self._get_sentinel_module()

        wf_cfg = {
            "nodes": [
                {"id": "test", "node_type": "agent", "gate": False},
            ]
        }

        task = {
            "task_id": "t-test-3",
            "workflow_id": "wf-1",
            "node": "test",
            "status": "working",
            "pane_id": "p1",
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {
                "test": {"retry_node": "implementation"},
            }
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))
            node_is_gate = staticmethod(lambda wf_id, nid: node_policy.node_is_gate(MockPorts, wf_id, nid))
            load_tasks = staticmethod(lambda: [task])
            get_task = staticmethod(lambda tid: task)
            _gate_verdict_is_fresh = staticmethod(lambda t: True)
            gate_verdict_ready = staticmethod(lambda t: True)
            get_agent_runtime_status = staticmethod(lambda pane: "idle")
            _set_observed_status = MagicMock(return_value=True)
            emit_done_if_allowed = MagicMock()

        with patch("herdr.projects.workflow_config_for", return_value=wf_cfg):
            self.assertFalse(cli_is_gate_task(task), "CLI must respect gate: False")
            self.assertFalse(sentinel._is_gate_task(task), "Sentinel must respect gate: False")
            self.assertFalse(node_policy.node_is_gate(MockPorts, "wf-1", "test"), "node_policy must respect gate: False")

            reconciled = auto_verdict.process_gate_verdict_reconciliations(MockPorts)
            self.assertEqual(reconciled, 0, "auto_verdict must NOT reconcile node with gate: False")

    def test_p4_custom_gate_nodes_regex_and_symmetry(self):
        """4. 自定义 gate 节点（如 security_gate, api_cleanroom）在各组件中一致判定为门禁。"""
        sentinel = self._get_sentinel_module()

        wf_cfg = {
            "nodes": [
                {"id": "security_gate", "node_type": "agent"},
                {"id": "api_cleanroom", "node_type": "agent"},
                {"id": "custom_checker", "node_type": "gate"},
            ]
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {}
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))
            node_is_gate = staticmethod(lambda wf_id, nid: node_policy.node_is_gate(MockPorts, wf_id, nid))

        with patch("herdr.projects.workflow_config_for", return_value=wf_cfg):
            for nid in ("security_gate", "api_cleanroom", "custom_checker"):
                task = {"workflow_id": "wf-1", "node": nid}
                self.assertTrue(cli_is_gate_task(task), f"CLI should recognize {nid} as gate")
                self.assertTrue(sentinel._is_gate_task(task), f"Sentinel should recognize {nid} as gate")
                self.assertTrue(node_policy.node_is_gate(MockPorts, "wf-1", nid), f"node_policy should recognize {nid} as gate")

    def test_p5_working_gate_task_reconciles_to_agent_done(self):
        """5. Agent 已产生合法 verdict，但 Task 仍处于 working，Controller 应自动推进至 agent_done。"""
        wf_cfg = {"nodes": [{"id": "test", "node_type": "agent"}]}
        task = {
            "task_id": "t-working-gate",
            "workflow_id": "wf-1",
            "node": "test",
            "status": "working",
            "pane_id": "p1",
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {"test": {}}
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))
            node_is_gate = staticmethod(lambda wf_id, nid: node_policy.node_is_gate(MockPorts, wf_id, nid))
            load_tasks = staticmethod(lambda: [task])
            get_task = staticmethod(lambda tid: task)
            _gate_verdict_is_fresh = staticmethod(lambda t: True)
            gate_verdict_ready = staticmethod(lambda t: True)
            get_agent_runtime_status = staticmethod(lambda pane: "idle")
            _set_observed_status = MagicMock(return_value=True)
            emit_done_if_allowed = MagicMock()

        reconciled = auto_verdict.process_gate_verdict_reconciliations(MockPorts)
        self.assertEqual(reconciled, 1)
        MockPorts._set_observed_status.assert_called_once_with(task, "agent_done", "gate_verdict_recovery")
        MockPorts.emit_done_if_allowed.assert_called_once_with(task)

    def test_p6_watchdog_failed_task_auto_adopted_when_verdict_exists(self):
        """6. Watchdog 将任务标记为 failed，但当前执行轮次存在合法 verdict，auto_verdict 应自动采纳。"""
        wf_cfg = {"nodes": [{"id": "test", "node_type": "agent"}]}
        task = {
            "task_id": "t-failed-watchdog",
            "workflow_id": "wf-1",
            "node": "test",
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {"test": {}}
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))
            node_is_gate = staticmethod(lambda wf_id, nid: node_policy.node_is_gate(MockPorts, wf_id, nid))
            load_tasks = staticmethod(lambda: [task])
            get_task = staticmethod(lambda tid: task)
            _gate_verdict_is_fresh = staticmethod(lambda t: True)
            read_gate_verdict = staticmethod(lambda t: ("pass", "all tests green", "file"))
            try_auto_verdict = MagicMock(return_value=True)

        reconciled = auto_verdict.process_gate_verdict_reconciliations(MockPorts)
        self.assertEqual(reconciled, 1)
        MockPorts.try_auto_verdict.assert_called_once_with("t-failed-watchdog")

    # =========================================================================
    # Problem B: Business Acceptance & Verdict Sync Failures (业务验收与同步失败)
    # =========================================================================

    def test_p7_record_business_acceptance_does_not_swallow_file_sync_exception(self):
        """7. record_business_acceptance 在文件同步异常时不应静默吞掉，必须抛出明确异常。"""
        from herdr.task_checkpoint import record_business_acceptance

        task_id = "t-sync-err-1"
        run_id = "run-1"
        epoch = "epoch-1"
        candidate_sha = "a" * 40
        execution_id = "exec-1"

        store_mock = MagicMock()
        store_mock.db_path = self.db_path

        mock_task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "execution_id": execution_id,
            "candidate_sha": candidate_sha,
            "status": "working",
            "run_id": run_id,
            "completion_epoch": epoch,
            "acceptance_criteria": ["AC-1"],
            "clone_path": None,
            "node": "test",
            "stage": "test",
        }
        mock_workflow = {
            "workflow_id": "wf-1",
            "execution_id": execution_id,
            "candidate_sha": candidate_sha,
            "status": "running",
        }

        with patch("herdr.state_db.get_readonly_db_connection") as mock_ro_conn, \
             patch("herdr.state_db._decode_task_row", return_value=mock_task), \
             patch("herdr.recovery_store._workflow_snapshot", return_value=(mock_workflow, {})), \
             patch("herdr.recovery_store._transaction") as mock_tx, \
             patch("herdr.task_checkpoint.validate_checkpoint_artifact", side_effect=lambda t, ref, **kw: ref), \
             patch("herdr.state_db.record_event"):

            conn_inst = MagicMock()
            mock_ro_conn.return_value = conn_inst
            mock_tx.return_value.__enter__.return_value = conn_inst

            # Simulate filesystem failure when writing tmp verdict file
            with patch.object(Path, "write_text", side_effect=OSError("Disk write failed: No space left")):
                with self.assertRaises(OSError):
                    record_business_acceptance(
                        store_mock,
                        task_id,
                        run_id,
                        epoch,
                        candidate_sha,
                        "pass",
                        [{"observation_id": "obs-1", "sha256": "digest-1"}],
                        ["AC-1"],
                    )

    def test_p8_controller_restores_verdict_file_from_authoritative_db_event(self):
        """8. DB 验收已记录（business_acceptance_recorded），但磁盘 verdict 文件丢失，Controller 能自动从权威 DB 事件恢复投影并完成裁决。"""
        from herdr.direct_dispatch import gate_verdict_path
        from herdr.task_checkpoint import project_gate_verdict_from_business_acceptance

        task_id = "t-restore-proj-1"
        run_id = "run-1"
        epoch = "epoch-100"
        candidate_sha = "b" * 40
        execution_id = "exec-100"

        store_mock = MagicMock()
        store_mock.db_path = self.db_path

        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "execution_id": execution_id,
            "candidate_sha": candidate_sha,
            "completion_epoch": epoch,
            "run_id": run_id,
            "status": "working",
            "node": "test",
            "stage": "test",
            "acceptance_criteria": ["AC-1"],
            "agent": "test-verifier",
            "execution_token": "token-100",
        }

        receipt = {
            "id": 999,
            "payload": {
                "verdict": "pass",
                "candidate_sha": candidate_sha,
                "run_id": run_id,
                "epoch": epoch,
                "execution_id": execution_id,
                "artifacts": [{"observation_id": "obs-1", "sha256": "hash-1"}],
                "criteria": ["AC-1"],
            }
        }

        # Ensure no verdict file exists initially
        vpath = Path(gate_verdict_path(task_id))
        if vpath.exists():
            vpath.unlink()

        with patch("herdr.state_db.get_readonly_db_connection"), \
             patch("herdr.task_checkpoint.business_acceptance_receipt", return_value=receipt):

            restored_path = project_gate_verdict_from_business_acceptance(store_mock, task)
            self.assertIsNotNone(restored_path)
            self.assertTrue(os.path.exists(restored_path))

            # Verify contents of restored projection
            doc = json.loads(Path(restored_path).read_text(encoding="utf-8"))
            self.assertEqual(doc["verdict"], "pass")
            self.assertEqual(doc["candidate_sha"], candidate_sha)
            self.assertEqual(doc["epoch"], epoch)
            self.assertEqual(doc["task_id"], task_id)
            self.assertEqual(doc["execution_token"], "token-100")
            self.assertEqual(doc["verifier_agent"], "test-verifier")

    def test_p9_restore_projection_rejects_stale_epoch_or_candidate(self):
        """9. 旧 epoch 或旧 candidate_sha 的 DB 事件必须被拒绝，绝不能误恢复过期凭证。"""
        from herdr.task_checkpoint import project_gate_verdict_from_business_acceptance

        task_id = "t-stale-reject-1"
        current_candidate = "c" * 40
        stale_candidate = "d" * 40
        current_epoch = "epoch-200"
        stale_epoch = "epoch-100"

        store_mock = MagicMock()
        store_mock.db_path = self.db_path

        task = {
            "task_id": task_id,
            "candidate_sha": current_candidate,
            "completion_epoch": current_epoch,
            "run_id": "run-2",
        }

        # Case 1: Stale candidate_sha
        stale_candidate_receipt = {
            "id": 1001,
            "payload": {
                "verdict": "pass",
                "candidate_sha": stale_candidate,
                "epoch": current_epoch,
                "run_id": "run-2",
                "artifacts": [{"observation_id": "obs-1"}],
            }
        }

        with patch("herdr.state_db.get_readonly_db_connection"), \
             patch("herdr.task_checkpoint.business_acceptance_receipt", return_value=stale_candidate_receipt):
            self.assertIsNone(project_gate_verdict_from_business_acceptance(store_mock, task),
                              "Must reject stale candidate_sha")

        # Case 2: Stale epoch
        stale_epoch_receipt = {
            "id": 1002,
            "payload": {
                "verdict": "pass",
                "candidate_sha": current_candidate,
                "epoch": stale_epoch,
                "run_id": "run-2",
                "artifacts": [{"observation_id": "obs-1"}],
            }
        }

        with patch("herdr.state_db.get_readonly_db_connection"), \
             patch("herdr.task_checkpoint.business_acceptance_receipt", return_value=stale_epoch_receipt):
            self.assertIsNone(project_gate_verdict_from_business_acceptance(store_mock, task),
                              "Must reject stale epoch")

    def test_p10_restore_projection_preserves_blocked_verdict(self):
        """10. 权威 DB 事件为 blocked 时，恢复出的投影必须是 blocked，绝不能被错误转换为 pass。"""
        from herdr.direct_dispatch import gate_verdict_path
        from herdr.task_checkpoint import project_gate_verdict_from_business_acceptance

        task_id = "t-blocked-preserve-1"
        candidate_sha = "e" * 40
        epoch = "epoch-300"
        store_mock = MagicMock()
        store_mock.db_path = self.db_path

        task = {
            "task_id": task_id,
            "candidate_sha": candidate_sha,
            "completion_epoch": epoch,
            "run_id": "run-3",
        }

        blocked_receipt = {
            "id": 1003,
            "payload": {
                "verdict": "blocked",
                "candidate_sha": candidate_sha,
                "epoch": epoch,
                "run_id": "run-3",
                "artifacts": [{"observation_id": "obs-blocked"}],
            }
        }

        with patch("herdr.state_db.get_readonly_db_connection"), \
             patch("herdr.task_checkpoint.business_acceptance_receipt", return_value=blocked_receipt):
            restored = project_gate_verdict_from_business_acceptance(store_mock, task)
            self.assertIsNotNone(restored)
            doc = json.loads(Path(restored).read_text(encoding="utf-8"))
            self.assertEqual(doc["verdict"], "blocked", "Blocked verdict must be preserved strictly as blocked")

    def test_p11_reconciliation_idempotency(self):
        """11. 同一凭证重复处理不得重复触发状态流转或重复推进。"""
        wf_cfg = {"nodes": [{"id": "test", "node_type": "agent"}]}
        completed_task = {
            "task_id": "t-idempotent-1",
            "workflow_id": "wf-1",
            "node": "test",
            "status": "completed",
            "stage_verdict": "pass",
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {"test": {}}
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))
            node_is_gate = staticmethod(lambda wf_id, nid: node_policy.node_is_gate(MockPorts, wf_id, nid))
            load_tasks = staticmethod(lambda: [completed_task])
            get_task = staticmethod(lambda tid: completed_task)
            _gate_verdict_is_fresh = staticmethod(lambda t: True)
            gate_verdict_ready = staticmethod(lambda t: True)
            _set_observed_status = MagicMock()
            try_auto_verdict = MagicMock()

        reconciled = auto_verdict.process_gate_verdict_reconciliations(MockPorts)
        self.assertEqual(reconciled, 0, "Already completed task must not be reconciled")
        MockPorts._set_observed_status.assert_not_called()
        MockPorts.try_auto_verdict.assert_not_called()

    # =========================================================================
    # Section III: Real End-to-End Chain Verification (真实完整链路集成验证)
    # =========================================================================

    def test_p12_full_chain_test_execution_acceptance_auto_verdict_advance(self):
        """12. 真实端到端完整链路：业务验收记录 -> 裁决文件缺失自愈 -> Controller 自动识别/推进 -> 无需人工裁决。"""
        import sys
        from herdr import state_db
        from herdr.direct_dispatch import gate_verdict_path
        from herdr.state_store import get_state_store
        from herdr.task_checkpoint import publish_task_checkpoint, record_business_acceptance

        store = get_state_store(Path(self.db_path))
        candidate_sha = "f" * 40

        store.save_workflow({
            "workflow_id": "wf-real-test",
            "execution_id": "exec-real-test",
            "status": "running",
            "created_at": 1000.0,
        })
        state_db.record_event({
            "workflow_id": "wf-real-test",
            "event_type": "candidate_frozen",
            "source": "critical-path-scheduler",
            "timestamp": 1001.0,
            "payload": {"candidate_sha": candidate_sha},
        }, db_path=Path(self.db_path))

        task_data = {
            "task_id": "t-e2e-gate-1",
            "workflow_id": "wf-real-test",
            "execution_id": "exec-real-test",
            "candidate_sha": candidate_sha,
            "status": "working",
            "node": "test",
            "stage": "test",
            "run_id": "run-e2e-1",
            "completion_epoch": "epoch-e2e-1",
            "acceptance_criteria": ["AC-PASS"],
            "clone_path": None,
        }
        store.save_task(task_data)

        # 1. Test Agent publishes genuine test execution checkpoint
        receipt = publish_task_checkpoint(
            "t-e2e-gate-1", "run-e2e-1", "epoch-e2e-1",
            "Execution Output: 45 passed, 0 failed in 1.2s; exit code: 0",
            1,
            store=store,
            summary="Test suite completed successfully",
        )

        # 2. Record business acceptance
        artifacts = [{"observation_id": receipt["observation_id"], "sha256": receipt["sha256"]}]
        ev = record_business_acceptance(
            store,
            "t-e2e-gate-1",
            "run-e2e-1",
            "epoch-e2e-1",
            candidate_sha,
            "pass",
            artifacts,
            ["AC-PASS"],
        )
        self.assertIsNotNone(ev)

        # 3. Simulate file desync (file missing on disk)
        vfile = Path(gate_verdict_path("t-e2e-gate-1"))
        if vfile.exists():
            vfile.unlink()
        self.assertFalse(vfile.exists())

        # 4. Controller reconciles and auto-adopts verdict without coordinator intervention
        mock_subprocess_run = MagicMock()
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "ok"
        mock_subprocess_run.return_value.stderr = ""

        class ControllerPorts:
            TASK_MANAGER = "herdr-task"
            FIX_LOOP_MAX = 5
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {"test": {}}
            DEFAULT_MAX_CLOCK_SKEW_SECONDS = 15.0
            DEFAULT_GATE_HANDOFF_GRACE_SECONDS = 5.0
            GATE_VERDICT_FILE = "gate-verdict.json"
            _GATE_VERDICT_ALIASES: ClassVar[dict[str, str]] = {"pass": "pass", "blocked": "blocked"}
            os = os
            json = json
            sys = sys
            time = __import__("time")
            math = __import__("math")
            hashlib = __import__("hashlib")
            direct_dispatch_planner = __import__("herdr.direct_dispatch", fromlist=["gate_verdict_path"])
            _get_store = staticmethod(lambda: store)
            get_task = staticmethod(lambda tid: store.get_task(tid))
            load_tasks = staticmethod(lambda: store.list_tasks(workflow_id="wf-real-test"))
            workflow_config_for = staticmethod(lambda wfid: {"nodes": [{"id": "test", "node_type": "agent"}]})
            resolve_gate_config = staticmethod(lambda node, nid: node_policy.resolve_gate_config(ControllerPorts, node, nid))
            node_is_gate = staticmethod(lambda wfid, nid: node_policy.node_is_gate(ControllerPorts, wfid, nid))
            get_stage_policy = staticmethod(lambda nid: {})
            auto_verdict_enabled = staticmethod(lambda: True)
            get_agent_runtime_status = staticmethod(lambda pane: "idle")
            sync_verdict_from_business_acceptance = staticmethod(lambda task: verdict_evidence.sync_verdict_from_business_acceptance(ControllerPorts, task))
            _gate_verdict_file_candidates = staticmethod(lambda task: verdict_evidence._gate_verdict_file_candidates(ControllerPorts, task))
            _gate_verdict_file_is_fresh = staticmethod(lambda path, task: verdict_evidence._gate_verdict_file_is_fresh(ControllerPorts, path, task))
            _gate_verdict_is_fresh = staticmethod(lambda task: verdict_evidence._gate_verdict_is_fresh(ControllerPorts, task))
            _gate_verdict_file_mtime = staticmethod(lambda task: verdict_evidence._gate_verdict_file_mtime(ControllerPorts, task))
            _verdict_from_file = staticmethod(lambda task: verdict_evidence._verdict_from_file(ControllerPorts, task))
            _verdict_from_screen = staticmethod(lambda task: (None, ""))
            _normalize_gate_verdict = staticmethod(lambda v: verdict_evidence._normalize_gate_verdict(ControllerPorts, v))
            read_gate_verdict = staticmethod(lambda task: verdict_evidence.read_gate_verdict(ControllerPorts, task))
            _verdict_affected_task_ids = staticmethod(lambda task: verdict_evidence._verdict_affected_task_ids(ControllerPorts, task))
            gate_verdict_ready = staticmethod(lambda task: True)
            _set_observed_status = staticmethod(lambda task, status, reason: store.transition_task(task["task_id"], status, reason=reason) is not None)
            emit_done_if_allowed = staticmethod(lambda task: None)
            subprocess = MagicMock(run=mock_subprocess_run)

        # 5. Process reconciliation: file projection restored, task transitions to agent_done
        reconciled = auto_verdict.process_gate_verdict_reconciliations(ControllerPorts)
        self.assertEqual(reconciled, 1)
        self.assertTrue(vfile.exists(), "Verdict file must be restored on disk from authoritative event")
        self.assertEqual(store.get_task("t-e2e-gate-1")["status"], "agent_done")

        # 6. Try auto verdict: auto adopts pass and spawns TASK_MANAGER set without coordinator intervention
        adopted = auto_verdict.try_auto_verdict(ControllerPorts, "t-e2e-gate-1")
        self.assertTrue(adopted)
        mock_subprocess_run.assert_called()
        cmd = mock_subprocess_run.call_args[0][0]
        self.assertIn("completed", cmd)
        self.assertIn("pass", cmd)



if __name__ == "__main__":
    unittest.main()
