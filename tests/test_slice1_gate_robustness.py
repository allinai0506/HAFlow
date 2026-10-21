import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

from herdr.domain import get_domain_adapter
from herdr.task_cli_verification import _is_gate_task as cli_is_gate_task
from services.controller import node_policy


class TestSlice1GateRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.verdicts_dir = Path(self.tmp_dir.name) / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True, exist_ok=True)
        self.patch_env = patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)})
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)

    # -------------------------------------------------------------------------
    # P1: 门禁判定口径对称性测试
    # -------------------------------------------------------------------------
    def test_p1_gate_empty_dict_symmetry(self):
        """P1: node_type: agent + gate: {} 必须被 CLI、Sentinel 和 Controller 统一识别为门禁。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("sentinel", Path("services/herdr-sentinel.py").resolve())
        sentinel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sentinel)

        wf_cfg = {
            "nodes": [
                {"id": "test", "node_type": "agent", "gate": {}},
                {"id": "review", "node_type": "agent", "gate": {}},
                {"id": "custom_gate", "node_type": "agent", "gate": {}},
                {"id": "aggregate_results", "node_type": "agent"},
                {"id": "disabled_test", "node_type": "agent", "gate": False},
                {"id": "exec_brief", "node_type": "gate"},
            ]
        }

        class MockPorts:
            GATE_DEFAULTS: ClassVar[dict[str, Any]] = {
                "test": {"retry_node": "implementation"},
                "review": {"retry_node": "implementation"},
                "wrapup": {"retry_node": "implementation"},
            }
            FIX_LOOP_MAX = 5
            get_stage_policy = staticmethod(lambda node_id: {})
            workflow_config_for = staticmethod(lambda wf_id: wf_cfg)
            resolve_gate_config = staticmethod(lambda node, node_id: node_policy.resolve_gate_config(MockPorts, node, node_id))

        with patch("herdr.projects.workflow_config_for", return_value=wf_cfg):
            # 1. test 节点 (gate: {}) -> 必须是 gate
            task_test = {"workflow_id": "wf-1", "node": "test"}
            self.assertTrue(cli_is_gate_task(task_test), "CLI should recognize test with gate: {} as gate")
            self.assertTrue(sentinel._is_gate_task(task_test), "Sentinel should recognize test with gate: {} as gate")
            self.assertTrue(node_policy.node_is_gate(MockPorts, "wf-1", "test"), "Controller should recognize test as gate")

            # 2. review 节点 (gate: {}) -> 必须是 gate
            task_review = {"workflow_id": "wf-1", "node": "review"}
            self.assertTrue(cli_is_gate_task(task_review), "CLI should recognize review with gate: {} as gate")
            self.assertTrue(sentinel._is_gate_task(task_review), "Sentinel should recognize review with gate: {} as gate")
            self.assertTrue(node_policy.node_is_gate(MockPorts, "wf-1", "review"), "Controller should recognize review as gate")

            # 3. custom_gate 节点 (自定义名 + gate: {}) -> 必须是 gate
            task_custom = {"workflow_id": "wf-1", "node": "custom_gate"}
            self.assertTrue(cli_is_gate_task(task_custom), "CLI should recognize custom_gate with gate: {} as gate")
            self.assertTrue(sentinel._is_gate_task(task_custom), "Sentinel should recognize custom_gate with gate: {} as gate")
            self.assertTrue(node_policy.node_is_gate(MockPorts, "wf-1", "custom_gate"), "Controller should recognize custom_gate as gate")

            # 4. aggregate_results 节点 (普通 agent 无 gate) -> 必须不是 gate
            task_agg = {"workflow_id": "wf-1", "node": "aggregate_results"}
            self.assertFalse(cli_is_gate_task(task_agg), "CLI should NOT recognize aggregate_results as gate")
            self.assertFalse(sentinel._is_gate_task(task_agg), "Sentinel should NOT recognize aggregate_results as gate")
            self.assertFalse(node_policy.node_is_gate(MockPorts, "wf-1", "aggregate_results"), "Controller should NOT recognize aggregate_results as gate")

            # 5. disabled_test 节点 (显式 gate: False) -> 必须不是 gate
            task_dis = {"workflow_id": "wf-1", "node": "disabled_test"}
            self.assertFalse(cli_is_gate_task(task_dis), "CLI should respect gate: False")
            self.assertFalse(sentinel._is_gate_task(task_dis), "Sentinel should respect gate: False")
            self.assertFalse(node_policy.node_is_gate(MockPorts, "wf-1", "disabled_test"), "Controller should respect gate: False")

            # 6. exec_brief 节点 (node_type: gate) -> 必须是 gate
            task_exec = {"workflow_id": "wf-1", "node": "exec_brief"}
            self.assertTrue(cli_is_gate_task(task_exec), "CLI should recognize node_type: gate as gate")
            self.assertTrue(sentinel._is_gate_task(task_exec), "Sentinel should recognize node_type: gate as gate")
            self.assertTrue(node_policy.node_is_gate(MockPorts, "wf-1", "exec_brief"), "Controller should recognize node_type: gate as gate")

    # -------------------------------------------------------------------------
    # P8: record_business_acceptance 自动生成 gate-verdicts 文件测试
    # -------------------------------------------------------------------------
    def test_p8_record_business_acceptance_creates_verdict_file(self):
        """P8: record_business_acceptance 在落盘事件时，必须同步写入合规的 gate-verdicts/<task_id>.json。"""
        from herdr.direct_dispatch import gate_verdict_path
        from herdr.task_checkpoint import record_business_acceptance

        task_id = "wf-test-task-1"
        run_id = "run-test-1"
        epoch = "epoch-12345"
        candidate_sha = "1" * 40
        execution_id = "exec-test-1"

        store_mock = MagicMock()
        store_mock.db_path = str(Path(self.tmp_dir.name) / "state.db")

        # Mock sqlite reader and writer
        mock_task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "execution_id": execution_id,
            "candidate_sha": candidate_sha,
            "status": "working",
            "run_id": run_id,
            "completion_epoch": epoch,
            "agent": "test-agent",
            "execution_token": "token-xyz",
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

            record_business_acceptance(
                store_mock,
                task_id,
                run_id,
                epoch,
                candidate_sha,
                "pass",
                [{"observation_id": "obs-1", "sha256": "a" * 64, "size_bytes": 100}],
                [{"id": "AC-1", "status": "pass"}],
            )

            # 验证 verdict 文件已自动落盘
            vpath = Path(gate_verdict_path(task_id))
            self.assertTrue(vpath.exists(), f"Verdict file should exist at {vpath}")
            data = json.loads(vpath.read_text(encoding="utf-8"))
            self.assertEqual(data["verdict"], "pass")
            self.assertEqual(data["candidate_sha"], candidate_sha)
            self.assertEqual(data["task_id"], task_id)
            self.assertEqual(data["run_id"], run_id)
            self.assertEqual(data["epoch"], epoch)
            self.assertEqual(data["execution_token"], "token-xyz")
            self.assertEqual(data["verifier_agent"], "test-agent")

    # -------------------------------------------------------------------------
    # P3: finalize 针对重放与更迭场景传 --allow-rebase 测试
    # -------------------------------------------------------------------------
    def test_p3_should_allow_rebase_for_replay_and_supersede(self):
        """P3: software domain adapter 识别 supersedes, onto_branch, is_replay, dispatch_round > 1 为合法 rebase 场景。"""
        adapter = get_domain_adapter("software")

        # 1. 显式 flag
        self.assertTrue(adapter.should_allow_rebase({}, explicit_flag=True))

        # 2. task 显式 allow_rebase
        self.assertTrue(adapter.should_allow_rebase({"allow_rebase": True}))
        self.assertFalse(adapter.should_allow_rebase({"allow_rebase": False}))

        # 3. onto / onto_branch
        self.assertTrue(adapter.should_allow_rebase({"onto": "dev"}))
        self.assertTrue(adapter.should_allow_rebase({"onto_branch": "origin/dev"}))

        # 4. supersedes 更迭场景
        self.assertTrue(adapter.should_allow_rebase({"supersedes": "wf-impl-craft-r7"}))

        # 5. dispatch_round > 1 重放轮次
        self.assertTrue(adapter.should_allow_rebase({"dispatch_round": 2}))
        self.assertTrue(adapter.should_allow_rebase({"dispatch_round": 8}))
        self.assertFalse(adapter.should_allow_rebase({"dispatch_round": 1}))

        # 6. is_replay 标记
        self.assertTrue(adapter.should_allow_rebase({"is_replay": True}))

    def test_p3_git_finalize_passes_allow_rebase_on_replay(self):
        """P3: git_finalize 在重放/更迭场景调用 TASK_MANAGER commit 时带上 --allow-rebase。"""
        from services.controller import git_finalize

        task_id = "wf-impl-craft-r8"
        mock_task = {
            "task_id": task_id,
            "status": "completed",
            "integration_mode": "git",
            "dispatch_round": 8,
            "supersedes": "wf-impl-craft-r7",
        }

        mock_subprocess_run = MagicMock()
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = json.dumps({"task_id": task_id, "result": "committed"})

        class MockFinalizePorts:
            TASK_MANAGER = "herdr-task"
            subprocess = MagicMock()
            subprocess.run = mock_subprocess_run
            os = os
            get_task = MagicMock(return_value=mock_task)
            _parse_commit_result = staticmethod(lambda stdout: json.loads(stdout) if stdout else {})
            _record_finalize_event = MagicMock()
            maybe_complete_on_task_done = staticmethod(lambda tid: [])
            ensure_no_git_processes = None
            set_task_status = MagicMock(return_value=True)

        # Call finalize commit
        git_finalize.finalize_completed_task(MockFinalizePorts, task_id)

        # Verify TASK_MANAGER commit was called with --allow-rebase
        mock_subprocess_run.assert_called()
        cmd_args = mock_subprocess_run.call_args[0][0]
        self.assertIn("commit", cmd_args)
        self.assertIn("--allow-rebase", cmd_args, f"Expected --allow-rebase in {cmd_args}")


if __name__ == "__main__":
    unittest.main()
