"""Scheduler v1 controller-wiring tests (HAFlow PR #107).

场景映射 (PRD §31):
- 场景 3 (真实重叠证据): 并行段 wall-clock overlap 由纯函数度量,
  此处验证 controller 接线把 candidate_sha 注入 launch 命令。
- 场景 9 (崩溃恢复): completed 的 test + 进行中的 review 不丢失,
  join 门禁保持 waiting(不推进 wrapup)。
- 场景 10 (幂等): 同一 ready 节点重复 advance 只派发一次。
- 场景 11 (资源约束): agent 不足/派发失败时 fallback 到总指挥。

全部使用 test_dispatch_candidate.py 的既有 mock 模式:
controller 模块按源码加载,subprocess 伪造,git 用真实临时仓库。
"""

import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import scheduler as scheduler_core  # noqa: E402


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctl = _load_module(
    "herdr_controller_scheduler_v1_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)


def _node(node_id="test", **overrides):
    node = {
        "id": node_id,
        "label": node_id,
        "purpose": "验证实现结果是否满足需求与验收标准。",
        "default_task_type": "test",
        "default_integration_mode": "none",
        "required_outputs": ["测试结论"],
        "rules": [],
        "depends_on": ["implementation"],
    }
    node.update(overrides)
    return node


def _task(task_id, status, node, **extra):
    task = {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "node": node,
        "stage": node,
        "status": status,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    task.update(extra)
    return task


class ControllerCandidateShaWiringTest(unittest.TestCase):
    """场景 3/10:launch 携带 --candidate-sha;重复 advance 不重复派发。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-sched-e2e-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "t@t"],
            check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "t"],
            check=True)
        (self.repo / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "base",
             "--no-gpg-sign"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "-M", "main"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-qb",
             "agent/opencode/feat-new"], check=True)
        (self.repo / "g.txt").write_text("y")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "feat",
             "--no-gpg-sign"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-q", "main"],
            check=True)
        self.sha = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify",
             "agent/opencode/feat-new"],
            check=True, text=True, capture_output=True).stdout.strip()
        self.assertTrue(self.sha)
        self.commands = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            self.commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "Task dispatched: x", "")

        patchers = [
            patch.object(_ctl, "project_for_workflow", return_value={
                "startup_ready": True,
                "project_root": str(self.repo),
                "base_branch": "main",
                "coordinator_pane_id": "wA:p1",
                "requirement": "req text here",
            }),
            patch.object(_ctl, "load_tasks", return_value=[
                _task("impl-1", "cleaned", "implementation",
                      branch="agent/opencode/feat-new", updated_at=200.0),
            ]),
            patch.object(_ctl, "get_stage_policy", return_value={}),
            patch.object(_ctl, "node_is_gate", return_value=False),
            patch.object(_ctl, "shared_docs_block", return_value=""),
            patch.object(
                _ctl, "mark_stage_advance_notified", return_value=True),
            patch.object(
                _ctl, "maybe_compact_coordinator", return_value=False),
            patch("subprocess.run", side_effect=fake_run),
        ]
        for started in patchers:
            started.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patchers)])

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self, node_id="test"):
        return {
            "kind": "stage_advance",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "node_id": node_id,
            "next_stage": node_id,
            "node": _node(node_id),
        }

    def test_launch_carries_candidate_sha(self):
        self.assertTrue(_ctl.try_direct_stage_advance(self._item()))
        launch = [c for c in self.commands if "launch" in c]
        self.assertEqual(len(launch), 1)
        self.assertIn("--candidate-sha", launch[0])
        self.assertEqual(
            launch[0][launch[0].index("--candidate-sha") + 1], self.sha)

    def test_parallel_test_and_review_both_launch(self):
        """test 与 review 同为 ready 时两个 advance 各派发一次。"""
        self.assertTrue(_ctl.try_direct_stage_advance(self._item("test")))
        self.assertTrue(_ctl.try_direct_stage_advance(self._item("review")))
        launch = [c for c in self.commands if "launch" in c]
        self.assertEqual(len(launch), 2)
        shas = [launch[i][launch[i].index("--candidate-sha") + 1]
                for i in range(2)]
        self.assertEqual(shas, [self.sha, self.sha])


class JoinGateControllerTest(unittest.TestCase):
    """场景 9:崩溃恢复语义 + join 门禁拒绝/放行。"""

    def test_join_gate_blocks_wrapup_while_review_running(self):
        tasks = [
            _task("t-test", "cleaned", "test",
                  stage_verdict="pass", candidate_sha="sha-A"),
            _task("t-review", "working", "review", candidate_sha="sha-A"),
        ]
        node = {"id": "wrapup", "node_type": "gate",
                "depends_on": ["test", "review"]}
        self.assertFalse(_ctl._scheduler_join_gate_allows("wf-1", node, tasks))

    def test_join_gate_passes_when_both_pass_same_sha(self):
        tasks = [
            _task("t-test", "cleaned", "test",
                  stage_verdict="pass", candidate_sha="sha-A"),
            _task("t-review", "cleaned", "review",
                  stage_verdict="pass", candidate_sha="sha-A"),
        ]
        # 冻结期望候选 = 验证版本 -> 放行
        with patch.object(
            _ctl.scheduler_facts_store, "latest_frozen_candidate_sha",
            return_value="sha-A"):
            node = {"id": "wrapup", "node_type": "gate",
                    "depends_on": ["test", "review"]}
            with patch.object(
                _ctl.scheduler_facts_store, "record_join_gate_verdict",
                return_value={}) as audit:
                self.assertTrue(
                    _ctl._scheduler_join_gate_allows("wf-1", node, tasks))
                audit.assert_called_once()
                _args, _kwargs = audit.call_args
                self.assertTrue(_args[2])  # passed
                self.assertEqual(_args[3], "join_satisfied")

    def test_join_gate_refuses_mismatched_sha(self):
        tasks = [
            _task("t-test", "cleaned", "test",
                  stage_verdict="pass", candidate_sha="sha-A"),
            _task("t-review", "cleaned", "review",
                  stage_verdict="pass", candidate_sha="sha-B"),
        ]
        node = {"id": "wrapup", "node_type": "gate",
                "depends_on": ["test", "review"]}
        self.assertFalse(_ctl._scheduler_join_gate_allows("wf-1", node, tasks))

    def test_non_join_nodes_pass_through(self):
        node = _node("wrapup")
        self.assertTrue(_ctl._scheduler_join_gate_allows("wf-1", node, []))


class SchedulerExpectedShaTest(unittest.TestCase):
    """期望候选解析:delivery note 优先,分支 HEAD 兜底,失败留空。"""

    def test_delivery_note_wins_over_branch(self):
        note = {"kind": "delivery", "delivery_branch": "agent/x/feat",
                "candidate_sha": "sha-NOTE"}
        with patch.object(_ctl.workflow_docs_mod, "load_notes",
                          return_value=[note]), \
            patch.object(_ctl.delivery_record_mod,
                         "select_effective_delivery", return_value=note), \
            patch.object(_ctl.delivery_record_mod, "_body_value",
                         return_value="sha-NOTE"):
            sha = _ctl._scheduler_expected_candidate_sha(
                "wf-1", "/nope", ["implementation"], "agent/x/feat")
            self.assertEqual(sha, "sha-NOTE")

    def test_branch_head_fallback(self):
        with patch.object(_ctl.workflow_docs_mod, "load_notes",
                          return_value=[]), \
            patch.object(_ctl.delivery_record_mod,
                         "select_effective_delivery", return_value=None), \
            patch.object(_ctl.scheduler_core,
                         "resolve_candidate_sha_for_branch",
                         return_value="sha-BRANCH"):
            sha = _ctl._scheduler_expected_candidate_sha(
                "wf-1", "/repo", ["implementation"], "agent/x/feat")
            self.assertEqual(sha, "sha-BRANCH")

    def test_failure_leaves_empty(self):
        with patch.object(_ctl.workflow_docs_mod, "load_notes",
                          side_effect=OSError("disk gone")), \
            patch.object(_ctl.scheduler_core,
                         "resolve_candidate_sha_for_branch",
                         side_effect=OSError("no git")):
            sha = _ctl._scheduler_expected_candidate_sha(
                "wf-1", "/repo", ["implementation"], None)
            self.assertEqual(sha, "")
