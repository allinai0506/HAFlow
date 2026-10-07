#!/usr/bin/env python3
"""_autosave_clone_wip 回归：gitignored 内部文件不得让 WIP 自动保存失败。

现场缺陷（tech-debt #4）：launch clone 的 .agent-task-context 被 .gitignore
忽略且存在于工作树，`git add -A ':!.agent-task-context' ...` 的排除式
pathspec 与 git 的 ignored-file advice 相撞（exit 1，提示 use -f），
导致每次 supersede 必现 [WIP AUTOSAVE WARN] git add failed。
"""

import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_task_manager(name):
    path = HERDR_ROOT / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AutosaveCloneWipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-autosave-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "clone"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.repo / ".gitignore").write_text(".agent-task-context\n")
        (self.repo / "base.txt").write_text("base\n")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")
        self.ctl = _load_task_manager("herdr_task_autosave_test")

    def _git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True, capture_output=True,
        )

    def _task(self):
        return {
            "task_id": "test-01-r5",
            "clone_path": str(self.repo),
            "branch": "master",
        }

    def test_ignored_internal_file_does_not_break_autosave(self):
        (self.repo / ".agent-task-context").write_text("launch identity\n")
        (self.repo / "src.txt").write_text("real work\n")

        ok = self.ctl._autosave_clone_wip(self._task())

        self.assertTrue(ok, "gitignored 内部文件不得让 WIP 保存失败")
        log = self._git("log", "--oneline", "-1").stdout.strip()
        self.assertIn("wip: auto-save test-01-r5", log)
        committed = self._git("show", "--name-only", "--format=", "HEAD").stdout
        self.assertIn("src.txt", committed)
        self.assertNotIn(".agent-task-context", committed)

    def test_only_ignored_changes_stay_silent(self):
        (self.repo / ".agent-task-context").write_text("launch identity\n")

        ok = self.ctl._autosave_clone_wip(self._task())

        self.assertFalse(ok)
        log = self._git("log", "--oneline").stdout.strip().splitlines()
        self.assertEqual(len(log), 1, "只有内部文件变更时不得产生 WIP commit")


if __name__ == "__main__":
    unittest.main()
