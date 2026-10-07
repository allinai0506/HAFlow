"""Selective Replan v1 controller-integration tests (HAFlow PR #110).

验证真实调用链:blocked 门禁 → 显式 affected_task_ids → 机器校验 →
immutable fact 持久化 → targeted invalidation(只 supersede 点名谱系)
→ preserve 零写入 → target-aware latch → 现有 redispatch pipeline 补派 -rN。

git 使用真实临时仓库;非 git 的 subprocess 被拦截,其中 herdr-task 的
supersede/finalize 由 shim 施加到测试 store(断言 controller 决策,
CLI 语义由 test_selective_replan_core.py 的子进程测试覆盖)。
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import direct_dispatch as direct_dispatch_planner  # noqa: E402
from herdr import scheduler_facts as facts  # noqa: E402
from herdr import selective_replan as srp  # noqa: E402


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctl = _load_module(
    "herdr_controller_selective_replan_integration_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)

WF = "wf-srp-ctl"
SHA_A = "a" * 40
SHA_B = "b" * 40

_TS = [1000.0]


def _tick():
    _TS[0] += 1.0
    return _TS[0]


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args), check=check,
        text=True, capture_output=True)


class _SubprocessShim:
    """subprocess 替身:git 走真实执行,其余命令交给 fake_run。"""

    def __init__(self, fake_run):
        self._fake_run = fake_run

    def run(self, cmd, **kwargs):
        return self._fake_run(cmd, **kwargs)

    def __getattr__(self, name):
        return getattr(subprocess, name)


class _Queue:
    def __init__(self, items):
        self.items = items

    def put(self, item):
        self.items.append(item)


class ControllerSelectiveReplanTest(unittest.TestCase):
    """blocked 门禁 + 显式归因 → 选择性返工,全链路。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-srp-ctl-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        self.write_repo("seed.txt", "seed")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "seed", "--no-gpg-sign")

        self.docs = self.root / "wdocs"
        self.docs.mkdir()
        self.verdicts = self.root / "verdicts"
        self.verdicts.mkdir()
        self.db = self.root / "state.db"
        self.state_file = self.root / "stage-state.json"
        self.workflow_file = self.root / "workflow.yaml"
        self.legacy_workflow_file = self.root / "workflow-legacy.yaml"
        self._write_workflow_files()

        env = patch.dict(os.environ, {
            "HERDR_WORKFLOW_DOCS_DIR": str(self.docs),
            "HERDR_STATE_DB": str(self.db),
            "HERDR_GATE_VERDICT_DIR": str(self.verdicts),
            "TASKS_FILE": str(self.root / "tasks.json"),
            "WORKFLOWS_FILE": str(self.workflow_file),
        })
        env.start()
        self.addCleanup(env.stop)

        # STAGE_STATE_FILE 在 import 时被读成模块常量,必须在模块上重定向,
        # 否则真实 sweep 会把测试 workflow 写进运维的 stage-state.json。
        self._patch("_ctl.STAGE_STATE_FILE", str(self.state_file))
        policies = self.root / "policies.json"
        policies.write_text("{}", encoding="utf-8")
        self._patch("_ctl.STAGE_POLICIES_FILE", str(policies))

        from herdr.state_store import get_state_store
        self.store = get_state_store(self.db)
        self.store.save_workflow({"workflow_id": WF, "status": "running"})

        self.launches = []
        self.queue = []
        real_run = subprocess.run
        task_manager = str(_ctl.TASK_MANAGER)

        def fake_run(cmd, **kwargs):
            argv = [str(c) for c in (cmd or [])]
            if argv and argv[0] == "git":
                return real_run(cmd, **kwargs)
            if argv and argv[0] == task_manager and len(argv) > 1:
                if argv[1] == "launch":
                    self.launches.append(argv)
                    return subprocess.CompletedProcess(
                        cmd, 0, "Task dispatched: x", "")
                if argv[1] == "supersede" and len(argv) > 2:
                    return self._apply_supersede(argv[2])
                if argv[1] == "finalize" and len(argv) > 2:
                    return self._apply_finalize(argv[2])
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        self._patch("_ctl.subprocess", _SubprocessShim(fake_run))
        self._patch("_ctl.coordinator_queue", _Queue(self.queue))
        self._patch("_ctl.project_for_workflow", lambda wf: {
            "project_root": str(self.repo),
            "base_branch": "main",
            "coordinator_pane_id": "1:1",
            "startup_ready": True,
            "requirement": "selective replan",
        })
        self._patch("_ctl.coordinator_pane_for_workflow", lambda wf: "1:1")
        self._patch("_ctl.workflow_config_for", lambda wf: self._wf_cfg())
        self._patch("_ctl.load_tasks", lambda: self.store.list_tasks())
        self._patch("_ctl.shared_docs_block", lambda *a, **k: "")
        self._patch("_ctl.notify_attention", lambda *a, **k: None)
        self._patch("_ctl.attention_get", lambda key: None)
        self._patch("_ctl.attention_note", lambda *a, **k: None)
        self._patch("_ctl.attention_blocks_retry", lambda key: False)
        self._patch("_ctl.workflow_closed", lambda wf: False)

    # -- harness ---------------------------------------------------------

    def _patch(self, target, value):
        owner, _, attr = target.rpartition(".")
        module = _ctl if owner == "_ctl" else __import__(owner, fromlist=["_"])
        patcher = patch.object(module, attr, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_workflow_files(self):
        import shutil
        template = (HERDR_ROOT / "workflow_templates"
                    / "software-development-v1.yaml")
        shutil.copy(template, self.workflow_file)
        text = template.read_text(encoding="utf-8")
        # 去掉 selective_replan 块 → 该 workflow 完全不感知 PR #110。
        head, sep, tail = text.partition("selective_replan:")
        assert sep, "template must carry the policy block"
        _, _, rest = tail.partition("retry_node: implementation")
        self.legacy_workflow_file.write_text(
            head + rest.lstrip("\n"), encoding="utf-8")

    def _wf_cfg(self):
        from herdr.workflow import load_template
        return load_template(str(self.workflow_file))

    def _legacy_wf_cfg(self):
        from herdr.workflow import load_template
        return load_template(str(self.legacy_workflow_file))

    def write_repo(self, rel, text="x"):
        target = self.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit_repo(self, message="c"):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", message, "--no-gpg-sign")
        return _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def _apply_supersede(self, task_id):
        record = dict(self.store.get_task(task_id) or {})
        if not record:
            return subprocess.CompletedProcess([], 1, "", "not found")
        record["status"] = "superseded"
        self.store.save_task(record)
        return subprocess.CompletedProcess([], 0, "ok", "")

    def _apply_finalize(self, task_id):
        record = dict(self.store.get_task(task_id) or {})
        if record:
            record["status"] = "cleaned"
            self.store.save_task(record)
        return subprocess.CompletedProcess([], 0, "ok", "")

    def save_task(self, **overrides):
        task_id = overrides.get("task_id")
        record = dict(self.store.get_task(task_id) or {}) if task_id else {}
        record.update({"workflow_id": WF})
        record.update(overrides)
        self.store.save_task(record)
        return record

    def seed_impl(self, task_id, status="completed", branch="main", **extra):
        overrides = {
            "node": "implementation",
            "stage": "implementation",
            "goal": f"实现 {task_id}",
            "acceptance_criteria": [f"AC-{task_id}"],
            "integration_mode": "git",
            "branch": branch,
            "updated_at": _tick(),
            "version": 1,
        }
        overrides.update(extra)
        return self.save_task(task_id=task_id, status=status, **overrides)

    def seed_gate(self, affected=None, sha=SHA_A, version=7,
                  task_id="wf-srp-ctl-review-1", node="review", **extra):
        overrides = {
            "node": node,
            "stage": node,
            "status": "completed",
            "stage_verdict": "blocked",
            "stage_verdict_note": "前端错误提示缺失",
            "candidate_sha": sha,
            "verified_candidate_sha": sha,
            "version": version,
            "updated_at": _tick(),
        }
        if affected is not None:
            overrides["stage_verdict_affected_task_ids"] = affected
        overrides.update(extra)
        return self.save_task(task_id=task_id, **overrides)

    def freeze(self, sha=SHA_A, branch=""):
        facts.record_candidate_frozen(
            WF, sha, source_node="implementation",
            delivery_branch=branch, db_path=self.db)

    def impl_tasks(self):
        return ["wf-srp-ctl-impl-A", "wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C"]

    def seed_three_impl(self, branch="main"):
        for task_id in self.impl_tasks():
            self.seed_impl(task_id, branch=branch)

    def snapshot(self, task_id):
        return dict(self.store.get_task(task_id) or {})

    def run_fix_loop(self, gate_node="review", workflow_cfg=None):
        cfg = workflow_cfg if workflow_cfg is not None else self._wf_cfg()
        _ctl.handle_fix_loop(
            WF, gate_node, {"retry_node": "implementation", "max_loops": 3},
            cfg,
        )

    def latch_state(self):
        if not self.state_file.exists():
            return None
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        return data.get(f"{WF}|fixloop|implementation|pending_redo")

    def selective_facts(self):
        return facts.list_selective_replan_decisions(WF, db_path=self.db)

    # -- cases -----------------------------------------------------------

    def test_selective_invalidation_preserves_untouched_tasks(self):
        """Case 1:只有被点名的 B 谱系被作废,A/C 零写入。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        before = {t: self.snapshot(t) for t in self.impl_tasks()}

        self.run_fix_loop()

        self.assertEqual(
            self.store.get_task("wf-srp-ctl-impl-B")["status"], "superseded")
        for task_id in ("wf-srp-ctl-impl-A", "wf-srp-ctl-impl-C"):
            self.assertEqual(
                self.snapshot(task_id), before[task_id],
                f"{task_id} must be untouched (status/version/branch/"
                "metadata/updated_at unchanged)")
        # 门禁与下游照常作废。
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-review-1")["status"],
            "superseded")

    def test_selective_persists_fact_latch_and_notification(self):
        """Case 2:事实、闩、通知三处同时携带 target 上下文。"""
        self.freeze()
        self.seed_three_impl()
        gate = self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        gate_version = self.store.get_task("wf-srp-ctl-review-1")["version"]

        self.run_fix_loop()

        stored = facts.latest_selective_replan_for_node(
            WF, "implementation", db_path=self.db)
        self.assertIsNotNone(stored, "selective fact must be persisted")
        self.assertEqual(stored["target_task_ids"], ["wf-srp-ctl-impl-B"])
        self.assertEqual(stored["target_lineage_roots"], ["wf-srp-ctl-impl-B"])
        self.assertEqual(stored["preserved_task_ids"],
                         ["wf-srp-ctl-impl-A", "wf-srp-ctl-impl-C"])
        self.assertEqual(stored["gate_task_id"], "wf-srp-ctl-review-1")
        self.assertEqual(stored["gate_task_version"], gate_version)
        self.assertTrue(gate_version > 0)
        self.assertEqual(stored["gate_candidate_sha"], SHA_A)
        self.assertEqual(stored["reason"], srp.REASON_OK)

        latch = self.latch_state()
        self.assertEqual(latch["mode"], "selective")
        self.assertEqual(latch["target_lineage_roots"], ["wf-srp-ctl-impl-B"])

        queued = [item for item in self.queue if item.get("kind") == "fix_loop"]
        self.assertTrue(queued)
        self.assertEqual(queued[-1]["mode"], "selective")
        self.assertEqual(queued[-1]["target_lineage_roots"],
                         ["wf-srp-ctl-impl-B"])

    def _assert_legacy_fallback(self, reason):
        """语义断言:无 selective 事实 → 后续一切走 legacy。"""
        self.assertIsNone(facts.latest_selective_replan_for_node(
            WF, "implementation", db_path=self.db))
        recorded = self.selective_facts()
        self.assertEqual(len(recorded), 1)
        payload = recorded[-1]["payload"]
        self.assertEqual(payload["mode"], srp.MODE_LEGACY_FALLBACK)
        self.assertEqual(payload["reason"], reason)

    def test_missing_affected_ids_falls_back_to_legacy(self):
        """Case 3:字段缺失 → legacy(implementation 完全不被作废)。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=None)

        self.run_fix_loop()

        for task_id in self.impl_tasks():
            self.assertEqual(self.store.get_task(task_id)["status"],
                             "completed")
        self._assert_legacy_fallback(srp.REASON_TARGETS_MISSING)
        self.assertNotIn("mode", self.latch_state() or {})
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-review-1")["status"],
            "superseded")

    def test_empty_affected_ids_falls_back_to_legacy(self):
        """Case 4:显式空列表(Verifier 无法归因)→ legacy。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=[])

        self.run_fix_loop()

        for task_id in self.impl_tasks():
            self.assertEqual(self.store.get_task(task_id)["status"],
                             "completed")
        self._assert_legacy_fallback(srp.REASON_TARGETS_EMPTY)

    def test_affected_ids_without_policy_are_ignored(self):
        """Case 5:未声明策略的 workflow 完全不感知 #110。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])

        self.run_fix_loop(workflow_cfg=self._legacy_wf_cfg())

        for task_id in self.impl_tasks():
            self.assertEqual(self.store.get_task(task_id)["status"],
                             "completed")
        self.assertEqual(self.selective_facts(), [])
        self.assertNotIn("mode", self.latch_state() or {})

    def test_unpersistable_fact_blocks_selective_invalidation(self):
        """Case 6:事实写不进去 → 绝不发生选择性作废(退 legacy)。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        with patch.object(
            _ctl.scheduler_facts_store, "record_selective_replan_decision",
            side_effect=RuntimeError("db down"),
        ):
            self.run_fix_loop()

        for task_id in self.impl_tasks():
            self.assertEqual(self.store.get_task(task_id)["status"],
                             "completed")
        self.assertEqual(self.selective_facts(), [])
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-review-1")["status"],
            "superseded")

    def test_one_invalid_target_rejects_entire_plan(self):
        """Case 7:2 个合法 + 1 个非法 = 整体拒绝,绝不部分接受。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=[
            "wf-srp-ctl-impl-A", "wf-srp-ctl-impl-B", "wf-srp-ctl-ghost"])

        self.run_fix_loop()

        for task_id in self.impl_tasks():
            self.assertEqual(self.store.get_task(task_id)["status"],
                             "completed")
        self._assert_legacy_fallback(
            f"{srp.REASON_TARGET_UNKNOWN}:wf-srp-ctl-ghost")

    def test_gate_identity_mismatch_falls_back(self):
        """Case 8:候选身份链断裂(冻结候选 ≠ 门禁验证) → legacy。"""
        self.freeze(SHA_B)
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"], sha=SHA_A)

        self.run_fix_loop()

        for task_id in self.impl_tasks():
            self.assertEqual(self.store.get_task(task_id)["status"],
                             "completed")
        self.assertEqual(self.selective_facts(), [])

    def test_resolution_is_idempotent_across_reruns(self):
        """Case 9:崩溃重放 → 同一 episode 只有一条事实,目标不变。"""
        self.freeze()
        self.seed_three_impl()
        gate = self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        tasks = self.store.list_tasks()

        first = _ctl._resolve_selective_replan(
            WF, "review", "implementation", self._wf_cfg(), [gate], tasks)
        second = _ctl._resolve_selective_replan(
            WF, "review", "implementation", self._wf_cfg(), [gate], tasks)

        self.assertEqual(first[0], ["wf-srp-ctl-impl-B"])
        self.assertEqual(second[0], ["wf-srp-ctl-impl-B"])
        self.assertEqual(first[1]["replan_id"], second[1]["replan_id"])
        self.assertEqual(len(self.selective_facts()), 1)

    def test_awaiting_redispatch_tracks_target_lineages(self):
        """Case 10:被作废谱系未补派前,节点必须被视为未完成。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])

        self.run_fix_loop()

        self.assertTrue(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))
        self.assertFalse(
            _ctl._selective_replan_awaiting_redispatch(WF, "test"))
        self.seed_impl("wf-srp-ctl-impl-B-r2", status="working")
        self.assertFalse(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))

    def test_selective_latch_ignores_preserved_completions(self):
        """Case 11:被保留任务落定不能解除 selective latch。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()

        self.assertTrue(_ctl._fix_loop_latch_blocks(WF, "implementation"))
        # 保留任务(A/C)在闩之后"完成"——不得放行。
        for task_id in ("wf-srp-ctl-impl-A", "wf-srp-ctl-impl-C"):
            self.save_task(task_id=task_id, status="completed",
                           updated_at=_tick())
        self.assertTrue(_ctl._fix_loop_latch_blocks(WF, "implementation"))
        # 目标谱系 B-r2 真正完成 → 放行。
        self.seed_impl("wf-srp-ctl-impl-B-r2", status="completed")
        self.assertFalse(_ctl._fix_loop_latch_blocks(WF, "implementation"))

    def test_replacement_dispatch_targets_only_superseded_lineage(self):
        """Case 12:补派只覆盖被作废谱系,且继承目标 + 注入 blocker 上下文。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()

        notes = _ctl._selective_redispatch_blocker_notes(WF, "implementation")
        self.assertIn("wf-srp-ctl-impl-B", notes)
        node = self._impl_node()
        plan = direct_dispatch_planner.plan_stage_dispatch(
            WF, node, self.store.list_tasks(), "selective replan",
            redispatch_blocker_notes=notes,
        )
        self.assertEqual(plan["reason"], "redispatch superseded subset")
        self.assertEqual([spec["task_id"] for spec in plan["specs"]],
                         ["wf-srp-ctl-impl-B-r2"])
        spec = plan["specs"][0]
        self.assertEqual(spec["goal"], "实现 wf-srp-ctl-impl-B")
        self.assertEqual(spec["acceptance"], ["AC-wf-srp-ctl-impl-B",
                                              *direct_dispatch_planner
                                              .GENERIC_ACCEPTANCE])
        self.assertEqual(spec["integration_mode"], "git")
        self.assertIn("前端错误提示缺失", spec["prompt"])
        self.assertIn("wf-srp-ctl-impl-A", spec["prompt"])
        self.assertIn("禁止扩大修改范围", spec["prompt"])
        # 保留任务绝不进入补派集合。
        self.assertNotIn("wf-srp-ctl-impl-A-r2",
                         [s["task_id"] for s in plan["specs"]])

    def test_gate_inventory_lists_current_heads_only(self):
        """Case 13:门禁清单只含当前谱系头,历史已作废任务绝不出现。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()
        self.seed_impl("wf-srp-ctl-impl-B-r2", status="working")

        block = _ctl._selective_gate_inventory_block(WF, self._wf_cfg())
        listed = {
            line.split("task_id:", 1)[1].strip()
            for line in block.splitlines()
            if "task_id:" in line
        }
        self.assertEqual(
            listed, {"wf-srp-ctl-impl-A", "wf-srp-ctl-impl-B-r2",
                     "wf-srp-ctl-impl-C"})
        # 未开启策略的 workflow 不注入清单(契约保持 legacy)。
        self.assertIsNone(
            _ctl._selective_gate_inventory_block(WF, self._legacy_wf_cfg()))

    def test_real_git_replacement_builds_on_preserved_work(self):
        """Case 14:replacement 必须建立在含保留成果的当前上下文之上。"""
        _git(self.repo, "checkout", "-q", "-b", "impl")
        self.write_repo("a.txt", "A work")
        self.commit_repo("A work")
        self.write_repo("c.txt", "C work")
        sha = self.commit_repo("C work")

        self.freeze(sha)
        self.seed_three_impl(branch="impl")
        self.seed_gate(affected=["wf-srp-ctl-impl-B"], sha=sha)

        self.run_fix_loop()

        notes = _ctl._selective_redispatch_blocker_notes(WF, "implementation")
        plan = direct_dispatch_planner.plan_stage_dispatch(
            WF, self._impl_node(), self.store.list_tasks(), "selective replan",
            context_branch="impl", redispatch_blocker_notes=notes,
        )
        spec = plan["specs"][0]
        self.assertEqual(spec["onto_branch"], "impl")
        # 保留任务(A/C)的成果仍在 replacement 的基线上。
        for path, expected in (("a.txt", "A work"), ("c.txt", "C work")):
            shown = _git(self.repo, "show", f"impl:{path}").stdout.strip()
            self.assertEqual(shown, expected)

    def test_selective_path_writes_no_foreign_facts(self):
        """Case 15:selective 路径只写一条 selective 事实,不碰 #107/#108 台账。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])

        self.run_fix_loop()

        self.assertEqual(len(self.selective_facts()), 1)
        freezes = facts.list_candidate_frozen_events(WF, db_path=self.db)
        self.assertEqual(len(freezes), 1)
        self.assertEqual(
            facts.list_reverification_decisions(WF, db_path=self.db), [])

    def test_verdict_file_is_the_only_attribution_source(self):
        """Case 16:归因只读结论文件结构化字段,屏幕文本永不作为来源。"""
        task = {"task_id": "wf-srp-ctl-review-1"}
        path = self.verdicts / "wf-srp-ctl-review-1.json"

        path.write_text(json.dumps({
            "verdict": "blocked", "note": "n",
            "affected_task_ids": ["wf-srp-ctl-impl-B"],
        }), encoding="utf-8")
        self.assertEqual(_ctl._verdict_affected_task_ids(task),
                         ["wf-srp-ctl-impl-B"])

        path.write_text(json.dumps({
            "verdict": "blocked", "note": "n", "affected_task_ids": [],
        }), encoding="utf-8")
        self.assertEqual(_ctl._verdict_affected_task_ids(task), [])

        path.write_text(json.dumps({"verdict": "pass", "note": "n"}),
                        encoding="utf-8")
        self.assertIsNone(_ctl._verdict_affected_task_ids(task))

        path.write_text(json.dumps({
            "verdict": "blocked", "note": "n",
            "affected_task_ids": "wf-srp-ctl-impl-B",
        }), encoding="utf-8")
        self.assertIsNone(_ctl._verdict_affected_task_ids(task))

        path.unlink()
        self.assertIsNone(_ctl._verdict_affected_task_ids(task))

    # -- S6 review-round fixes --------------------------------------------

    def test_policy_retry_node_mismatch_falls_back(self):
        """门禁解析出的 retry_node 与策略声明不一致 → 整体回退 legacy。

        目标按策略节点校验、作废按门禁节点执行时,「保留」过滤器永不命中:
        未被点名的 A/C 会被一并作废,却留下一条自称 selective 的事实。
        回退后走 legacy 语义:实现节点自身不进作废范围(由总指挥整体返工)。
        """
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        cfg = self._wf_cfg()
        cfg["selective_replan"]["retry_node"] = "plan"
        before = {t: self.snapshot(t) for t in self.impl_tasks()}

        output = io.StringIO()
        with redirect_stdout(output):
            _ctl.handle_fix_loop(
                WF, "review", {"retry_node": "implementation", "max_loops": 3},
                cfg)

        self.assertIn("retry_node_policy_mismatch", output.getvalue())
        for task_id, snap in before.items():
            self.assertEqual(self.snapshot(task_id)["status"], snap["status"])
        self.assertEqual(self.selective_facts(), [])
        self.assertNotIn("mode", self.latch_state() or {})

    def test_dangling_superseded_by_does_not_stall_forever(self):
        """superseded_by 指向一个从未创建的替代者 → 不得永远等待补派。

        「等待」谓词必须与补派管线同源:补派管线给不出候选的谱系,
        等待它就会把节点永久钉在未完成。
        """
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()
        self.assertTrue(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))

        # 维修时把 B 标记为「已被 B-r2 取代」,但 B-r2 从未创建。
        record = dict(self.store.get_task("wf-srp-ctl-impl-B"))
        record["superseded_by"] = "wf-srp-ctl-impl-B-r2"
        self.store.save_task(record)

        self.assertFalse(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))

    def test_replacement_present_ends_awaiting_state(self):
        """替代任务落地后不再等待(否则节点永远回不到完成态)。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()
        self.assertTrue(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))

        self.seed_impl("wf-srp-ctl-impl-B-r2", status="dispatched")

        self.assertFalse(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))

    def test_awaiting_window_only_redispatches_targeted_lineage(self):
        """等待窗口内节点被重开,但直派绝不给全量重开、也不回落总指挥。

        sweep 每轮清 stage-advance 会绕开 notified 闩,所以「重开」的安全
        边界不在闩上,而在判据同源:只要 AWAIT 为真,补派管线就必须只产出
        被点名谱系的 `-rN`;窗口一关,直派转为 wait(不唤醒总指挥)。
        这条不变量把「每 2s 全量重派 / 每 2s 叫醒总指挥」的病理排除掉。
        """
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()
        self.assertTrue(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))

        plan = direct_dispatch_planner.plan_stage_dispatch(
            WF, self._impl_node(), self.store.list_tasks(), "需求:实现功能",
        )
        self.assertEqual(plan["reason"], "redispatch superseded subset")
        self.assertEqual(
            [spec["task_id"] for spec in plan["specs"]],
            ["wf-srp-ctl-impl-B-r2"],
        )

        # 反向:替代任务落地 → 窗口关闭 → 直派不再派发任何东西。
        self.seed_impl("wf-srp-ctl-impl-B-r2", status="dispatched")
        self.assertFalse(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))
        plan2 = direct_dispatch_planner.plan_stage_dispatch(
            WF, self._impl_node(), self.store.list_tasks(), "需求:实现功能",
        )
        self.assertEqual(plan2["mode"], "wait")
        self.assertEqual(plan2["specs"], [])

    def _seed_completed_upstream(self):
        for node in ("requirements", "plan"):
            self.save_task(
                task_id=f"{WF}-{node}-1", node=node, stage=node,
                status="cleaned", updated_at=_tick(),
            )

    def test_sweep_reopens_node_while_replacement_pending(self):
        """Case 17:真实 sweep——补派候选未落地时节点必须被判为未完成。

        A/C 仍 completed、B 已 superseded 时 is_node_complete 会把
        implementation 误判为完成;若 sweep 不纠正,替代任务永远没有
        补派窗口,工作流会带着一个被作废的谱系直接收口。
        """
        self.store.save_workflow({"workflow_id": WF, "status": "running",
                                  "config": self._wf_cfg()})
        self.freeze()
        self._seed_completed_upstream()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()
        self.queue.clear()

        output = io.StringIO()
        with redirect_stdout(output):
            _ctl.check_workflow_stage_advance(WF)

        self.assertIn("[SELECTIVE REPLAN AWAIT]", output.getvalue())
        queued = [
            item for item in self.queue
            if item.get("kind") == "stage_advance"
            and item.get("node_id") == "implementation"
        ]
        self.assertTrue(queued, "implementation 必须重新进入就绪节点流程")
        self.assertEqual(self.store.list_workflows()[0]["status"], "running")

    def test_sweep_releases_node_once_replacement_lands(self):
        """Case 18:替代任务落地后 sweep 不再钉住节点(反向对照)。"""
        self.freeze()
        self._seed_completed_upstream()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        self.run_fix_loop()
        self.seed_impl("wf-srp-ctl-impl-B-r2", status="dispatched",
                       branch="main")
        self.queue.clear()

        output = io.StringIO()
        with redirect_stdout(output):
            _ctl.check_workflow_stage_advance(WF)

        self.assertNotIn("[SELECTIVE REPLAN AWAIT]", output.getvalue())

    def test_selective_message_forbids_full_stage_fix_task(self):
        """Case 19:selective 通知绝不指引总指挥重做整个实现阶段。

        归因已明确到谱系,替代任务由 Controller 补派;再发 legacy 的
        launch 骨架会让两套修复同时落在同一分支上,并把范围扩大到被
        明确保留的任务。
        """
        item = {
            "kind": "fix_loop",
            "workflow_id": WF,
            "gate_stage": "review",
            "retry_node": "implementation",
            "blockers": [{"task_id": "wf-srp-ctl-review-1", "note": "错误提示缺失"}],
            "invalidated": ["wf-srp-ctl-impl-B"],
            "loop_count": 1,
            "max_loops": 3,
            "suggested_branch": "main",
            "mode": "selective",
            "target_lineage_roots": ["wf-srp-ctl-impl-B"],
        }

        text = _ctl.build_fix_loop_message(item, "proj")

        self.assertIn("HERDR_CONTROLLER_SELECTIVE_REPLAN_EVENT", text)
        self.assertIn("wf-srp-ctl-impl-B", text)
        self.assertIn("错误提示缺失", text)
        self.assertIn("⛔ 禁止", text)
        # 不给出可照抄的全量返工派发骨架。
        self.assertNotIn("herdr-task launch", text)

    def test_legacy_message_unchanged_for_legacy_items(self):
        """未带 mode 的 fix_loop 事件仍走原消息(legacy 逐字节等价)。"""
        item = {
            "kind": "fix_loop",
            "workflow_id": WF,
            "gate_stage": "review",
            "retry_node": "implementation",
            "blockers": [],
            "invalidated": ["x"],
            "loop_count": 1,
            "max_loops": 3,
            "suggested_branch": "main",
        }

        text = _ctl.build_fix_loop_message(item, "proj")

        self.assertIn("HERDR_CONTROLLER_FIX_LOOP_EVENT", text)
        self.assertIn("--stage implementation", text)

    def test_unreadable_stored_fact_falls_back(self):
        """exists 但读不回事实 → 不得用本次重算结果顶替权威。

        重放场景:同一 episode 的事实已落库,但读回失败。此时若拿本次
        重算的 targets 去作废,等于用「未经持久化确认」的结论改写事实。
        """
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        before = {t: self.snapshot(t) for t in self.impl_tasks()}
        for name, ret in (
            ("record_selective_replan_decision", {"status": "exists"}),
            ("find_selective_replan_decision", None),
        ):
            patcher = patch.object(_ctl.scheduler_facts_store, name,
                                   return_value=ret)
            patcher.start()
            self.addCleanup(patcher.stop)

        output = io.StringIO()
        with redirect_stdout(output):
            _ctl.handle_fix_loop(
                WF, "review", {"retry_node": "implementation", "max_loops": 3},
                self._wf_cfg())

        self.assertIn("stored_fact_unreadable", output.getvalue())
        for task_id, snap in before.items():
            self.assertEqual(self.snapshot(task_id)["status"], snap["status"])

    def test_rejection_log_surfaces_identity_mismatch(self):
        """同一 episode 换 targets → 拒绝原因必须可见(而非打印计划自身 reason)。"""
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(affected=["wf-srp-ctl-impl-B"])
        patcher = patch.object(
            _ctl.scheduler_facts_store, "record_selective_replan_decision",
            lambda wf, plan: {"status": "rejected",
                              "reason": "identity_content_mismatch"})
        patcher.start()
        self.addCleanup(patcher.stop)

        output = io.StringIO()
        with redirect_stdout(output):
            targets, _plan = _ctl._resolve_selective_replan(
                WF, "review", "implementation", self._wf_cfg(),
                [self.store.get_task("wf-srp-ctl-review-1")],
                self.store.list_tasks(),
            )

        self.assertIn("identity_content_mismatch", output.getvalue())
        self.assertIsNone(targets)

    def _impl_node(self):
        return {
            "id": "implementation",
            "label": "实现",
            "purpose": "实现需求",
            "required_outputs": ["代码"],
            "rules": [],
            "default_task_type": "feat",
            "default_integration_mode": "git",
        }

    # -- P1 regressions (PR #110 review round-2) --------------------------

    def test_p1_partial_invalidation_defers_then_restart_recovers(self):
        """P1-1: targets=[B,C], B ok + C fail → 无 latch/通知,重启后复用 Fact 续作 C。

        Fact 先持久化 [B,C];首轮 C supersede 失败时门禁/下游一律不动,
        不写全量 latch;第二轮基于已落盘 Fact(不重新决定 targets)续作 C,
        最终 B-r2/C-r2 各一个,A 保留,latch 可解除。
        """
        self.freeze()
        self.seed_three_impl()
        before_a = self.snapshot("wf-srp-ctl-impl-A")
        self.seed_gate(affected=["wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C"])

        orig_supersede = self._apply_supersede

        def _fail_c(task_id):
            if task_id == "wf-srp-ctl-impl-C":
                return subprocess.CompletedProcess([], 1, "", "boom")
            return orig_supersede(task_id)

        self._apply_supersede = _fail_c
        try:
            self.run_fix_loop()
        finally:
            self._apply_supersede = orig_supersede

        # 首轮部分失败:Fact 已落盘, B 已作废, C 未动,门禁仍存活,无 latch/通知。
        stored = facts.latest_selective_replan_for_node(
            WF, "implementation", db_path=self.db)
        self.assertIsNotNone(stored)
        self.assertEqual(
            stored["target_task_ids"],
            ["wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C"])
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-impl-B")["status"], "superseded")
        # C 的 supersede 失败,但其 finalize 已将其规范化为 cleaned(仍存活,
        # 非 superseded);第二轮重试时直接 supersede 即可,无需再次 finalize。
        self.assertIn(
            self.store.get_task("wf-srp-ctl-impl-C")["status"],
            ("completed", "cleaned"))
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-review-1")["status"], "completed")
        latch = self.latch_state()
        self.assertTrue(latch is None or "mode" not in (latch or {}))
        self.assertEqual(
            [i for i in self.queue if i.get("kind") == "fix_loop"], [])

        # 重启后重算必须复用已落盘 Fact,而非因 B 已非谱系头整体 fallback。
        reused_targets, reused_plan = _ctl._resolve_selective_replan(
            WF, "review", "implementation", self._wf_cfg(),
            [self.store.get_task("wf-srp-ctl-review-1")],
            self.store.list_tasks())
        self.assertEqual(
            reused_targets,
            ["wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C"])
        self.assertEqual(reused_plan["replan_id"], stored["replan_id"])

        # 第二轮(故障已除):C 续作,门禁作废,latch 全量,补派 B-r2/C-r2。
        self.run_fix_loop()
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-impl-C")["status"], "superseded")
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-review-1")["status"], "superseded")
        self.assertEqual(self.snapshot("wf-srp-ctl-impl-A"), before_a)
        latch = self.latch_state()
        self.assertEqual(latch["mode"], "selective")
        self.assertEqual(
            sorted(latch["target_lineage_roots"]),
            ["wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C"])
        self.assertEqual(len(self.selective_facts()), 1)

        notes = _ctl._selective_redispatch_blocker_notes(WF, "implementation")
        plan = direct_dispatch_planner.plan_stage_dispatch(
            WF, self._impl_node(), self.store.list_tasks(), "selective replan",
            redispatch_blocker_notes=notes)
        self.assertEqual(
            sorted(s["task_id"] for s in plan["specs"]),
            ["wf-srp-ctl-impl-B-r2", "wf-srp-ctl-impl-C-r2"])

        # 目标谱系真正完成 → latch 解除;保留任务更新永不放行(见 Case 11)。
        self.assertTrue(_ctl._fix_loop_latch_blocks(WF, "implementation"))
        self.seed_impl("wf-srp-ctl-impl-B-r2", status="completed")
        self.assertTrue(_ctl._fix_loop_latch_blocks(WF, "implementation"))
        self.seed_impl("wf-srp-ctl-impl-C-r2", status="completed")
        self.assertFalse(_ctl._fix_loop_latch_blocks(WF, "implementation"))

    def _seed_dual_blocked_gates(self):
        self.freeze()
        self.seed_three_impl()
        self.seed_gate(
            affected=["wf-srp-ctl-impl-B"], sha=SHA_A, version=7,
            task_id="wf-srp-ctl-test-1", node="test")
        self.seed_gate(
            affected=["wf-srp-ctl-impl-C"], sha=SHA_A, version=7,
            task_id="wf-srp-ctl-review-1", node="review")

    def _assert_dual_gate_merged(self):
        before_a = self.snapshot("wf-srp-ctl-impl-A")
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-impl-B")["status"], "superseded")
        self.assertEqual(
            self.store.get_task("wf-srp-ctl-impl-C")["status"], "superseded")
        self.assertEqual(self.snapshot("wf-srp-ctl-impl-A"), before_a)
        # 两条单门禁事实各 persist 其归因,awaiting/notes 取并集。
        by_gate = {
            (e["payload"].get("gate_task_id")): e["payload"]
            for e in self.selective_facts()
            if e["payload"].get("mode") == "selective"
        }
        self.assertEqual(
            by_gate["wf-srp-ctl-test-1"]["target_task_ids"],
            ["wf-srp-ctl-impl-B"])
        self.assertEqual(
            by_gate["wf-srp-ctl-review-1"]["target_task_ids"],
            ["wf-srp-ctl-impl-C"])
        self.assertTrue(
            _ctl._selective_replan_awaiting_redispatch(WF, "implementation"))
        notes = _ctl._selective_redispatch_blocker_notes(WF, "implementation")
        self.assertIn("wf-srp-ctl-impl-B", notes)
        self.assertIn("wf-srp-ctl-impl-C", notes)
        plan = direct_dispatch_planner.plan_stage_dispatch(
            WF, self._impl_node(), self.store.list_tasks(), "selective replan",
            redispatch_blocker_notes=notes)
        self.assertEqual(
            sorted(s["task_id"] for s in plan["specs"]),
            ["wf-srp-ctl-impl-B-r2", "wf-srp-ctl-impl-C-r2"])
        latch = self.latch_state()
        self.assertEqual(latch["mode"], "selective")
        self.assertEqual(
            sorted(latch["target_lineage_roots"]),
            ["wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C"])

    def test_p1_dual_gate_merge_test_then_review(self):
        """P1-2: test→[B] + review→[C] 同轮 blocked,先处理 test 再 review。"""
        self._seed_dual_blocked_gates()
        self.run_fix_loop(gate_node="test")
        self.run_fix_loop(gate_node="review")
        self._assert_dual_gate_merged()

    def test_p1_dual_gate_merge_review_then_test(self):
        """P1-2:顺序无关,先 review 再 test 结果一致。"""
        self._seed_dual_blocked_gates()
        self.run_fix_loop(gate_node="review")
        self.run_fix_loop(gate_node="test")
        self._assert_dual_gate_merged()

    def test_p1_dual_gate_real_sweep_single_pass(self):
        """Unknown execution/run identity preserves both gates and merged repair scope.

        A sweep registers the durable obligation before DAG processing. It must
        not invalidate or dispatch until the identity boundary is established.
        Direct handle_fix_loop tests above independently cover selective replan.
        """
        # Give both gates the same real candidate, while leaving run identity
        # unknown so recovery must preserve the snapshot for human resolution.
        real_sha = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.store.save_workflow({"workflow_id": WF, "status": "running",
                                  "config": self._wf_cfg()})
        self.freeze(real_sha)
        self._seed_completed_upstream()
        self.seed_three_impl()
        self.seed_gate(
            affected=["wf-srp-ctl-impl-B"], sha=real_sha, version=7,
            task_id="wf-srp-ctl-test-1", node="test")
        self.seed_gate(
            affected=["wf-srp-ctl-impl-C"], sha=real_sha, version=7,
            task_id="wf-srp-ctl-review-1", node="review")
        before = {tid: self.snapshot(tid) for tid in (
            "wf-srp-ctl-impl-A", "wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C",
            "wf-srp-ctl-test-1", "wf-srp-ctl-review-1")}
        self.queue.clear()

        _ctl.check_workflow_stage_advance(WF)

        from herdr.recovery_store import list_operations
        operations = list_operations(self.db, WF)
        self.assertEqual(len(operations), 1, operations)
        operation = operations[0]
        self.assertEqual(operation["status"], "waiting_human")
        self.assertEqual(operation["payload"]["reason"], "identity_unknown")
        self.assertEqual(operation["payload"]["task_ids"],
                         ["wf-srp-ctl-review-1", "wf-srp-ctl-test-1"])
        self.assertEqual(operation["payload"]["affected_task_ids"],
                         ["wf-srp-ctl-impl-B", "wf-srp-ctl-impl-C"])
        for tid, snapshot in before.items():
            self.assertEqual(self.snapshot(tid), snapshot)
        self.assertEqual(self.queue, [])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.selective_facts(), [])
        _ctl.check_workflow_stage_advance(WF)
        repeated = list_operations(self.db, WF)
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["id"], operation["id"])
        self.assertEqual(repeated[0]["version"], operation["version"])
        for tid, snapshot in before.items():
            self.assertEqual(self.snapshot(tid), snapshot)
        self.assertEqual(self.queue, [])
        self.assertEqual(self.launches, [])

    def test_p1_replacement_baseline_is_frozen_candidate(self):
        """P1-3: B-r2 基线必须是当前冻结 Candidate,而非 B 旧分支/plan 分支。

        A/B/C 各自独立分支, Candidate 为集成后的 A+B+C。
        断言 dispatch 的 --onto 即 Candidate 分支,且 Candidate 树中
        A/C 文件俱在、HEAD 即冻结 SHA。
        """
        base = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        for branch, rel, text in (
            ("branch-A", "a.txt", "A work"),
            ("branch-B", "b.txt", "B work"),
            ("branch-C", "c.txt", "C work"),
        ):
            _git(self.repo, "checkout", "-q", "-b", branch, base)
            self.write_repo(rel, text)
            self.commit_repo(f"{branch} work")
        _git(self.repo, "checkout", "-q", "-b", "candidate", base)
        for rel, text in (("a.txt", "A work"), ("b.txt", "B work"), ("c.txt", "C work")):
            self.write_repo(rel, text)
        candidate_sha = self.commit_repo("candidate A+B+C")

        self.freeze(candidate_sha, branch="candidate")
        self.seed_impl("wf-srp-ctl-impl-A", branch="branch-A")
        self.seed_impl("wf-srp-ctl-impl-B", branch="branch-B")
        self.seed_impl("wf-srp-ctl-impl-C", branch="branch-C")
        self.seed_gate(affected=["wf-srp-ctl-impl-B"], sha=candidate_sha)
        self.run_fix_loop()

        fact = facts.latest_selective_replan_for_node(
            WF, "implementation", db_path=self.db)
        self.assertIsNotNone(fact)
        baseline = srp.selective_replacement_baseline(
            fact, candidate_sha, "candidate")
        self.assertEqual(
            baseline, {"onto_branch": "candidate", "candidate_sha": candidate_sha})
        # 轮换/缺分支一律 fail-closed,不猜。
        self.assertIsNone(
            srp.selective_replacement_baseline(fact, SHA_A, "candidate"))
        self.assertIsNone(
            srp.selective_replacement_baseline(fact, candidate_sha, ""))

        item = {
            "kind": "stage_advance", "workflow_id": WF,
            "stage": "plan", "node_id": "implementation",
            "next_stage": "implementation",
            "node": dict(self._impl_node(), depends_on=["plan"]),
        }
        self.assertTrue(_ctl.try_direct_stage_advance(item))
        onto_flags = [
            argv[i + 1]
            for argv in self.launches
            for i, token in enumerate(argv[:-1])
            if token == "--onto"
        ]
        self.assertTrue(onto_flags, "replacement must carry --onto")
        self.assertTrue(all(o == "candidate" for o in onto_flags))
        self.assertNotIn("branch-B", onto_flags)
        sha_flags = [
            argv[i + 1]
            for argv in self.launches
            for i, token in enumerate(argv[:-1])
            if token == "--candidate-sha"
        ]
        self.assertTrue(all(s == candidate_sha for s in sha_flags))
        for rel, expected in (("a.txt", "A work"), ("c.txt", "C work")):
            shown = _git(self.repo, "show", f"candidate:{rel}").stdout.strip()
            self.assertEqual(shown, expected)
        head = _git(self.repo, "rev-parse", "candidate").stdout.strip()
        self.assertEqual(head, candidate_sha)


if __name__ == "__main__":
    unittest.main()
