"""Direct-commit adoption tests (AC-2, AC-5, AC-10).

End-to-end cover for `herdr-task commit` with real git clones:
agent-side direct commits are adopted (no new commit object), vacuum tasks
keep exit 3, unattributable pushes exit 4, and re-commit is idempotent.
"""

import contextlib
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
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_herdr_task(name="herdr_task_commit_adopt_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "bin" / "herdr-task"),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _load_herdr_task()


class CommitAdoptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-commit-adopt-")
        self.root = Path(self.tmp.name)
        self.db_path = str(self.root / "state.db")
        self.old_state_db = os.environ.get("HERDR_STATE_DB")
        self.old_lock_root = os.environ.get("HERDR_GIT_LOCK_ROOT")
        os.environ["HERDR_STATE_DB"] = self.db_path
        os.environ["HERDR_GIT_LOCK_ROOT"] = str(self.root / "locks")
        _ht.TASKS_FILE = str(self.root / "tasks.json")
        self.clone = self.root / "clone"
        self.clone.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "herdr-test")
        (self.clone / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "base.txt")
        self._git("commit", "-m", "baseline")
        self.baseline = self._rev("HEAD")
        self.created_at = time.time()
        # P1: the worker leaves the clone checked out on the task branch,
        # so adoption fixtures must reproduce that checkout identity.
        self._git("checkout", "-b", "agent/opencode/test-t-adopt")
        self._save_task()

    def tearDown(self):
        if self.old_state_db is None:
            os.environ.pop("HERDR_STATE_DB", None)
        else:
            os.environ["HERDR_STATE_DB"] = self.old_state_db
        if self.old_lock_root is None:
            os.environ.pop("HERDR_GIT_LOCK_ROOT", None)
        else:
            os.environ["HERDR_GIT_LOCK_ROOT"] = self.old_lock_root
        try:
            from herdr.state_store import reset_state_store

            reset_state_store()
        except ImportError:
            pass
        self.tmp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=str(self.clone),
            text=True,
            capture_output=True,
            check=True,
        )

    def _rev(self, rev):
        return subprocess.run(
            ["git", "-C", str(self.clone), "rev-parse", rev],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def _count(self):
        return subprocess.run(
            ["git", "-C", str(self.clone), "rev-list", "--count", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def _direct_commit(self, name, content):
        (self.clone / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-m", f"agent direct: {name}")

    def _save_task(self, task_id="t-adopt", **extra):
        task = {
            "task_id": task_id,
            "workflow_id": "wf-adopt",
            "run_id": "run-adopt",
            "status": "completed",
            "stage": "implementation",
            "node": "implementation",
            "agent": "opencode",
            "clone_path": str(self.clone),
            "branch": "agent/opencode/test-t-adopt",
            "integration_mode": "git",
            "baseline_commit": self.baseline,
            "created_at": self.created_at,
        }
        task.update(extra)
        _ht._get_store().save_task(task)
        return task

    def _commit_call(self, task_id):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                _ht.commit_task(task_id)
        except SystemExit as exc:
            return exc.code, buf.getvalue()
        return 0, buf.getvalue()

    def _store_task(self, task_id):
        return _ht._get_store().get_task(task_id)


class DirectCommitAdoptTest(CommitAdoptBase):
    def test_direct_commits_adopted_without_new_commit(self):
        before = self._count()
        self._direct_commit("one.txt", "one\n")
        self._direct_commit("two.txt", "two\n")
        self.assertEqual(self._count(), str(int(before) + 2))
        head = self._rev("HEAD")

        code, output = self._commit_call("t-adopt")

        self.assertEqual(code, 0)
        self.assertEqual(self._count(), str(int(before) + 2))
        self.assertEqual(self._rev("HEAD"), head)
        stored = self._store_task("t-adopt")
        self.assertEqual(stored["status"], "committed")
        self.assertEqual(stored["commit"], head)
        self.assertEqual(stored.get("commit_result"), "adopted")
        self.assertIn("[ADOPTED]", output)
        self.assertIn("HERDR_COMMIT_RESULT=", output)
        payload = json.loads(
            next(
                line.split("=", 1)[1]
                for line in output.splitlines()
                if line.startswith("HERDR_COMMIT_RESULT=")
            )
        )
        self.assertEqual(payload["result"], "adopted")
        self.assertEqual(payload["commit"], head)
        self.assertEqual(payload["commits"], 2)

    def test_vacuum_keeps_exit_3_with_empty_result(self):
        code, output = self._commit_call("t-adopt")

        self.assertEqual(code, 3)
        self.assertIn("HERDR_COMMIT_RESULT=", output)
        payload = json.loads(
            next(
                line.split("=", 1)[1]
                for line in output.splitlines()
                if line.startswith("HERDR_COMMIT_RESULT=")
            )
        )
        self.assertEqual(payload["result"], "empty")
        stored = self._store_task("t-adopt")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored.get("commit_result"), "empty")

    def test_onto_without_anchor_refused(self):
        self._save_task(
            task_id="t-onto",
            baseline_commit=None,
            onto_branch="pr-branch",
        )
        (self.clone / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        self._git("add", "foreign.txt")
        self._git("commit", "-m", "other task commit")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
            _ht.commit_task("t-onto")

        self.assertEqual(ctx.exception.code, 4)
        self.assertIn("HERDR_COMMIT_RESULT=", buf.getvalue())
        stored = self._store_task("t-onto")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored.get("commit_result"), "refused")

    def test_onto_with_anchor_adopted(self):
        self._save_task(
            task_id="t-onto-ok",
            onto_branch="pr-branch",
        )
        self._direct_commit("own.txt", "own\n")

        code, output = self._commit_call("t-onto-ok")

        self.assertEqual(code, 0)
        stored = self._store_task("t-onto-ok")
        self.assertEqual(stored["status"], "committed")
        self.assertEqual(stored.get("commit_result"), "adopted")
        self.assertIn("[ADOPTED]", output)

    def test_repeat_commit_is_idempotent(self):
        self._direct_commit("one.txt", "one\n")
        code, _ = self._commit_call("t-adopt")
        self.assertEqual(code, 0)
        head = self._rev("HEAD")
        before = self._count()

        code, output = self._commit_call("t-adopt")

        self.assertEqual(code, 0)
        self.assertEqual(self._rev("HEAD"), head)
        self.assertEqual(self._count(), before)
        self.assertEqual(self._store_task("t-adopt")["commit"], head)
        payload = json.loads(
            next(
                line.split("=", 1)[1]
                for line in output.splitlines()
                if line.startswith("HERDR_COMMIT_RESULT=")
            )
        )
        self.assertIn(payload["result"], ("created", "adopted"))
        self.assertEqual(payload["commit"], head)

    def test_staged_changes_take_normal_commit_path(self):
        before = self._count()
        (self.clone / "staged.txt").write_text("staged\n", encoding="utf-8")

        code, output = self._commit_call("t-adopt")

        self.assertEqual(code, 0)
        self.assertEqual(self._count(), str(int(before) + 1))
        stored = self._store_task("t-adopt")
        self.assertEqual(stored["status"], "committed")
        self.assertNotIn("[ADOPTED]", output)
        self.assertIn("HERDR_COMMIT_RESULT=", output)


if __name__ == "__main__":
    unittest.main()
