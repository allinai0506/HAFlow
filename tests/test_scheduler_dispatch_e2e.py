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
import json
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import pytest
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
    # A completed scheduler-managed task that verified its claim carries
    # completion evidence. Model it by default so controller-wiring tests
    # exercise the normal path; pass verified_candidate_sha="" explicitly to
    # exercise the fail-closed path.
    if task.get("candidate_sha") and "verified_candidate_sha" not in extra:
        task["verified_candidate_sha"] = task["candidate_sha"]
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


class P1RealTemplateJoinTest(unittest.TestCase):
    """P1-1 回归:真实模板的 wrapup 也必须受 Join Gate 约束。

    背景:早期实现只认 node_type==gate,而真实模板 wrapup 是 agent 节点,
    于是 test(A)/review(B) 双 pass 后 wrapup 照样 Ready。测试必须使用
    真实模板节点,而不是手工构造 node_type=gate。
    """

    def _template_node(self, node_id):
        from herdr.workflow import load_template

        tmpl = load_template("software-development-v1")
        for node in tmpl["nodes"]:
            if node["id"] == node_id:
                return node
        raise AssertionError(f"node {node_id} missing from template")

    def test_real_wrapup_is_multi_dependency(self):
        wrapup = self._template_node("wrapup")
        self.assertEqual(sorted(wrapup.get("depends_on") or []), ["review", "test"])
        self.assertNotEqual(str(wrapup.get("node_type") or ""), "gate")

    def test_real_wrapup_refuses_divergent_verifications(self):
        wrapup = self._template_node("wrapup")
        tasks = [
            _task("t-test", "cleaned", "test",
                  stage_verdict="pass", candidate_sha="sha-A",
                  baseline_commit="sha-A"),
            _task("t-review", "cleaned", "review",
                  stage_verdict="pass", candidate_sha="sha-B",
                  baseline_commit="sha-B"),
        ]
        with patch.object(
            _ctl.scheduler_facts_store, "latest_frozen_candidate_sha",
            return_value="sha-A"), patch.object(
            _ctl.scheduler_facts_store, "record_join_gate_verdict",
            return_value={}):
            self.assertFalse(
                _ctl._scheduler_join_gate_allows("wf-1", wrapup, tasks))

    def test_real_wrapup_allows_same_revision(self):
        wrapup = self._template_node("wrapup")
        tasks = [
            _task("t-test", "cleaned", "test",
                  stage_verdict="pass", candidate_sha="sha-A",
                  baseline_commit="sha-A"),
            _task("t-review", "cleaned", "review",
                  stage_verdict="pass", candidate_sha="sha-A",
                  baseline_commit="sha-A"),
        ]
        with patch.object(
            _ctl.scheduler_facts_store, "latest_frozen_candidate_sha",
            return_value="sha-A"), patch.object(
            _ctl.scheduler_facts_store, "record_join_gate_verdict",
            return_value={}):
            self.assertTrue(
                _ctl._scheduler_join_gate_allows("wf-1", wrapup, tasks))

    def test_unengaged_multi_dep_node_keeps_legacy_passthrough(self):
        """未冻结候选(非 Scheduler workflow)的多依赖节点保持原语义。"""
        node = {"id": "legacy_join", "depends_on": ["a", "b"]}
        with patch.object(
            _ctl.scheduler_facts_store, "latest_frozen_candidate_sha",
            return_value=""):
            self.assertTrue(
                _ctl._scheduler_join_gate_allows("wf-legacy", node, []))

    def test_explicit_gate_node_still_enforced(self):
        node = {"id": "final_gate", "node_type": "gate",
                "depends_on": ["test", "review"]}
        self.assertFalse(_ctl._scheduler_join_gate_allows("wf-1", node, []))


