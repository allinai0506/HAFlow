"""T2 验收：worker launch 时固化不可变锚点 baseline_commit（AC2-1..AC2-4）。

契约（docs/plan-arch.md §4.1.1-B / §6-T2）：
- 采集点：分支检出（新建任务分支 或 --onto）**之后**、`.agent-task-context`
  落盘**之前**；锚点走 `git rev-parse HEAD` 的 full 40-hex，禁用 `--short`。
- `worker_result` 回传 `baseline_commit` + `onto_branch`；launch 侧落库。

为什么顺序是铁律：早一步会把 `--onto` 分支的既有提交算进本任务区间（B1），
晚一步会把 `.agent-task-context` 自己算进区间。
"""

import ast
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
WORKER_PATH = ROOT / "services" / "herdr-worker.py"
LAUNCH_PATH = ROOT / "bin" / "herdr-task"

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def load_worker():
    spec = importlib.util.spec_from_loader(
        "herdr_worker_anchor_test",
        importlib.machinery.SourceFileLoader("herdr_worker_anchor_test", str(WORKER_PATH)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    """Run git and return stdout (stripped); raises on non-zero rc."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def git_rc(repo, *args):
    """Run git and return only the exit code (for predicate subcommands)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    ).returncode


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.name", "Test")
        git(self.source, "config", "user.email", "test@test.com")
        (self.source / "base.txt").write_text("base\n")
        git(self.source, "add", "base.txt")
        git(self.source, "commit", "-m", "initial commit")
        self.main_tip = git(self.source, "rev-parse", "HEAD")

        self.clone_root = self.tmp / "clones"
        self.clone_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_onto_origin(self):
        """造出 origin，并在其上准备带既有提交的 PR 分支 feat/wip。"""
        origin = self.tmp / "origin"
        subprocess.run(
            ["git", "clone", "--bare", str(self.source), str(origin)],
            check=True,
            capture_output=True,
        )
        git(self.source, "remote", "add", "origin", str(origin))
        git(self.source, "checkout", "-b", "feat/wip")
        (self.source / "pr.txt").write_text("pr only\n")
        git(self.source, "add", "pr.txt")
        git(self.source, "commit", "-m", "pr existing commit")
        self.pr_tip = git(self.source, "rev-parse", "HEAD")
        git(self.source, "push", "origin", "feat/wip")
        git(self.source, "push", "origin", "main")
        git(self.source, "checkout", "main")
        return origin


class TestCaptureHeadSha(GitFixture):
    def test_capture_returns_full_40_hex_head(self):
        worker = load_worker()
        with patch.object(worker, "CLONE_ROOT", self.clone_root), \
             patch.object(worker, "_registered_tasks", return_value=[]):
            clone = worker.create_clone(str(self.source), "anchor-new-task")
            anchor = worker.capture_head_sha(clone)

        self.assertEqual(len(anchor), 40)
        self.assertTrue(FULL_SHA.match(anchor), anchor)
        self.assertEqual(anchor, git(clone, "rev-parse", "HEAD"))
        # 禁止 --short：短 sha 是 40-hex 的前缀，但长度不等
        self.assertNotEqual(anchor, git(clone, "rev-parse", "--short", "HEAD"))

    def test_capture_fails_closed_outside_git_repo(self):
        worker = load_worker()
        plain = self.tmp / "plain-dir"
        plain.mkdir()
        with self.assertRaises(RuntimeError):
            worker.capture_head_sha(plain)

    def test_capture_rejects_short_sha(self):
        worker = load_worker()
        short = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self.main_tip[:7] + "\n", stderr=""
        )
        with patch.object(worker.subprocess, "run", return_value=short), \
             self.assertRaises(RuntimeError) as ctx:
            worker.capture_head_sha("/tmp/any-clone")
        self.assertIn("40-hex", str(ctx.exception))

    def test_capture_argv_never_requests_short_sha(self):
        worker = load_worker()
        full = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self.main_tip + "\n", stderr=""
        )
        with patch.object(worker.subprocess, "run", return_value=full) as run:
            anchor = worker.capture_head_sha("/tmp/any-clone")

        self.assertEqual(anchor, self.main_tip)
        self.assertEqual(
            run.call_args.args[0],
            ["git", "-C", "/tmp/any-clone", "rev-parse", "HEAD"],
        )


