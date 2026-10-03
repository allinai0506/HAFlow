"""Dispatch-candidate recovery tests (P1).

r6 tested main instead of the feature branch (vacant blocked verdict),
and fix outputs never landed on any branch. Covers:
- planner carries a sanitized onto_branch into specs (pure);
- controller passes --onto to launch, and refuses vacuous candidates
  (fail-open on git errors);
- supersede auto-saves clone WIP to the task branch (best-effort).
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import direct_dispatch as planner


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctl = _load_module(
    "herdr_controller_dispatch_candidate_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)
_ht = _load_module(
    "herdr_task_dispatch_candidate_test",
    HERDR_ROOT / "bin" / "herdr-task",
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


class PlannerOntoTest(unittest.TestCase):
    def test_redispatch_carries_onto_branch(self):
        old = _task(
            "wf-1-test-auto", "superseded", "test",
            goal="g", acceptance_criteria=["a"],
            integration_mode="none",
        )
        plan = planner.plan_stage_dispatch(
            "wf-1", _node(), [old], "req",
            context_branch="agent/opencode/feat-x",
        )
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(
            plan["specs"][0]["onto_branch"], "agent/opencode/feat-x"
        )

    def test_initial_dispatch_carries_onto_branch(self):
        plan = planner.plan_stage_dispatch(
            "wf-1", _node(), [], "req",
            context_branch="agent/opencode/feat-x",
        )
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(
            plan["specs"][0]["onto_branch"], "agent/opencode/feat-x"
        )

    def test_invalid_branches_filtered(self):
        for bad in ["", None, "  ", "../evil", "has space",
                    "-rf", "/abs/path", "a~b", "a^b", "a:b"]:
            plan = planner.plan_stage_dispatch(
                "wf-1", _node(), [], "req", context_branch=bad,
            )
            self.assertEqual(plan["mode"], "dispatch")
            self.assertIsNone(plan["specs"][0].get("onto_branch"))

    def test_candidate_branch_prefers_dependencies(self):
        """优先取依赖节点的分支，而非本节点自己的旧任务分支。

        （此处 `delivered_in_base=False`：只验证"选哪条谱系"，
        "已交付是否该跳过"由 DeliveredDependencyOntoTest 覆盖。）
        """
        tasks = [
            _task("impl-1", "cleaned", "implementation",
                  branch="agent/opencode/feat-new", updated_at=200.0),
            _task("t-old", "cleaned", "test",
                  branch="agent/codex/stale", updated_at=300.0),
        ]
        self.assertEqual(
            planner.candidate_branch_for_node(
                tasks, "wf-1", "test", ["implementation"],
                delivered_in_base=False,
            ),
            "agent/opencode/feat-new",
        )

    def test_candidate_excludes_superseded_tasks(self):
        tasks = [
            _task("impl", "integrated", "implementation", branch="agent/x/task", integration_ref="refs/herdr/tasks/impl", updated_at=200),
            _task("old", "superseded", "implementation", branch="agent/x/old", updated_at=300),
        ]
        self.assertEqual(planner.candidate_branch_for_node(tasks, "wf-1", "test", ["implementation"], delivered_in_base=True), "agent/x/task")

    def test_candidate_branch_falls_back_to_own_node(self):
        tasks = [
            _task("impl-1", "cleaned", "implementation", updated_at=200.0),
        ]
        self.assertIsNone(
            planner.candidate_branch_for_node(
                tasks, "wf-1", "test", ["implementation"]
            )
        )


class DeliveredDependencyOntoTest(unittest.TestCase):
    """交付物已合入 base 的依赖，**不能**再把任务分支当 onto。

    实测事故（wf-project-0929-01）：test 节点反复
    `[DIRECT DISPATCH ERROR] Onto branch not found on origin:
    agent/opencode/feat-impl-t7-integration-gates-r2`，任务在 `pending` 阶段就被
    判 `router_isolation_rejected`，从未真正执行（r1/r5 都是这样死的）。

    两个事实叠加导致：
      1. `herdr-task launch` 要求 `--onto` 分支存在于
         `refs/remotes/origin/`（fix-loop 续接既有 PR 分支的约束）；
      2. 该工作流的 9 个 `integration_mode=git` 任务**全部** `cleaned`
         —— 交付物早已合入 base `agent/gemini-init`，而任务分支从未推送。

    于是 onto 指向一个 origin 上不存在的本地分支，launch 必然 `exit 2`。
    同一工作流里手工 `herdr-task launch`（不带 `--onto`）的
    `test-...-r6` 成功落地，且 `baseline_commit=5d3d615e07ab` 正是合入 base
    的交付点 —— 证明"落在 base 上测"才是正确形态。
    """

    def _impl(self, task_id, status, branch, **extra):
        return _task(task_id, status, "implementation", branch=branch,
                     integration_mode="git", **extra)

    def test_all_dependencies_delivered_yields_no_onto(self):
        tasks = [
            self._impl("impl-a", "cleaned", "agent/opencode/feat-a", updated_at=300.0),
            self._impl("impl-b", "integrated", "agent/opencode/feat-b", updated_at=200.0),
        ]
        self.assertIsNone(
            planner.candidate_branch_for_node(
                tasks, "wf-1", "test", ["implementation"], delivered_in_base=True
            ),
            "交付物已在 base 上时必须不设 onto，否则 launch 必然拒绝",
        )

    def test_undelivered_dependency_still_uses_task_branch(self):
        """交付物尚未合入 base 时，任务分支仍是唯一载体（fix-loop 续接）。"""
        tasks = [
            self._impl("impl-done", "cleaned", "agent/opencode/feat-done", updated_at=200.0),
            self._impl("impl-live", "working", "agent/opencode/feat-live", updated_at=300.0),
        ]
        self.assertEqual(
            planner.candidate_branch_for_node(
                tasks, "wf-1", "test", ["implementation"], delivered_in_base=True
            ),
            "agent/opencode/feat-live",
        )

    def test_does_not_fall_back_to_own_node_branch(self):
        """依赖已交付时**不得**回退到本节点分支。

        `candidate_branch_for_node` 在依赖无候选时会回退到本节点（为了"依赖无
        分支时仍能派发"）。但在 delivered_in_base 模式下这个回退是错的：本节点
        自己的任务分支（如 test 节点的 agent/pi/test-...-r6）同样从未推送，
        launch 仍会 `Onto branch not found on origin` 拒绝派发。

        实测：修复前 onto = agent/opencode/feat-impl-t7-integration-gates-r2，
        只加"跳过已交付依赖"后变成 agent/pi/test-...-r6，两者都 exit 2。
        正确答案是 None —— 任务落在 base（source HEAD）上测候选交付物。
        """
        tasks = [
            self._impl("impl-a", "cleaned", "agent/opencode/feat-a", updated_at=200.0),
            _task("t-r4", "rework", "test",
                  branch="agent/agy/test-test-unified-task-workbench-v1-r4",
                  updated_at=250.0),
            _task("t-r6", "working", "test",
                  branch="agent/pi/test-test-unified-task-workbench-v1-r6",
                  updated_at=300.0),
        ]
        self.assertIsNone(
            planner.candidate_branch_for_node(
                tasks, "wf-1", "test", ["implementation"], delivered_in_base=True
            ),
            "依赖已交付时应回落到 base(None)，不是本节点任务分支",
        )

    def test_legacy_call_without_flag_keeps_old_behavior(self):
        """未显式告知"交付已合入 base"时保持原行为，不静默改变既有调用方。"""
        tasks = [self._impl("impl-a", "cleaned", "agent/opencode/feat-a", updated_at=200.0)]
        self.assertEqual(
            planner.candidate_branch_for_node(tasks, "wf-1", "test", ["implementation"]),
            "agent/opencode/feat-a",
        )

    def test_delivered_filter_covers_every_git_delivered_status(self):
        for status in ("integrated", "cleanup_ready", "cleaned"):
            with self.subTest(status=status):
                tasks = [self._impl("impl-a", status, "agent/opencode/feat-a", updated_at=200.0)]
                self.assertIsNone(
                    planner.candidate_branch_for_node(
                        tasks, "wf-1", "test", ["implementation"], delivered_in_base=True
                    ),
                    f"{status} 属于已交付状态，应跳过任务分支",
                )


class ControllerOntoWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-cand-")
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

    def _item(self):
        return {
            "kind": "stage_advance",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "node_id": "test",
            "next_stage": "test",
            "node": _node(),
        }

    def test_launch_receives_frozen_sha_without_shared_dependency_branch(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-qb", "agent/opencode/feat-new"],
            check=True)
        (self.repo / "g.txt").write_text("y")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "feat",
             "--no-gpg-sign"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-q", "main"],
            check=True)
        self.assertTrue(_ctl.try_direct_stage_advance(self._item()))
        launch = [c for c in self.commands if "launch" in c]
        self.assertEqual(len(launch), 1)
        self.assertNotIn("--onto", launch[0])
        self.assertIn("--candidate-sha", launch[0])
        self.assertEqual(launch[0][launch[0].index("--candidate-sha") + 1],
                         subprocess.check_output(["git", "-C", str(self.repo), "rev-parse",
                                                  "agent/opencode/feat-new"], text=True).strip())

    def test_vacuous_candidate_falls_back_without_launch(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-qb", "agent/opencode/feat-empty"],
            check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-q", "main"],
            check=True)
        with patch.object(
            _ctl, "load_tasks", return_value=[
                _task("impl-1", "cleaned", "implementation",
                      branch="agent/opencode/feat-empty",
                      updated_at=200.0),
            ]):
            self.assertFalse(_ctl.try_direct_stage_advance(self._item()))
        self.assertEqual(
            [c for c in self.commands if "launch" in c], [])

    def test_git_failure_fails_open_to_launch(self):
        with patch.object(
            _ctl, "project_for_workflow", return_value={
                "startup_ready": True,
                "project_root": str(self.root / "no-such-repo"),
                "base_branch": "main",
                "coordinator_pane_id": "wA:p1",
                "requirement": "req text here",
            }):
            self.assertTrue(_ctl.try_direct_stage_advance(self._item()))
        self.assertEqual(len([c for c in self.commands if "launch" in c]), 1)


class SupersedeAutosaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-wip-")
        self.clone = Path(self.tmp.name) / "clone"
        self.clone.mkdir()
        subprocess.run(["git", "init", "-q", str(self.clone)], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "config", "user.email", "t@t"],
            check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "config", "user.name", "t"],
            check=True)
        (self.clone / "a.py").write_text("v1")
        subprocess.run(["git", "-C", str(self.clone), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "commit", "-qm", "base",
             "--no-gpg-sign"], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "checkout", "-qb", "agent/task"],
            check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self):
        return {
            "task_id": "t-fix",
            "branch": "agent/task",
            "clone_path": str(self.clone),
        }

    def test_wip_committed_without_internals(self):
        (self.clone / "a.py").write_text("v2")
        (self.clone / ".herdr-loop").mkdir()
        (self.clone / ".herdr-loop" / "STATE.md").write_text("x")
        result = _ht._autosave_clone_wip(self._task())
        self.assertTrue(result)
        log = subprocess.run(
            ["git", "-C", str(self.clone), "log", "--oneline", "-1"],
            text=True, capture_output=True, check=True)
        self.assertIn("t-fix", log.stdout)
        show = subprocess.run(
            ["git", "-C", str(self.clone), "show", "--name-only",
             "--format=", "HEAD"],
            text=True, capture_output=True, check=True)
        self.assertIn("a.py", show.stdout)
        self.assertNotIn(".herdr-loop", show.stdout)

    def test_clean_tree_no_commit(self):
        self.assertFalse(_ht._autosave_clone_wip(self._task()))

    def test_missing_clone_is_silent_noop(self):
        self.assertFalse(_ht._autosave_clone_wip({"task_id": "t"}))
        self.assertFalse(_ht._autosave_clone_wip(
            {"task_id": "t", "branch": "b",
             "clone_path": str(self.clone) + "-missing"}))


if __name__ == "__main__":
    unittest.main()