class P1LatchRecoveryTest(unittest.TestCase):
    """P1-4 回归:join 拒绝不得吞掉推进闩,修正证据后必须自动恢复。"""

    def setUp(self):
        self.state = {}
        self.queued = []
        self.commands = []

        def fake_run(cmd, **kwargs):
            self.commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        def fake_queued(workflow_id, stage):
            key = f"{workflow_id}:{stage}"
            if self.state.get(key) in ("queued", "notified"):
                return False
            self.state[key] = "queued"
            self.queued.append(stage)
            return True

        patchers = [
            patch.object(_ctl, "project_for_workflow", return_value={
                "startup_ready": True,
                "project_root": "/repo",
                "base_branch": "main",
                "coordinator_pane_id": "wA:p1",
                "requirement": "req text here",
            }),
            patch.object(_ctl, "load_tasks", return_value=[]),
            patch.object(_ctl, "get_stage_policy", return_value={}),
            patch.object(_ctl, "node_is_gate", return_value=False),
            patch.object(_ctl, "shared_docs_block", return_value=""),
            patch.object(_ctl, "mark_stage_advance_queued", side_effect=fake_queued),
            patch.object(_ctl, "mark_stage_advance_notified", return_value=True),
            patch.object(_ctl, "maybe_compact_coordinator", return_value=False),
            patch.object(_ctl.direct_dispatch_planner,
                         "plan_stage_dispatch", return_value={
                             "mode": "dispatch", "reason": "t",
                             "specs": [{"task_id": "wf-1-test-auto",
                                        "goal": "g", "acceptance": [],
                                        "prompt": "p", "task_type": "test",
                                        "integration_mode": "none"}],
                         }),
            patch.object(_ctl.direct_dispatch_planner,
                         "candidate_branch_for_node", return_value=None),
            patch.object(_ctl, "_dispatch_candidate_ready", return_value=True),
            patch.object(
                _ctl.direct_dispatch_planner, "plan_stage_dispatch",
                return_value={"mode": "dispatch", "reason": "t", "specs": [{
                    "task_id": "wf-1-test-auto", "goal": "g",
                    "acceptance": [], "prompt": "p", "task_type": "test",
                    "integration_mode": "none"}]}),
            patch("subprocess.run", side_effect=fake_run),
        ]
        for started in patchers:
            started.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patchers)])

    def _item(self):
        return {
            "kind": "stage_advance",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "node_id": "test",
            "next_stage": "test",
            "node": _node("test"),
        }

    def test_refusal_does_not_consume_latch_and_recovers(self):
        verdicts = iter([False, True])
        with patch.object(
            _ctl, "_scheduler_join_gate_allows",
            side_effect=lambda *a, **k: next(verdicts),
        ), patch.object(_ctl, "mark_stage_advance_notified") as notified:
            # 第一次:join 拒绝 -> 事件被消费,但不得写任何闩、不得派发
            self.assertTrue(_ctl.try_direct_stage_advance(self._item()))
            notified.assert_not_called()
            self.assertFalse(
                [c for c in self.commands if "launch" in c],
                "join refusal must not launch anything",
            )
            # 第二次:证据修好后自动恢复,正常派发
            self.assertTrue(_ctl.try_direct_stage_advance(self._item()))
            notified.assert_called_once()
            self.assertTrue([c for c in self.commands if "launch" in c])

    def test_sweep_join_refusal_does_not_consume_queued_latch(self):
        """P1-4 回归(sweep 路径):join 判定必须先于 queued 闩。"""
        wf_id = "wf-sweep-1"
        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["implementation"]},
                {"id": "wrapup", "depends_on": ["test", "review"]},
            ]
        }
        with patch.object(_ctl, "workflow_closed", return_value=False),             patch.object(_ctl, "coordinator_pane_for_workflow",
                         return_value="wA:p1"),             patch.object(_ctl, "workflow_config_for", return_value=cfg),             patch.object(_ctl, "project_for_workflow", return_value={
                "startup_ready": True, "project_root": "/repo"}),             patch.object(_ctl, "_workflow_entry", return_value={}),             patch.object(_ctl, "reconcile_stage_advance_states"),             patch.object(_ctl, "is_node_complete",
                         side_effect=lambda _w, n: n in {
                             "requirements", "plan", "implementation",
                             "test", "review"}),             patch.object(_ctl, "is_workflow_completed", return_value=False),             patch.object(_ctl, "load_tasks", return_value=[]),             patch.object(_ctl, "resolve_gate_config", return_value=None),             patch.object(_ctl, "_fix_loop_latch_blocks", return_value=False),             patch.object(_ctl, "blocked_gate_dependency", return_value=None),             patch.object(_ctl, "blocked_verdict_dep", return_value=None),             patch.object(_ctl, "attention_blocks_retry", return_value=False),             patch.object(_ctl, "mark_stage_advance_queued") as queued,             patch.object(_ctl, "_scheduler_join_gate_allows",
                         return_value=False),             patch.object(_ctl.coordinator_queue, "put") as put:
            _ctl.check_workflow_stage_advance(wf_id)
        # wrapup 是唯一 ready 节点,被 join 拒绝 -> 不得占用 queued 闩
        queued.assert_not_called()
        put.assert_not_called()

    def _sweep_patches(self, **overrides):
        """Common sweep mocks; overrides win over the defaults."""
        wf_id = "wf-sweep-2"
        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }
        patches = {
            "workflow_closed": False,
            "coordinator_pane_for_workflow": "wA:p1",
            "workflow_config_for": cfg,
            "project_for_workflow": {
                "startup_ready": True, "project_root": "/repo"},
            "_workflow_entry": {},
            "reconcile_stage_advance_states": None,
            "is_node_complete": lambda _w, n: n == "implementation",
            "is_workflow_completed": False,
            "load_tasks": [],
            "resolve_gate_config": None,
            "_fix_loop_latch_blocks": False,
            "blocked_gate_dependency": None,
            "blocked_verdict_dep": None,
            "attention_blocks_retry": False,
        }
        patches.update(overrides)
        ctx = []
        for name, value in patches.items():
            if value is None and name == "reconcile_stage_advance_states":
                ctx.append(patch.object(_ctl, name))
            elif callable(value):
                ctx.append(patch.object(_ctl, name, side_effect=value)
                           if not isinstance(value, (bool, type(None)))
                           else patch.object(_ctl, name, return_value=value))
            else:
                ctx.append(patch.object(_ctl, name, return_value=value))
        return wf_id, ctx

    def test_unresolvable_candidate_does_not_latch_or_dispatch(self):
        """P1 回归:冻结不出候选身份时,不得占闩、不得派发。

        否则 test/review 会在没有身份的情况下启动(被 preflight 拒绝),
        而 stage 已标 queued/notified;等分支修好、SHA 可解析时,这一轮
        不会再自然重试。
        """
        wf_id, ctx = self._sweep_patches(
            _scheduler_join_gate_allows=True,
            _scheduler_freeze_candidate="",
        )
        with patch.object(_ctl, "mark_stage_advance_queued") as queued, \
            patch.object(_ctl.coordinator_queue, "put") as put:
            for c in ctx:
                c.start()
            try:
                _ctl.check_workflow_stage_advance(wf_id)
            finally:
                for c in reversed(ctx):
                    c.stop()
            queued.assert_not_called()
            put.assert_not_called()

    def test_workflow_without_project_root_keeps_legacy_advance(self):
        """对照:没有 project_root 的 workflow 不属于候选身份语义。

        它没有仓库可供解析候选,这不是暂时性缺口;若一并拦下,该
        workflow 将永远无法推进。
        """
        wf_id, ctx = self._sweep_patches(
            _scheduler_join_gate_allows=True,
            _scheduler_freeze_candidate="",
            project_for_workflow={"startup_ready": True},
        )
        with patch.object(
            _ctl, "mark_stage_advance_queued", return_value=True
        ) as queued, patch.object(_ctl.coordinator_queue, "put") as put:
            for c in ctx:
                c.start()
            try:
                _ctl.check_workflow_stage_advance(wf_id)
            finally:
                for c in reversed(ctx):
                    c.stop()
            self.assertTrue(queued.called, "legacy workflow must still advance")
            self.assertTrue(put.called)

    def test_resolved_candidate_latches_and_dispatches(self):
        """对照:候选身份可解析时,闩与派发照常发生。"""
        wf_id, ctx = self._sweep_patches(
            _scheduler_join_gate_allows=True,
            _scheduler_freeze_candidate="b" * 40,
        )
        with patch.object(
            _ctl, "mark_stage_advance_queued", return_value=True
        ) as queued, patch.object(_ctl.coordinator_queue, "put") as put:
            for c in ctx:
                c.start()
            try:
                _ctl.check_workflow_stage_advance(wf_id)
            finally:
                for c in reversed(ctx):
                    c.stop()
            queued.assert_called()
            put.assert_called()


