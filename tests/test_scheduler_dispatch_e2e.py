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
import os
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


class P1DeliveryNoteFreezeTest(unittest.TestCase):
    """P1-3 回归:freeze 必须真能打通 launch 的 delivery preflight。

    背景:Controller 曾用"分支 HEAD 兜底"算出 candidate_sha,而
    herdr-task launch 的 _preflight_delivery_identity 在缺 delivery note 时
    直接 exit 2 —— 两层契约互相打架。本测试真实执行 record-delivery 路径,
    证明冻结后 delivery note 存在且 SHA 一致。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-p13-")
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
            {"workflow_id": "wf-freeze-1", "status": "running"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_freeze_creates_delivery_note_usable_by_launch(self):
        from herdr import delivery_record as dr
        from herdr import workflow_docs as wd

        sha = "a" * 40
        with patch.object(_ctl, "_scheduler_expected_candidate_sha",
                          return_value=sha), \
            patch.object(_ctl, "load_tasks", return_value=[]):
            frozen = _ctl._scheduler_freeze_candidate(
                "wf-freeze-1", str(self.root), "implementation", [])
        self.assertEqual(frozen, sha)
        notes = wd.load_notes("wf-freeze-1")
        self.assertTrue(
            [n for n in notes if n.get("kind") == "delivery"],
            "freeze must create a delivery note so FR-6.2 preflight passes",
        )
        effective = dr.select_effective_delivery(notes, workflow_id="wf-freeze-1")
        self.assertIsNotNone(effective)
        self.assertEqual(dr._body_value(effective, "candidate_sha"), sha)

    def test_freeze_is_idempotent_on_same_sha(self):
        from herdr import workflow_docs as wd

        sha = "b" * 40
        with patch.object(_ctl, "_scheduler_expected_candidate_sha",
                          return_value=sha), \
            patch.object(_ctl, "load_tasks", return_value=[]):
            self.assertEqual(_ctl._scheduler_freeze_candidate(
                "wf-freeze-1", str(self.root), "implementation", []), sha)
            self.assertEqual(_ctl._scheduler_freeze_candidate(
                "wf-freeze-1", str(self.root), "implementation", []), sha)
        notes = [n for n in wd.load_notes("wf-freeze-1")
                 if n.get("kind") == "delivery"]
        self.assertEqual(len(notes), 1)

    def test_candidate_rotation_supersedes_previous_note(self):
        from herdr import delivery_record as dr
        from herdr import workflow_docs as wd

        first, second = "c" * 40, "d" * 40
        with patch.object(_ctl, "load_tasks", return_value=[]):
            with patch.object(_ctl, "_scheduler_expected_candidate_sha",
                              return_value=first):
                self.assertEqual(_ctl._scheduler_freeze_candidate(
                    "wf-freeze-1", str(self.root), "implementation", []), first)
            with patch.object(_ctl, "_scheduler_expected_candidate_sha",
                              return_value=second):
                self.assertEqual(_ctl._scheduler_freeze_candidate(
                    "wf-freeze-1", str(self.root), "implementation", []), second)
        notes = wd.load_notes("wf-freeze-1")
        effective = dr.select_effective_delivery(notes, workflow_id="wf-freeze-1")
        self.assertIsNotNone(effective, "rotation must leave exactly one eligible tip")
        self.assertEqual(dr._body_value(effective, "candidate_sha"), second)

    def test_unprovable_sha_returns_empty(self):
        with patch.object(_ctl, "_scheduler_expected_candidate_sha",
                          return_value=""):
            self.assertEqual(_ctl._scheduler_freeze_candidate(
                "wf-freeze-1", str(self.root), "implementation", []), "")


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
            raise AssertionError(f"ancestor probe must not decide: {cmd}")

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