class TestNewBranchAnchor(GitFixture):
    """AC2-1：新建分支任务的锚点 == 检出后的 HEAD，区间为空。"""

    def test_anchor_equals_head_after_task_branch_checkout(self):
        worker = load_worker()
        with patch.object(worker, "CLONE_ROOT", self.clone_root), \
             patch.object(worker, "_registered_tasks", return_value=[]):
            clone = worker.create_clone(str(self.source), "anchor-new-task")
            branch = worker.create_task_branch(
                clone, "anchor-new-task", "claude", "feat", "main"
            )
            anchor = worker.capture_head_sha(clone)

        self.assertEqual(branch, "agent/claude/feat-anchor-new-task")
        self.assertEqual(git(clone, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertEqual(anchor, git(clone, "rev-parse", "HEAD"))
        self.assertEqual(anchor, self.main_tip)
        self.assertEqual(git(clone, "rev-list", "--count", f"{anchor}..HEAD"), "0")


class TestOntoBranchAnchor(GitFixture):
    """AC2-2：--onto 任务的锚点落在 onto 检出之后 ⇒ PR 既有提交不在区间内。"""

    def test_onto_existing_commits_fall_outside_anchor_range(self):
        self.make_onto_origin()
        worker = load_worker()
        with patch.object(worker, "CLONE_ROOT", self.clone_root), \
             patch.object(worker, "_registered_tasks", return_value=[]):
            clone = worker.create_clone(str(self.source), "anchor-onto-task")
            branch = worker.checkout_onto_branch(clone, "feat/wip")
            anchor = worker.capture_head_sha(clone)

        self.assertEqual(branch, "feat/wip")
        self.assertEqual(anchor, self.pr_tip)

        # 区间 baseline_commit..HEAD 为空 —— PR 分支的既有提交被排除在区间外
        self.assertEqual(git(clone, "rev-list", "--count", f"{anchor}..HEAD"), "0")
        self.assertNotIn(self.pr_tip, git(clone, "rev-list", f"{anchor}..HEAD").split())
        # PR 提交是锚点的祖先（即"已在锚点之下"），故不属于本任务成果
        self.assertEqual(git_rc(clone, "merge-base", "--is-ancestor", self.pr_tip, anchor), 0)

        # B1 铁证：若采集早于 --onto 检出（错序），锚点会退化成 main tip，
        # 区间会凭空多出 PR 的既有提交。
        self.assertNotEqual(anchor, self.main_tip)
        self.assertEqual(git(clone, "rev-list", "--count", f"{self.main_tip}..{anchor}"), "1")


class TestWorkerResultContract(GitFixture):
    """AC2-3 / AC2-4：真跑 worker main()，断言回传契约与采集时点。"""

    def _run_worker_main(self, task_id, onto=None):
        worker = load_worker()
        events = {}
        real_capture = worker.capture_head_sha
        real_write = worker.write_task_context

        def spy_capture(clone):
            sha = real_capture(clone)
            events["anchor"] = sha
            events["anchor_taken"] = True
            return sha

        def spy_write(clone, agent, branch, **kwargs):
            # 写入 .agent-task-context 的这一刻：锚点必须已经采集，
            # 且文件尚不存在（否则它自己会落进锚点区间）。
            events["anchor_missing_at_write"] = not events.get("anchor_taken")
            events["context_existed_at_write"] = (Path(clone) / ".agent-task-context").exists()
            events["head_at_write"] = git(clone, "rev-parse", "HEAD")
            return real_write(clone, agent, branch, **kwargs)

        argv = [
            "herdr-worker",
            "--task-id", task_id,
            "--source", str(self.source),
            "--agent", "claude",
            "--task-type", "feat",
            "--base-branch", "main",
            "--pane-id", "w1:p1",
        ]
        if onto:
            argv += ["--onto", onto]

        stdout = io.StringIO()
        with patch.object(worker, "CLONE_ROOT", self.clone_root), \
             patch.object(worker, "_registered_tasks", return_value=[]), \
             patch.object(worker, "is_task_active_in_registry", return_value=False), \
             patch.object(worker, "prepare_existing_pane"), \
             patch.object(worker, "verify_request_preflight", return_value={"request_verified": True}), \
             patch.object(worker, "wait_startup_ready", return_value={"status": "READY", "interactive_ready": True}), \
             patch.object(
                 worker, "start_agent",
                 return_value={
                     "agent": "claude",
                     "name": "test-agent",
                     "agent_session": {"value": "sess-1"},
                     "agent_status": "running",
                 },
             ), \
             patch.object(worker, "capture_head_sha", side_effect=spy_capture), \
             patch.object(worker, "write_task_context", side_effect=spy_write), \
             patch.object(sys, "argv", argv), \
             contextlib.redirect_stdout(stdout):
            worker.main()

        output = stdout.getvalue()
        payload = None
        for line in output.splitlines():
            if line.startswith("HERDR_WORKER_RESULT="):
                payload = json.loads(line.split("=", 1)[1])
                break
        self.assertIsNotNone(payload, output[-2000:])
        return payload, output, events

    def test_worker_result_carries_anchor_and_onto_for_new_branch(self):
        payload, output, _events = self._run_worker_main("anchor-result-task")

        anchor = payload["baseline_commit"]
        self.assertIsNotNone(anchor)
        self.assertEqual(len(anchor), 40)
        self.assertTrue(FULL_SHA.match(anchor), anchor)
        self.assertIsNone(payload["onto_branch"])
        self.assertIn(f"[BASELINE COMMIT] {anchor}", output)
        self.assertLess(
            output.index("[BRANCH]"), output.index("[BASELINE COMMIT]")
        )
        self.assertEqual(payload["branch"], "agent/claude/feat-anchor-result-task")

    def test_worker_result_carries_onto_branch_name(self):
        self.make_onto_origin()
        payload, output, _events = self._run_worker_main("anchor-result-onto", onto="feat/wip")

        self.assertEqual(payload["branch"], "feat/wip")
        self.assertEqual(payload["onto_branch"], "feat/wip")
        self.assertEqual(payload["baseline_commit"], self.pr_tip)
        self.assertIn(f"[BASELINE COMMIT] {self.pr_tip}", output)

    def test_anchor_collected_before_agent_context_is_written(self):
        self.make_onto_origin()
        payload, _, events = self._run_worker_main("anchor-order-task", onto="feat/wip")

        # 采集先于 .agent-task-context 落盘
        self.assertFalse(events["anchor_missing_at_write"])
        self.assertFalse(events["context_existed_at_write"])
        # 且在落盘那一刻 HEAD 就是锚点本身
        self.assertEqual(events["head_at_write"], payload["baseline_commit"])
        self.assertEqual(events["anchor"], payload["baseline_commit"])


class TestCapturePrecedesContextInSource(GitFixture):
    """AC2-4 的结构性护栏：main() 语句顺序中采集必须先于任务上下文写入。"""

    def _main_statements(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                return node
        self.fail("main() not found")

    def _call_lines(self, names):
        found = {}
        for node in ast.walk(self._main_statements()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in names and node.func.id not in found:
                found[node.func.id] = node.lineno
        return found

    def test_anchor_capture_line_precedes_context_write_line(self):
        lines = self._call_lines({"capture_head_sha", "write_task_context"})
        self.assertIn("capture_head_sha", lines)
        self.assertIn("write_task_context", lines)
        self.assertLess(lines["capture_head_sha"], lines["write_task_context"])


class TestLaunchFieldPersistence(unittest.TestCase):
    """AC2-3（任务记录）：launch 侧必须落库 baseline_commit / onto_branch。

    `herdr-task launch` 需要 panes / agent CLI 现场，故此处以 AST 断言锁定
    字段落库契约；端到端 launch 由 T3 的收编 e2e 覆盖。若 T3 与该落库冲突，
    以 T3 合并结果为准（plan-arch §4.3）。
    """

    def _launch_fn(self):
        tree = ast.parse(LAUNCH_PATH.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_launch_task":
                return node
        self.fail("_launch_task not found")

    def test_task_record_literal_persists_anchor_and_onto(self):
        fn = self._launch_fn()
        keys = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            if not targets or targets[0].id != "task":
                continue
            keys |= {
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        self.assertIn("baseline_commit", keys)
        self.assertIn("onto_branch", keys)

    def test_launch_reads_anchor_and_onto_from_worker_result(self):
        reads = set()
        for node in ast.walk(self._launch_fn()):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "worker_result":
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                reads.add(node.args[0].value)
        self.assertIn("baseline_commit", reads)
        self.assertIn("onto_branch", reads)


if __name__ == "__main__":
    unittest.main()