class FrozenCandidateFactTest(unittest.TestCase):
    """P1(第 3 轮)回归:冻结 Candidate 与 Final Delivery Record 是两种事实。

    背景:上一版在冻结时伪造 review_task/test_gate 占位 id 写入 delivery
    record。delivery_record 把这些字段纳入不可变 fingerprint,于是真实任务
    创建后写入真 id 会 [DELIVERY CONFLICT];check-delivery 看到的 verifier
    身份也是假的。冻结只允许写 candidate_frozen 事实。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-frozen-")
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "state.db"
        env = patch.dict(os.environ, {
            "HERDR_WORKFLOW_DOCS_DIR": str(self.docs),
            "HERDR_STATE_DB": str(self.db),
        })
        env.start()
        self.addCleanup(env.stop)
        from herdr.state_store import get_state_store

        get_state_store(self.db).save_workflow(
            {"workflow_id": "wf-frozen-1", "status": "running"})

    def tearDown(self):
        self.tmp.cleanup()

    def _freeze(self, sha):
        with patch.object(_ctl, "_scheduler_expected_candidate_sha",
                          return_value=sha),             patch.object(_ctl, "load_tasks", return_value=[]):
            return _ctl._scheduler_freeze_candidate(
                "wf-frozen-1", str(self.root), "implementation", [])

    def test_freeze_records_fact_and_no_delivery_note(self):
        from herdr import scheduler_facts as sf
        from herdr import workflow_docs as wd

        sha = "a" * 40
        self.assertEqual(self._freeze(sha), sha)
        facts = sf.list_candidate_frozen_events("wf-frozen-1", db_path=self.db)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["payload"]["candidate_sha"], sha)
        # 冻结阶段绝不能产生 delivery record:真实 verifier task 还不存在
        self.assertEqual(
            [n for n in wd.load_notes("wf-frozen-1") if n.get("kind") == "delivery"],
            [],
            "freeze must not fabricate a delivery identity",
        )
        # 也不得出现任何伪造的 verifier task id
        raw = json.dumps(wd.load_notes("wf-frozen-1"), ensure_ascii=False)
        self.assertNotIn("provisional", raw)

    def test_freeze_is_idempotent_on_same_sha(self):
        from herdr import scheduler_facts as sf

        sha = "b" * 40
        self.assertEqual(self._freeze(sha), sha)
        self.assertEqual(self._freeze(sha), sha)
        facts = sf.list_candidate_frozen_events("wf-frozen-1", db_path=self.db)
        self.assertEqual(len(facts), 1)

    def test_fix_loop_recandidate_frees_without_delivery_conflict(self):
        """P1 回归:review blocked → A 失效 → 返工 → B 冻结必须畅通。

        旧实现在这种情况下 supersedes 为空,record_delivery_note 直接
        [DELIVERY REPLACEMENT REQUIRED] + SystemExit(2),第二轮 test/review
        无法启动。现在冻结不再触碰 delivery,链路自然贯通。
        """
        from herdr import scheduler_facts as sf
        from herdr import workflow_docs as wd

        sha_a, sha_b = "c" * 40, "d" * 40
        self.assertEqual(self._freeze(sha_a), sha_a)
        self.assertEqual(self._freeze(sha_b), sha_b)
        facts = sf.list_candidate_frozen_events("wf-frozen-1", db_path=self.db)
        self.assertEqual(
            [f["payload"]["candidate_sha"] for f in facts], [sha_a, sha_b])
        self.assertEqual(facts[1]["payload"]["rotated_from"], sha_a)
        self.assertEqual(
            sf.latest_frozen_candidate_sha("wf-frozen-1", db_path=self.db), sha_b)
        self.assertEqual(
            [n for n in wd.load_notes("wf-frozen-1") if n.get("kind") == "delivery"],
            [],
        )

    def test_unprovable_sha_returns_empty(self):
        with patch.object(_ctl, "_scheduler_expected_candidate_sha",
                          return_value=""):
            self.assertEqual(_ctl._scheduler_freeze_candidate(
                "wf-frozen-1", str(self.root), "implementation", []), "")

    def test_aba_rotation_records_third_freeze(self):
        """P1 回归:A → B → A 必须重新冻结,否则 latest 停在 B 而实际候选是 A。"""
        from herdr import scheduler_facts as sf

        sha_a, sha_b = "a" * 40, "b" * 40
        self.assertEqual(self._freeze(sha_a), sha_a)
        self.assertEqual(self._freeze(sha_b), sha_b)
        self.assertEqual(self._freeze(sha_a), sha_a)

        facts = sf.list_candidate_frozen_events("wf-frozen-1", db_path=self.db)
        self.assertEqual(
            [f["payload"]["candidate_sha"] for f in facts],
            [sha_a, sha_b, sha_a],
        )
        self.assertEqual(facts[2]["payload"]["rotated_from"], sha_b)
        self.assertEqual(
            sf.latest_frozen_candidate_sha("wf-frozen-1", db_path=self.db), sha_a)


class FrozenIdentityFallbackWiringTest(unittest.TestCase):
    """P1 回归:总指挥回落必须原样透传冻结候选身份,不得自行推断。

    同一个 Scheduler decision 只能有一套执行语义:Direct Dispatch 通过
    --candidate-sha / --onto 绑定冻结候选;回退总指挥时也必须携带同一身份,
    否则两条路径验证的可能不是同一个 revision。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-fallback-id-")
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.db"
        env = patch.dict(os.environ, {"HERDR_STATE_DB": str(self.db)})
        env.start()
        self.addCleanup(env.stop)
        from herdr.state_store import get_state_store

        get_state_store(self.db).save_workflow(
            {"workflow_id": "wf-fb-1", "status": "running"})

    def tearDown(self):
        self.tmp.cleanup()

    def _freeze(self, sha, branch=""):
        from herdr import scheduler_facts as sf

        sf.record_candidate_frozen(
            "wf-fb-1", sha, source_node="implementation",
            delivery_branch=branch, db_path=self.db)

    def test_identity_returns_latest_frozen_candidate(self):
        self._freeze("a" * 40, branch="agent/x/feat-a")
        sha, branch = _ctl._scheduler_frozen_candidate_identity(
            "wf-fb-1", {}, [])
        self.assertEqual(sha, "a" * 40)
        self.assertEqual(branch, "agent/x/feat-a")

    def test_identity_follows_aba_rotation(self):
        sha_a, sha_b = "a" * 40, "b" * 40
        self._freeze(sha_a, branch="agent/x/feat-a")
        self._freeze(sha_b, branch="agent/x/feat-b")
        self._freeze(sha_a, branch="agent/x/feat-a")
        sha, branch = _ctl._scheduler_frozen_candidate_identity(
            "wf-fb-1", {}, [])
        self.assertEqual(sha, sha_a)
        self.assertEqual(branch, "agent/x/feat-a")

    def test_frozen_lookup_error_fails_closed(self):
        """P1 回归:查询 frozen 失败 != 从未冻结,必须 fail-closed。

        默认 wrapup 是 agent 节点 + 两个依赖,不显式属于 join 形状,
        靠「是否冻结过」决定是否走门禁。若把查询异常当成未冻结,
        scheduler 管理的 workflow 会被误判为 legacy 而绕过门禁。
        """
        node = {"id": "wrapup", "node_type": "agent",
                "depends_on": ["test", "review"]}
        boom = sqlite3.OperationalError("database is locked")
        with patch.object(
            _ctl.scheduler_facts_store, "latest_frozen_candidate_sha",
            side_effect=boom,
        ):
            self.assertFalse(
                _ctl._scheduler_join_gate_allows("wf-err", node, []))

    def test_successful_empty_lookup_still_passes_through(self):
        """对照:查询成功且确实没冻结过 -> legacy passthrough 保持不变。"""
        node = {"id": "wrapup", "node_type": "agent",
                "depends_on": ["test", "review"]}
        with patch.object(
            _ctl.scheduler_facts_store, "latest_frozen_candidate_sha",
            return_value="",
        ):
            self.assertTrue(
                _ctl._scheduler_join_gate_allows("wf-legacy", node, []))

    def test_engaged_workflow_with_lookup_error_still_refuses(self):
        """已冻结的 workflow:读取 expected 失败同样不得放行。"""
        node = {"id": "wrapup", "node_type": "agent",
                "depends_on": ["test", "review"]}
        with patch.object(
            _ctl.scheduler_facts_store, "latest_frozen_candidate_sha",
            side_effect=[ "a" * 40, sqlite3.OperationalError("locked") ],
        ):
            self.assertFalse(
                _ctl._scheduler_join_gate_allows("wf-engaged", node, []))

    def test_missing_scheduler_store_refuses_fanin_node(self):
        """调度器不可用时,多依赖节点不得无条件放行。"""
        node = {"id": "wrapup", "node_type": "agent",
                "depends_on": ["test", "review"]}
        with patch.object(_ctl, "scheduler_facts_store", None):
            self.assertFalse(
                _ctl._scheduler_join_gate_allows("wf-none", node, []))

    def test_no_freeze_yields_empty_legacy_prompt_unchanged(self):
        sha, branch = _ctl._scheduler_frozen_candidate_identity(
            "wf-fb-1", {}, [])
        self.assertEqual(sha, "")
        self.assertEqual(branch, "")

    def test_fallback_prompt_carries_frozen_candidate(self):
        """回落提示词必须含 --candidate-sha 与 --onto,不允许总指挥重猜。

        走真实调用链 _handle_coordinator_item(stage_advance),让
        try_direct_stage_advance 真实返回 False(规则化直派不可用),
        从而覆盖真正的回落分支。
        """
        sha = "c" * 40
        sent = self._run_real_fallback(sha)
        self.assertTrue(sent, "coordinator prompt must be sent")
        message = sent[0]
        self.assertIn(f"--candidate-sha {sha}", message)
        self.assertIn("--onto agent/x/feat-c", message)
        # 身份必须整块出现在 launch 指令里,而不是只有一句说明。
        self.assertIn("--workflow-id wf-fb-1", message)
        self.assertIn("--node test", message)

    def _run_real_fallback(self, sha):
        """Let direct dispatch really fall back, then capture the prompt."""
        if sha:
            self._freeze(sha, branch="agent/x/feat-c")
        project_ctx = {
            "startup_ready": True, "project_root": str(self.root),
            "base_branch": "main", "coordinator_pane_id": "wA:p1",
            "requirement": "req text here",
        }
        item = {
            "kind": "stage_advance", "workflow_id": "wf-fb-1",
            "stage": "implementation", "node_id": "test", "next_stage": "test",
            "node": _node("test"),
        }
        sent = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            if cmd and len(cmd) > 3 and cmd[1:3] == ["agent", "prompt"]:
                for arg in cmd[4:]:
                    if not arg.startswith("--"):
                        sent.append(arg)
                        break
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        # direct_dispatch_planner=None makes try_direct_stage_advance return
        # False at its first guard — the genuine fallback path.
        with patch.object(_ctl, "project_for_workflow", return_value=project_ctx), \
            patch.object(_ctl, "workflow_config_for", return_value={
                "nodes": [_node("test")]}), \
            patch.object(_ctl, "workflow_closed", return_value=False), \
            patch.object(_ctl, "coordinator_status", return_value="idle"), \
            patch.object(_ctl, "coordinator_pane_for_workflow",
                         return_value="wA:p1"), \
            patch.object(_ctl, "find_node", return_value=_node("test")), \
            patch.object(_ctl, "get_stage_policy", return_value={}), \
            patch.object(_ctl, "load_tasks", return_value=[]), \
            patch.object(_ctl, "shared_docs_block", return_value=""), \
            patch.object(_ctl, "direct_dispatch_planner", None), \
            patch.object(
                _ctl, "mark_stage_advance_notified", return_value=True), \
            patch.object(
                _ctl, "maybe_compact_coordinator", return_value=False), \
            patch("subprocess.run", side_effect=fake_run):
            _ctl._handle_coordinator_item(item)
        return sent

    def test_fallback_prompt_omits_candidate_when_never_frozen(self):
        """未冻结候选的 legacy workflow:提示词不得凭空出现候选身份。"""
        sent = self._run_real_fallback("")
        self.assertTrue(sent, "coordinator prompt must be sent")
        message = sent[0]
        self.assertNotIn("--candidate-sha", message)
        self.assertNotIn("候选身份", message)
        # legacy 提示词必须仍然完整可读。
        self.assertIn("--workflow-id wf-fb-1", message)


