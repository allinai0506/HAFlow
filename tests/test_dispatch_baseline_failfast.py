"""派发前基线新鲜度 fail-fast（方案 A + 缺陷 #5 同源时序错配）。

契约（交接 `docs/handoffs/herdr-dev-baseline-drift-handoff.md` §10）：
- 当候选基线落后 `dev`（`dev` 不是候选的祖先）时，`herdr-task launch`
  必须在创建任何 clone / Pane 之前拒绝，并指出 dev 前进了 N 个提交、
  给出处置（rebase / 指定新基线），且不产生孤儿资源。
- 未知情况（ref 不可解析、git 失败）fail-open 照常派发：
  派发前门禁只是 fail-fast 优化，正确性后栏仍由 FR-6.2
  系列校验 fail-closed 兜底（参考 `_dispatch_candidate_ready` 的未知策略）。
- 新鲜分支（无 candidate 声明、无 --onto）不受此门禁影响：
  它们 checkout 的是实时 `origin/base_branch`，天然跟踪 dev。

测试分三层：
1. `decide_baseline_failfast` 纯判定（无 git、无 I/O）；
2. `probe_claim_freshness` 真 git fixture（tempfile 仓库，不碰生产状态）；
3. `_launch_task` 源码顺序护栏：预检调用必须先于一切资源创建
   （`ensure_stage_topology` / `acquire_pane_for_task` / worker 子进程），
   手法同 `test_worker_baseline_anchor.py::TestCapturePrecedesContextInSource`。
"""

import ast
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCH_PATH = ROOT / "bin" / "herdr-task"

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True, capture_output=True, check=True,
    ).stdout.strip()


class GitFixture(unittest.TestCase):
    """source 仓库：dev 上 1 个提交，打桩 claim；随后 dev 再前进 2 个提交。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "dev")
        git(self.source, "config", "user.name", "Test")
        git(self.source, "config", "user.email", "test@test.com")
        git(self.source, "config", "commit.gpgsign", "false")
        (self.source / "base.txt").write_text("base\n")
        git(self.source, "add", "base.txt")
        git(self.source, "commit", "-m", "stale claim point")
        self.stale_claim = git(self.source, "rev-parse", "HEAD")
        for i in (1, 2):
            (self.source / f"dev{i}.txt").write_text(f"dev advance {i}\n")
            git(self.source, "add", f"dev{i}.txt")
            git(self.source, "commit", "-m", f"dev advance {i}")
        self.dev_tip = git(self.source, "rev-parse", "HEAD")
        assert FULL_SHA.match(self.stale_claim)
        assert FULL_SHA.match(self.dev_tip)
        assert self.stale_claim != self.dev_tip

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestDecideBaselineFailfast(unittest.TestCase):
    """纯判定：落后 dev 即拒，持平/领先即放，未知即放（fail-open）。"""

    def test_stale_claim_two_behind_is_refused(self):
        from herdr.launch_baseline import decide_baseline_failfast
        verdict = decide_baseline_failfast(
            dev_tip="d" * 40, claim_sha="c" * 40, behind_by=2,
        )
        self.assertTrue(verdict["refused"])
        self.assertEqual(verdict["code"], "baseline_stale")
        self.assertEqual(verdict["behind_by"], 2)
        self.assertIn("2", verdict["message"])
        self.assertIn("rebase", verdict["remediation"].lower())

    def test_claim_equal_to_dev_tip_passes(self):
        from herdr.launch_baseline import decide_baseline_failfast
        sha = "a" * 40
        verdict = decide_baseline_failfast(
            dev_tip=sha, claim_sha=sha, behind_by=0,
        )
        self.assertFalse(verdict["refused"])

    def test_claim_ahead_of_dev_passes(self):
        from herdr.launch_baseline import decide_baseline_failfast
        verdict = decide_baseline_failfast(
            dev_tip="d" * 40, claim_sha="e" * 40, behind_by=0,
        )
        self.assertFalse(verdict["refused"])

    def test_unknown_git_state_fails_open(self):
        from herdr.launch_baseline import decide_baseline_failfast
        verdict = decide_baseline_failfast(
            dev_tip="", claim_sha="c" * 40, behind_by=None,
        )
        self.assertFalse(verdict["refused"])
        self.assertEqual(verdict["code"], "baseline_unknown")


class TestProbeClaimFreshness(GitFixture):
    """真 git：stale claim 被拒且 behind==2；新鲜 claim 放行；未知放行。"""

    def test_stale_claim_reports_two_behind(self):
        from herdr.launch_baseline import probe_claim_freshness
        result = probe_claim_freshness(
            str(self.source), base_branch="dev", claim_sha=self.stale_claim,
        )
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["behind_by"], 2)
        self.assertEqual(result["dev_tip"], self.dev_tip)
        self.assertEqual(result["claim_sha"], self.stale_claim)

    def test_fresh_claim_at_dev_tip_is_fresh(self):
        from herdr.launch_baseline import probe_claim_freshness
        result = probe_claim_freshness(
            str(self.source), base_branch="dev", claim_sha=self.dev_tip,
        )
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["behind_by"], 0)

    def test_unresolvable_claim_is_unknown_not_stale(self):
        from herdr.launch_baseline import probe_claim_freshness
        result = probe_claim_freshness(
            str(self.source), base_branch="dev",
            claim_sha="f" * 40,
        )
        self.assertEqual(result["status"], "unknown")

    def test_no_claim_is_skipped_not_fresh(self):
        from herdr.launch_baseline import probe_claim_freshness
        result = probe_claim_freshness(str(self.source), base_branch="dev")
        self.assertEqual(result["status"], "skipped")

    def test_missing_onto_branch_is_unknown_not_stale(self):
        from herdr.launch_baseline import probe_claim_freshness
        result = probe_claim_freshness(
            str(self.source), base_branch="dev",
            onto_branch="agent/ghost/does-not-exist",
        )
        self.assertEqual(result["status"], "unknown")

    def test_onto_branch_head_is_used_as_claim(self):
        from herdr.launch_baseline import probe_claim_freshness
        git(self.source, "checkout", "-b", "agent/x/fix-probe", self.stale_claim)
        git(self.source, "checkout", "dev")
        result = probe_claim_freshness(
            str(self.source), base_branch="dev",
            onto_branch="agent/x/fix-probe",
        )
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["behind_by"], 2)
        self.assertEqual(result["claim_sha"], self.stale_claim)


class TestLaunchOrderGuard(unittest.TestCase):
    """结构性护栏：`_launch_task` 内预检必须先于一切资源创建。

    若将来有人把校验移回 worker 之后（重蹈 #5），此测试变红。
    """

    def _launch_fn(self):
        tree = ast.parse(LAUNCH_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_launch_task":
                return node
        self.fail("_launch_task not found")

    def _first_call_line(self, fn, name):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == name:
                return node.lineno
        return None

    def test_preflight_precedes_all_resource_creation(self):
        fn = self._launch_fn()
        gate = self._first_call_line(fn, "_preflight_baseline_freshness")
        self.assertIsNotNone(
            gate, "_launch_task 必须调用 _preflight_baseline_freshness",
        )
        for resource_call in (
            "ensure_stage_topology",
            "acquire_pane_for_task",
        ):
            line = self._first_call_line(fn, resource_call)
            if line is not None:
                self.assertLess(
                    gate, line,
                    f"_preflight_baseline_freshness 必须先于 {resource_call}",
                )


if __name__ == "__main__":
    unittest.main()
