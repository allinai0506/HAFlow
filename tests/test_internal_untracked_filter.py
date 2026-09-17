"""Regression tests: controller-internal files must never be committed or
fingerprinted as task deliverables (.agent-task-context, .herdr-loop/)."""

import importlib.machinery
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _load_module(
    "herdr_task_internal_filter_test",
    HERDR_ROOT / "bin" / "herdr-task",
)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )


class InternalUntrackedFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-internal-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "clone"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@test.local")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-qm", "base")
        self.tasks_file = self.root / "tasks.json"
        _ht.TASKS_FILE = str(self.tasks_file)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_task(self, **extra):
        task = {
            "task_id": "t1",
            "status": "completed",
            "clone_path": str(self.repo),
            "branch": "agent/probe/feat",
            "baseline_untracked": [],
        }
        task.update(extra)
        self.tasks_file.write_text(
            json.dumps({"tasks": [task]}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _write_internal_files(self):
        (self.repo / ".agent-task-context").write_text(
            "agent=probe\n",
            encoding="utf-8",
        )
        loop = self.repo / ".herdr-loop"
        loop.mkdir()
        (loop / "GOAL.md").write_text("# goal\n", encoding="utf-8")
        (loop / "logs").mkdir()
        (loop / "logs" / "test.log").write_text("ok\n", encoding="utf-8")
        gate = self.repo / ".herdr"
        gate.mkdir()
        (gate / "gate-verdict.json").write_text(
            '{"verdict": "pass", "note": "probe"}\n',
            encoding="utf-8",
        )

    def _committed_paths(self):
        return _git(
            self.repo,
            "show",
            "--name-only",
            "--format=",
            "HEAD",
        ).stdout.split()

    def test_helper_matches_only_internal_paths(self):
        self.assertTrue(_ht._is_internal_untracked(".agent-task-context"))
        self.assertTrue(_ht._is_internal_untracked(".herdr-loop"))
        self.assertTrue(_ht._is_internal_untracked(".herdr-loop/GOAL.md"))
        self.assertTrue(
            _ht._is_internal_untracked(".herdr-loop/logs/test.log")
        )
        self.assertTrue(_ht._is_internal_untracked(".herdr"))
        self.assertTrue(
            _ht._is_internal_untracked(".herdr/gate-verdict.json")
        )
        self.assertFalse(_ht._is_internal_untracked("delivery.txt"))
        self.assertFalse(_ht._is_internal_untracked(".agent-task-context.bak"))
        self.assertFalse(_ht._is_internal_untracked(".herdr.bak"))
        self.assertFalse(
            _ht._is_internal_untracked("sub/.herdr-loop/GOAL.md")
        )

    def test_commit_task_skips_internal_untracked(self):
        self._write_internal_files()
        (self.repo / "delivery.txt").write_text("payload\n", encoding="utf-8")
        self._write_task()
        _ht.commit_task("t1")
        self.assertEqual(self._committed_paths(), ["delivery.txt"])
        status = _git(self.repo, "status", "--porcelain").stdout
        self.assertIn("?? .agent-task-context", status)
        self.assertIn("?? .herdr-loop/", status)

    def test_commit_task_keeps_baseline_untracked(self):
        (self.repo / "baseline-note.txt").write_text("pre\n", encoding="utf-8")
        self._write_internal_files()
        (self.repo / "delivery.txt").write_text("payload\n", encoding="utf-8")
        self._write_task(baseline_untracked=["baseline-note.txt"])
        _ht.commit_task("t1")
        self.assertEqual(self._committed_paths(), ["delivery.txt"])
        status = _git(self.repo, "status", "--porcelain").stdout
        self.assertIn("?? baseline-note.txt", status)

    def test_fingerprint_filters_internal_untracked(self):
        self._write_internal_files()
        (self.repo / "delivery.txt").write_text("payload\n", encoding="utf-8")
        fingerprint = _ht.current_workspace_fingerprint(str(self.repo))
        self.assertEqual(sorted(fingerprint["untracked"]), ["delivery.txt"])

    def test_fingerprint_ignores_internal_file_changes(self):
        (self.repo / "delivery.txt").write_text("payload\n", encoding="utf-8")
        before = _ht.current_workspace_fingerprint(str(self.repo))
        self._write_internal_files()
        after = _ht.current_workspace_fingerprint(str(self.repo))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