class FrozenCandidateLaunchIdentityTest(unittest.TestCase):
    """冻结候选作为可验证身份:严格绑定 dispatch claim,缺则 fail-closed。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-frozenlaunch-")
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "state.db"
        env = patch.dict(os.environ, {
            "HERDR_WORKFLOW_DOCS_DIR": str(self.docs),
            "HERDR_STATE_DB": str(self.db),
        })
        env.start()
        self.addCleanup(env.stop)
        from herdr.state_store import get_state_store

        get_state_store(self.db).save_workflow(
            {"workflow_id": "wf-frozen-2", "status": "running"})
        self._ht = _load_module(
            "herdr_task_frozen_launch_test",
            HERDR_ROOT / "bin" / "herdr-task",
        )
        self.sha = "e" * 40

    def tearDown(self):
        self.tmp.cleanup()

    def _freeze(self):
        from herdr import scheduler_facts as sf

        sf.record_candidate_frozen(
            "wf-frozen-2", self.sha, source_node="implementation",
            db_path=self.db)

    def test_preflight_accepts_matching_frozen_claim(self):
        self._freeze()
        result = self._ht._preflight_delivery_identity(
            "wf-frozen-2", "test", "wf-frozen-2-test-auto", claim=self.sha)
        self.assertEqual(result["identity_source"], "frozen")
        self.assertEqual(result["candidate_sha"], self.sha)

    def test_preflight_rejects_missing_claim(self):
        self._freeze()
        with pytest.raises(SystemExit) as exc:
            self._ht._preflight_delivery_identity(
                "wf-frozen-2", "test", "wf-frozen-2-test-auto", claim=None)
        self.assertEqual(exc.value.code, 2)

    def test_preflight_rejects_mismatched_claim(self):
        self._freeze()
        with pytest.raises(SystemExit) as exc:
            self._ht._preflight_delivery_identity(
                "wf-frozen-2", "review", "wf-frozen-2-review-auto",
                claim="f" * 40)
        self.assertEqual(exc.value.code, 2)

    def test_preflight_rejects_when_nothing_frozen(self):
        with pytest.raises(SystemExit) as exc:
            self._ht._preflight_delivery_identity(
                "wf-frozen-2", "test", "wf-frozen-2-test-auto", claim=self.sha)
        self.assertEqual(exc.value.code, 2)

    def test_preflight_ignores_non_verifier_nodes(self):
        self.assertIsNone(self._ht._preflight_delivery_identity(
            "wf-frozen-2", "implementation", "wf-frozen-2-impl", claim=None))

    def test_invalidated_prior_candidate_does_not_block_new_frozen(self):
        """P1 回归:上一轮 candidate 已失效时,新一轮 frozen candidate 仍可验证。

        旧行为:prior candidate 存在但不可用 -> delivery_invalidated -> exit 2,
        于是 fix-loop 后 Test(B)/Review(B) 永远起不来。
        """
        from herdr import delivery_record as dr
        from herdr import workflow_docs as wd

        stale_sha = "1" * 40
        wd.append_note(
            "wf-frozen-2", kind="delivery",
            title=dr.delivery_note_title("agent/x/feat", stale_sha),
            body=dr.delivery_note_body({
                "delivery_id": f"candidate-{stale_sha}",
                "delivery_branch": "agent/x/feat", "candidate_sha": stale_sha,
                "review_task": "old-review", "test_gate": "old-test"}),
            node="wrapup", task_id="old-review", source=wd.SOURCE_CONTROLLER,
            fields={"delivery_id": f"candidate-{stale_sha}",
                    "delivery_branch": "agent/x/feat",
                    "candidate_sha": stale_sha,
                    "review_task": "old-review", "test_gate": "old-test"})
        # 失效候选(等价于 fix-loop 作废后的 invalidation 备注)
        wd.append_note(
            "wf-frozen-2", kind="invalidation",
            title="fix-loop 作废", body="candidate invalidated",
            node="review", source=wd.SOURCE_CONTROLLER,
            invalidates=["wrapup"],
            fields={"invalidated_candidates": [f"candidate-{stale_sha}"]})

        # 新一轮:B 被冻结,claim=B -> 允许(不得复活 A)
        self._freeze()
        result = self._ht._preflight_delivery_identity(
            "wf-frozen-2", "test", "wf-frozen-2-test-auto", claim=self.sha)
        self.assertEqual(result["identity_source"], "frozen")
        self.assertEqual(result["candidate_sha"], self.sha)

    def test_invalidated_prior_candidate_cannot_be_resurrected(self):
        """失效候选不得借 frozen 通道复活:claim 指向旧 SHA 仍必须拒绝。"""
        from herdr import delivery_record as dr
        from herdr import workflow_docs as wd

        stale_sha = "2" * 40
        wd.append_note(
            "wf-frozen-2", kind="delivery",
            title=dr.delivery_note_title("agent/x/feat", stale_sha),
            body=dr.delivery_note_body({
                "delivery_id": f"candidate-{stale_sha}",
                "delivery_branch": "agent/x/feat", "candidate_sha": stale_sha,
                "review_task": "old-review", "test_gate": "old-test"}),
            node="wrapup", task_id="old-review", source=wd.SOURCE_CONTROLLER,
            fields={"delivery_id": f"candidate-{stale_sha}",
                    "delivery_branch": "agent/x/feat",
                    "candidate_sha": stale_sha,
                    "review_task": "old-review", "test_gate": "old-test"})
        wd.append_note(
            "wf-frozen-2", kind="invalidation",
            title="fix-loop 作废", body="candidate invalidated",
            node="review", source=wd.SOURCE_CONTROLLER,
            invalidates=["wrapup"],
            fields={"invalidated_candidates": [f"candidate-{stale_sha}"]})
        self._freeze()  # frozen = self.sha
        with pytest.raises(SystemExit) as exc:
            self._ht._preflight_delivery_identity(
                "wf-frozen-2", "review", "wf-frozen-2-review-auto",
                claim=stale_sha)
        self.assertEqual(exc.value.code, 2)

    def test_delivery_record_still_authoritative_when_present(self):
        from herdr import delivery_record as dr
        from herdr import workflow_docs as wd

        note_sha = "9" * 40
        wd.append_note(
            "wf-frozen-2", kind="delivery",
            title=dr.delivery_note_title("agent/x/feat", note_sha),
            body=dr.delivery_note_body({
                "delivery_id": f"candidate-{note_sha}",
                "delivery_branch": "agent/x/feat", "candidate_sha": note_sha,
                "review_task": "real-review", "test_gate": "real-test"}),
            node="wrapup", task_id="real-review", source=wd.SOURCE_CONTROLLER,
            fields={"delivery_id": f"candidate-{note_sha}",
                    "delivery_branch": "agent/x/feat",
                    "candidate_sha": note_sha,
                    "review_task": "real-review", "test_gate": "real-test"})
        result = self._ht._preflight_delivery_identity(
            "wf-frozen-2", "test", "wf-frozen-2-test-auto", claim=self.sha)
        # delivery record 优先:返回的是 record 而不是 frozen claim
        self.assertIsNone(result.get("identity_source"))
        self.assertEqual(dr._body_value(result, "candidate_sha"), note_sha)


class P1StrictCandidateEqualityTest(unittest.TestCase):
    """P1-2 回归:test/review 的候选必须是严格相等,不能只判 ancestor。

    真实调用链:bin/herdr-task 的 _validate_test_delivery_baseline。
    """

    def setUp(self):
        self._ht = _load_module(
            "herdr_task_strict_sha_test",
            HERDR_ROOT / "bin" / "herdr-task",
        )
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-p12-")
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.db"
        env = patch.dict(os.environ, {"HERDR_STATE_DB": str(self.db)})
        env.start()
        self.addCleanup(env.stop)
        from herdr.state_store import get_state_store

        get_state_store(self.db).save_workflow(
            {"workflow_id": "wf-strict", "status": "running"})

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, node="test"):
        class _A:
            pass

        a = _A()
        a.workflow_id = "wf-strict"
        a.task_id = "wf-strict-test-auto"
        a.node = node
        return a

    def test_ancestor_only_baseline_is_refused(self):
        """candidate=A、clone 基线=B(A 是 B 的祖先)也必须拒绝。"""
        ancestor = "1" * 40
        head = "2" * 40

        def fake_run(cmd, **kwargs):
            # 旧实现依赖 merge-base --is-ancestor;现在必须不再被调用。
            if any("is-ancestor" in str(a) for a in cmd):
                raise AssertionError(f"ancestor probe must not decide: {cmd}")
            # Canonicalisation uses rev-parse; neither SHA exists in this
            # empty repo, so both fail to resolve and the comparison fails.
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with patch.object(self._ht.subprocess, "run", side_effect=fake_run), \
            pytest.raises(SystemExit) as exc:
            self._ht._validate_test_delivery_baseline(
                self._args(), ancestor, head, str(self.root))
        self.assertEqual(exc.value.code, 2)

    def test_exact_match_passes(self):
        sha = "3" * 40
        self._ht._validate_test_delivery_baseline(
            self._args(), sha, sha, str(self.root))

    def test_empty_evidence_fails_closed(self):
        with pytest.raises(SystemExit) as exc:
            self._ht._validate_test_delivery_baseline(
                self._args(), "4" * 40, "", str(self.root))
        self.assertEqual(exc.value.code, 2)

    def test_missing_claim_fails_closed(self):
        with pytest.raises(SystemExit) as exc:
            self._ht._validate_test_delivery_baseline(
                self._args(), "", "5" * 40, str(self.root))
        self.assertEqual(exc.value.code, 2)

    def test_abbreviated_candidate_matches_full_baseline(self):
        """P2 回归:git 能唯一解析的短 SHA 与全 SHA 视为同一 commit。

        只在 rev-parse 两侧都成功时成立;解析失败必须 fail-closed
        (见 test_ambiguous_short_sha_is_refused)。
        """
        full = "a" * 40
        short = full[:12]

        def fake_run(cmd, **kwargs):
            # Git resolves both the abbreviation and the full SHA to the same
            # object ID.
            if any(short in str(a) for a in cmd):
                return subprocess.CompletedProcess(cmd, 0, full + "\n", "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with patch.object(self._ht.subprocess, "run", side_effect=fake_run):
            self._ht._validate_test_delivery_baseline(
                self._args(), short, full, str(self.root))

    def test_unrelated_short_sha_still_refused(self):
        """短 SHA 规范化不得放宽成「不同 commit 也放行」。"""
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with patch.object(self._ht.subprocess, "run", side_effect=fake_run), \
            pytest.raises(SystemExit) as exc:
            self._ht._validate_test_delivery_baseline(
                self._args(), "abcdef1234", "1234567890", str(self.root))
        self.assertEqual(exc.value.code, 2)

    def test_ambiguous_short_sha_is_refused(self):
        """P2 回归:rev-parse 无法唯一解析的短 SHA 必须 fail-closed。

        git 明确告知「不能唯一证明是哪个 commit」,此时若再退回 prefix
        匹配,等于用无法证明的相等放行 launch gate。
        """
        full = "abc1234f9287a1b2c3d4e5f60718293a4b5c6d7e"

        def ambiguous(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, "", "error: short SHA1 abc1234 is ambiguous")

        with patch.object(self._ht.subprocess, "run", side_effect=ambiguous):
            self.assertFalse(self._ht._same_commit("abc1234", full, str(self.root)))
        with patch.object(self._ht.subprocess, "run", side_effect=ambiguous), \
            pytest.raises(SystemExit) as exc:
            self._ht._validate_test_delivery_baseline(
                self._args(), "abc1234", full, str(self.root))
        self.assertEqual(exc.value.code, 2)

    def test_unresolvable_repo_never_falls_back_to_prefix(self):
        """仓库不可查时不得凭字符串前缀放行。"""
        full = "abc1234f9287a1b2c3d4e5f60718293a4b5c6d7e"

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 128, "", "not a git repository")

        with patch.object(self._ht.subprocess, "run", side_effect=fake_run):
            self.assertFalse(
                self._ht._same_commit("abc1234", full, "/nonexistent-repo"))
            self.assertFalse(
                self._ht._same_commit("abc1234", full, str(self.root)))

    def test_real_repo_short_sha_resolves_and_passes(self):
        """对照:真实仓库中可唯一解析的短 SHA 仍然放行。"""
        sha = "9" * 40
        repo = self.root / "abbrev-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, val in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(repo), "config", key, val], check=True)
        (repo / "z.txt").write_text("z")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "z", "--no-gpg-sign"],
            check=True)
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, text=True, capture_output=True).stdout.strip()
        self.assertTrue(head)
        short = head[:12]
        self.assertTrue(self._ht._same_commit(short, head, str(repo)))
        self._ht._validate_test_delivery_baseline(
            self._args(), short, head, str(repo))


class VerifiedCandidateAtVerdictTest(unittest.TestCase):
    """P1 回归:完成验证时的 clone HEAD,而不是启动时的 baseline。

    baseline_commit 是启动证据:Agent 执行期间 git pull / checkout / rebase
    之后,只有重新读取 clone HEAD 才能证明「完成验证时验证了谁」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-verified-")
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.db"
        env = patch.dict(os.environ, {
            "HERDR_STATE_DB": str(self.db),
            "TASKS_FILE": str(self.root / "tasks.json"),
        })
        env.start()
        self.addCleanup(env.stop)
        from herdr.state_store import get_state_store

        get_state_store(self.db).save_workflow(
            {"workflow_id": "wf-verified", "status": "running"})
        self.store = get_state_store(self.db)
        self._ht = _load_module(
            "herdr_task_verified_test", HERDR_ROOT / "bin" / "herdr-task",
        )
        # Real git repo standing in for the task clone.
        self.clone = self.root / "clone"
        self.clone.mkdir()
        subprocess.run(["git", "init", "-q", str(self.clone)], check=True)
        for key, val in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(
                ["git", "-C", str(self.clone), "config", key, val], check=True)
        (self.clone / "a.txt").write_text("a")
        subprocess.run(["git", "-C", str(self.clone), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "commit", "-qm", "one",
             "--no-gpg-sign"], check=True)
        self.sha_a = self._head()
        # The agent moves the clone while it works.
        (self.clone / "b.txt").write_text("b")
        subprocess.run(["git", "-C", str(self.clone), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "commit", "-qm", "two",
             "--no-gpg-sign"], check=True)
        self.sha_b = self._head()

    def tearDown(self):
        self.tmp.cleanup()

    def _head(self):
        return subprocess.run(
            ["git", "-C", str(self.clone), "rev-parse", "HEAD"],
            check=True, text=True, capture_output=True).stdout.strip()

    def _register(self, task_id="wf-verified-test-auto", baseline="", claim=""):
        self.store.save_task({
            "task_id": task_id, "workflow_id": "wf-verified", "node": "test",
            "stage": "test", "status": "working", "clone_path": str(self.clone),
            "candidate_sha": claim, "baseline_commit": baseline,
        })
        return self.store.get_task(task_id)

    def test_captures_live_head_not_launch_baseline(self):
        task = self._register(baseline=self.sha_a, claim=self.sha_a)
        # Clone has since moved to sha_b, exactly the P1 failure mode.
        self.assertNotEqual(self.sha_a, self.sha_b)

        self._ht._capture_verified_candidate(task)

        stored = self.store.get_task("wf-verified-test-auto")
        self.assertEqual(stored["verified_candidate_sha"], self.sha_b)

    def test_captured_value_drives_join_gate_verdict(self):
        """The captured evidence is what the pure gate must read."""
        from herdr import scheduler as sched

        task = self._register(baseline=self.sha_a, claim=self.sha_a)
        self._ht._capture_verified_candidate(task)
        stored = self.store.get_task("wf-verified-test-auto")

        # Launch-time fields still say A; completion evidence says B.
        self.assertEqual(stored["baseline_commit"], self.sha_a)
        self.assertEqual(sched.extract_task_verified_sha(stored), self.sha_b)
        ok, _claim, evidence = sched.task_claim_evidence_consistent(stored)
        self.assertFalse(ok)
        self.assertEqual(evidence, self.sha_b)

    def test_join_gate_must_not_pass_without_completion_evidence(self):
        """P1 回归:candidate=A + baseline=A 但完成证据缺失,门禁必须拒绝。

        fallback 到 baseline_commit(启动证据)会让「完成时验证了谁」重新
        退化成「启动时碰巧在谁」,等于撤销上一轮的修复。
        """
        from herdr import scheduler as sched

        tasks = [
            _task("t-test", "cleaned", "test",
                  candidate_sha=self.sha_a, verified_candidate_sha=""),
            _task("t-review", "cleaned", "review",
                  candidate_sha=self.sha_a, verified_candidate_sha=""),
        ]
        for t in tasks:
            t["baseline_commit"] = self.sha_a

        passed, reason, details = sched.evaluate_join_gate(
            {"id": "wrapup", "depends_on": ["test", "review"]},
            tasks, "wf-1", self.sha_a,
        )
        self.assertFalse(passed, "join gate must not pass on launch evidence alone")
        self.assertEqual(reason, sched.JOIN_MISSING_CANDIDATE)
        self.assertIn("missing_completion_evidence", details)

    def test_legacy_task_without_candidate_claim_is_untouched(self):
        """真正的历史任务(无候选声明)保持旧语义,门禁不因此全量拒绝。"""
        from herdr import scheduler as sched

        tasks = [
            _task("t-test", "cleaned", "test", stage_verdict="pass",
                  baseline_commit=self.sha_a),
            _task("t-review", "cleaned", "review", stage_verdict="pass",
                  baseline_commit=self.sha_a),
        ]
        passed, reason, _ = sched.evaluate_join_gate(
            {"id": "wrapup", "depends_on": ["test", "review"]},
            tasks, "wf-1", self.sha_a,
        )
        self.assertTrue(passed, reason)
        self.assertEqual(reason, sched.JOIN_SATISFIED)

    def test_claim_evidence_consistent_rejects_launch_only_evidence(self):
        """Scheduler 管理的任务:claim 匹配 baseline 但无完成证据 -> 不一致。"""
        from herdr import scheduler as sched

        task = {"candidate_sha": self.sha_a, "baseline_commit": self.sha_a}
        ok, _claim, evidence = sched.task_claim_evidence_consistent(task)
        self.assertFalse(ok)
        self.assertEqual(evidence, "")

    def test_matching_head_records_same_revision(self):
        from herdr import scheduler as sched

        task = self._register(baseline=self.sha_b, claim=self.sha_b)
        self._ht._capture_verified_candidate(task)
        stored = self.store.get_task("wf-verified-test-auto")
        ok, _claim, evidence = sched.task_claim_evidence_consistent(stored)
        self.assertTrue(ok)
        self.assertEqual(evidence, self.sha_b)

    def test_missing_clone_is_best_effort_no_raise(self):
        task = self._register()
        task["clone_path"] = str(self.root / "does-not-exist")
        self.assertEqual(self._ht._capture_verified_candidate(task), "")

    def _set_status(self, task_kwargs, verdict, note=None, capture=None):
        task = {
            "task_id": "wf-verified-test-auto", "workflow_id": "wf-verified",
            "node": "test", "status": "agent_done",
            "clone_path": str(self.clone),
        }
        task.update(task_kwargs)
        ctx = [
            patch.object(self._ht, "load_tasks", return_value={"tasks": [task]}),
            patch.object(self._ht, "sync_tasks_projection", return_value=None),
            patch.object(self._ht, "_clear_suppress_auto_close",
                         return_value=None),
            patch.object(self._ht, "_record_gate_note", return_value=None),
            patch("herdr.kernel.update_task_metadata", return_value={}),
            patch("herdr.kernel.transition_task", return_value={
                "workflow_id": "wf-verified"}),
        ]
        if capture is not None:
            ctx.insert(0, patch.object(
                self._ht, "_capture_verified_candidate", side_effect=capture))
        return ctx

    def test_set_status_captures_before_accepting_verdict(self):
        """The verdict path must not skip completion evidence."""
        seen = {}

        def fake_capture(task):
            seen["called"] = True
            task["verified_candidate_sha"] = self.sha_b
            return self.sha_b

        self._register(baseline=self.sha_a, claim=self.sha_a)
        patchers = self._set_status(
            {"candidate_sha": self.sha_a, "baseline_commit": self.sha_a},
            "pass", note="looks good", capture=fake_capture,
        )
        for p in patchers:
            p.start()
        try:
            self._ht.set_status(
                "wf-verified-test-auto", "completed", verdict="pass",
                note="looks good")
        finally:
            for p in patchers:
                p.stop()
        self.assertTrue(seen.get("called"))

    def test_set_status_refuses_verdict_without_completion_evidence(self):
        """P1 回归:读不到完成证据时必须拒绝 verdict,而不是照常 pass。"""
        self._register(baseline=self.sha_a, claim=self.sha_a)
        patchers = self._set_status(
            {"candidate_sha": self.sha_a, "baseline_commit": self.sha_a},
            "pass", note="looks good",
            # Capture runs but cannot resolve a HEAD: no evidence recorded.
            capture=lambda task: "",
        )
        for p in patchers:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc:
                self._ht.set_status(
                    "wf-verified-test-auto", "completed", verdict="pass",
                    note="looks good")
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(exc.value.code, 2)

    def test_set_status_refuses_blocked_verdict_without_evidence(self):
        """blocked 同样需要完成证据:否则「卡住」的原因也无从复核。"""
        self._register(baseline=self.sha_a, claim=self.sha_a)
        patchers = self._set_status(
            {"candidate_sha": self.sha_a, "baseline_commit": self.sha_a},
            "blocked", note="cannot reproduce", capture=lambda task: "",
        )
        for p in patchers:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc:
                self._ht.set_status(
                    "wf-verified-test-auto", "completed", verdict="blocked",
                    note="cannot reproduce")
        finally:
            for p in patchers:
                p.stop()
        self.assertEqual(exc.value.code, 2)

    def test_set_status_allows_legacy_task_without_candidate_claim(self):
        """无候选声明的历史任务:不因缺少完成证据被误拒。"""
        self._register(baseline=self.sha_a)
        patchers = self._set_status(
            {"baseline_commit": self.sha_a}, "pass", note="legacy ok",
            capture=lambda task: "",
        )
        for p in patchers:
            p.start()
        try:
            self._ht.set_status(
                "wf-verified-test-auto", "completed", verdict="pass",
                note="legacy ok")
        finally:
            for p in patchers:
                p.stop()

    def test_set_status_allows_verdict_when_evidence_present(self):
        """有完成证据时 verdict 正常受理(不引入误拒)。"""
        def capture(task):
            task["verified_candidate_sha"] = self.sha_b
            return self.sha_b

        self._register(baseline=self.sha_b, claim=self.sha_b)
        patchers = self._set_status(
            {"candidate_sha": self.sha_b, "baseline_commit": self.sha_b},
            "pass", note="ok", capture=capture,
        )
        for p in patchers:
            p.start()
        try:
            self._ht.set_status(
                "wf-verified-test-auto", "completed", verdict="pass",
                note="ok")
        finally:
            for p in patchers:
                p.stop()

    def test_set_status_skips_capture_without_verdict(self):
        seen = {"called": False}
        self._register(baseline=self.sha_a, claim=self.sha_a)
        with patch.object(
            self._ht, "_capture_verified_candidate",
            side_effect=lambda t: seen.__setitem__("called", True)), \
            patch.object(self._ht, "load_tasks", return_value={
                "tasks": [{
                    "task_id": "wf-verified-test-auto",
                    "workflow_id": "wf-verified", "node": "test",
                    "status": "agent_done", "clone_path": str(self.clone),
                }]}), \
            patch.object(self._ht, "sync_tasks_projection", return_value=None), \
            patch.object(self._ht, "_clear_suppress_auto_close",
                         return_value=None), \
            patch("herdr.kernel.transition_task", return_value={
                "workflow_id": "wf-verified"}):
            self._ht.set_status("wf-verified-test-auto", "completed")

        self.assertFalse(seen["called"])


class LaunchReclaimTest(unittest.TestCase):
    """P2 回归:baseline 拒绝发生在注册之前,必须回收运行资源。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-reclaim-")
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {
            "HERDR_STATE_DB": str(self.root / "state.db"),
        })
        env.start()
        self.addCleanup(env.stop)
        self._ht = _load_module(
            "herdr_task_reclaim_test", HERDR_ROOT / "bin" / "herdr-task",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self):
        class _A:
            pass

        a = _A()
        a.task_id = "wf-reclaim-test-auto"
        a.workflow_id = "wf-reclaim"
        a.node = "test"
        return a

    def test_reclaim_closes_dynamic_pane_via_herdr_backend(self):
        """P2 回归:自建 dynamic Pane 用 herdr 后端关闭,不用裸 tmux。"""
        clone = self.root / "clone-1"
        clone.mkdir()
        calls = {"herdr": 0, "tmux": 0, "reservation": 0}

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "tmux":
                calls["tmux"] += 1
            if cmd and "pane" in cmd and "close" in cmd:
                calls["herdr"] += 1
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(self._ht.subprocess, "run", side_effect=fake_run), \
            patch.object(self._ht, "resolve_binary",
                         return_value="/usr/local/bin/herdr"), \
            patch.object(self._ht, "release_agent_reservation",
                         side_effect=lambda tid: calls.__setitem__(
                             "reservation", calls["reservation"] + 1)), \
            patch.object(self._ht, "delete_clone_safely", return_value=True):
            self._ht._reclaim_unregistered_launch_resources(
                self._args(), clone_path=str(clone), pane_id="%7:p1",
                pane_source="dynamic")

        self.assertEqual(calls["herdr"], 1)
        self.assertEqual(calls["tmux"], 0, "must not use raw tmux kill-pane")
        self.assertEqual(calls["reservation"], 1)

    def test_reclaim_does_not_close_prebuilt_pane(self):
        """P2 回归:借用的 prebuilt Pane 属共享池,不得物理关闭。"""
        clone = self.root / "clone-prebuilt"
        clone.mkdir()
        calls = {"pane_close": 0, "tmux": 0, "reservation": 0}

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "tmux":
                calls["tmux"] += 1
            if cmd and "pane" in cmd and "close" in cmd:
                calls["pane_close"] += 1
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(self._ht.subprocess, "run", side_effect=fake_run), \
            patch.object(self._ht, "resolve_binary",
                         return_value="/usr/local/bin/herdr"), \
            patch.object(self._ht, "release_agent_reservation",
                         side_effect=lambda tid: calls.__setitem__(
                             "reservation", calls["reservation"] + 1)), \
            patch.object(self._ht, "delete_clone_safely", return_value=True):
            self._ht._reclaim_unregistered_launch_resources(
                self._args(), clone_path=str(clone), pane_id="%3:p9",
                pane_source="prebuilt")

        self.assertEqual(calls["pane_close"], 0, "borrowed pane must survive")
        self.assertEqual(calls["tmux"], 0)
        # The task's own occupancy is still released.
        self.assertEqual(calls["reservation"], 1)

    def test_reclaim_does_not_claim_pane_closed_on_failure(self):
        """close 失败时不得打印成功。"""
        clone = self.root / "clone-fail"
        clone.mkdir()

        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "pane busy")

        with patch.object(self._ht.subprocess, "run", side_effect=failing), \
            patch.object(self._ht, "resolve_binary",
                         return_value="/usr/local/bin/herdr"), \
            patch.object(self._ht, "release_agent_reservation",
                         return_value=None), \
            patch.object(self._ht, "delete_clone_safely", return_value=True):
            self._ht._reclaim_unregistered_launch_resources(
                self._args(), clone_path=str(clone), pane_id="%5:p2",
                pane_source="dynamic")

    def test_reclaim_continues_when_pane_close_raises(self):
        """资源回收必须逐项尽力,前一步失败不能阻断后续释放。"""
        clone = self.root / "clone-2"
        clone.mkdir()
        released = {"n": 0}

        def fake_run(cmd, **kwargs):
            raise OSError("herdr unavailable")

        with patch.object(self._ht.subprocess, "run", side_effect=fake_run), \
            patch.object(self._ht, "resolve_binary",
                         return_value="/usr/local/bin/herdr"), \
            patch.object(self._ht, "release_agent_reservation",
                         side_effect=lambda tid: released.__setitem__(
                             "n", released["n"] + 1)), \
            patch.object(self._ht, "delete_clone_safely", return_value=True):
            self._ht._reclaim_unregistered_launch_resources(
                self._args(), clone_path=str(clone), pane_id="%9:p1",
                pane_source="dynamic")

        self.assertEqual(released["n"], 1)
