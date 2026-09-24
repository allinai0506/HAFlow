"""Legacy-task convergence tests (AC-3, AC-13).

Time-basis adoption for tasks without `baseline_commit` (the
wf-nexusarchive-0921-01-wrapup-auto shape), verify-baseline before/after
adoption, and integrate conflict/success outcomes after adoption.
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
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_herdr_task(name="herdr_task_legacy_converge_test"):
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


class LegacyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-legacy-")
        self.root = Path(self.tmp.name)
        self.old_state_db = os.environ.get("HERDR_STATE_DB")
        self.old_lock_root = os.environ.get("HERDR_GIT_LOCK_ROOT")
        os.environ["HERDR_STATE_DB"] = str(self.root / "state.db")
        os.environ["HERDR_GIT_LOCK_ROOT"] = str(self.root / "locks")
        _ht.TASKS_FILE = str(self.root / "tasks.json")
        self.clone = self.root / "clone"
        self.clone.mkdir()
        self.now = time.time()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "herdr-test")
        (self.clone / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "base.txt")
        self._git("commit", "-m", "baseline", env=self._date_env(self.now - 7200))
        self.created_at = self.now - 3600
        # P1: the worker leaves the clone checked out on the task branch,
        # so legacy time-basis fixtures reproduce that checkout identity.
        self._git("checkout", "-b", "agent/opencode/test-t-legacy")

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

    def _date_env(self, ts):
        stamp = str(int(ts))
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        return env

    def _git(self, *args, env=None, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.clone),
            text=True,
            capture_output=True,
            check=True,
            env=env,
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

    def _direct_commit(self, name, content, ts):
        (self.clone / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-m", f"legacy direct: {name}",
                  env=self._date_env(ts))

    def _save_task(self, task_id="t-legacy", **extra):
        task = {
            "task_id": task_id,
            "workflow_id": "wf-legacy",
            "run_id": "run-legacy",
            "status": "completed",
            "stage": "implementation",
            "node": "implementation",
            "agent": "opencode",
            "clone_path": str(self.clone),
            "branch": "agent/opencode/test-t-legacy",
            "integration_mode": "git",
            "created_at": self.created_at,
            "baseline_fingerprint": {"tracked": {}, "untracked": {}},
        }
        task.update(extra)
        _ht._get_store().save_task(task)
        return task

    def _run_commit(self, task_id):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                _ht.commit_task(task_id)
        except SystemExit as exc:
            return exc.code, buf.getvalue()
        return 0, buf.getvalue()

    def _run_verify(self, task_id):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                _ht.verify_baseline(task_id)
        except SystemExit as exc:
            return exc.code, buf.getvalue()
        return 0, buf.getvalue()


class LegacyAdoptTest(LegacyBase):
    def test_time_basis_adopts_stored_commits(self):
        self._save_task()
        self._direct_commit("one.txt", "one\n", self.now - 1800)
        self._direct_commit("two.txt", "two\n", self.now - 600)
        before = self._count()
        head = self._rev("HEAD")

        code, output = self._run_commit("t-legacy")

        self.assertEqual(code, 0)
        self.assertEqual(self._count(), before)
        stored = _ht._get_store().get_task("t-legacy")
        self.assertEqual(stored["status"], "committed")
        self.assertEqual(stored["commit"], head)
        self.assertEqual(stored.get("commit_result"), "adopted")
        self.assertEqual(stored.get("commit_basis"), "time")
        self.assertIn("[ADOPTED]", output)
        self.assertIn('"basis": "time"', output)

    def test_verify_baseline_reports_unregistered_commits(self):
        # Anchor-less tasks keep the commit分量 closed (M-1/AC3-8):
        # verify-baseline is worktree-only and stays BASELINE_MATCH even
        # with unregistered direct commits; adoption still happens via
        # `commit` (time basis + D-2 branch guard).
        self._save_task()
        self._direct_commit("one.txt", "one\n", self.now - 600)

        code, output = self._run_verify("t-legacy")

        self.assertEqual(code, 0)
        self.assertIn("BASELINE_MATCH", output)
        self.assertIn("HERDR_BASELINE_RESULT=", output)
        payload = json.loads(
            next(
                line.split("=", 1)[1]
                for line in output.splitlines()
                if line.startswith("HERDR_BASELINE_RESULT=")
            )
        )
        self.assertTrue(payload["baseline_match"])
        self.assertEqual(payload["changes"], [])
        self.assertEqual(payload["commits_ahead"], 0)
        self.assertEqual(payload["basis"], "absent")

    def test_verify_baseline_match_after_adopt(self):
        self._save_task()
        self._direct_commit("one.txt", "one\n", self.now - 600)
        code, _ = self._run_commit("t-legacy")
        self.assertEqual(code, 0)

        code, output = self._run_verify("t-legacy")

        self.assertEqual(code, 0)
        self.assertIn("BASELINE_MATCH", output)


class IntegrateOutcomeBase(LegacyBase):
    def _build_origin_fixture(self, conflict):
        origin = self.root / "origin.git"
        seed = self.root / "seed"
        main_repo = self.root / "mrepo"
        self.clone = self.root / "tclone"
        subprocess.run(
            ["git", "init", "--bare", str(origin)],
            text=True, capture_output=True, check=True,
        )
        seed.mkdir()
        self._git("init", "-b", "main", cwd=seed)
        self._git("config", "user.email", "test@example.com", cwd=seed)
        self._git("config", "user.name", "herdr-test", cwd=seed)
        (seed / "file.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "file.txt", cwd=seed)
        self._git("commit", "-m", "seed", cwd=seed)
        self._git("remote", "add", "origin", str(origin), cwd=seed)
        self._git("push", "-u", "origin", "main", cwd=seed)
        subprocess.run(
            ["git", "clone", str(origin), str(self.clone)],
            text=True, capture_output=True, check=True,
        )
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "herdr-test")
        self._git("checkout", "-b", "agent/opencode/test-t-int")
        (self.clone / "file.txt").write_text("task\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "-m", "task work")
        if conflict:
            (seed / "file.txt").write_text("origin\n", encoding="utf-8")
        else:
            (seed / "other.txt").write_text("origin\n", encoding="utf-8")
            self._git("add", "other.txt", cwd=seed)
        self._git("add", "file.txt", cwd=seed)
        self._git("commit", "-m", "origin advance", cwd=seed)
        self._git("push", "origin", "main", cwd=seed)
        subprocess.run(
            ["git", "clone", str(origin), str(main_repo)],
            text=True, capture_output=True, check=True,
        )
        return main_repo

    def _save_integrate_task(self, main_repo, task_id="t-int"):
        task = {
            "task_id": task_id,
            "workflow_id": "wf-int",
            "run_id": "run-int",
            "status": "committed",
            "stage": "implementation",
            "node": "implementation",
            "agent": "opencode",
            "clone_path": str(self.clone),
            "branch": "agent/opencode/test-t-int",
            "base_branch": "main",
            "source_repo": str(main_repo),
            "integration_mode": "git",
            "commit": self._rev("HEAD"),
            "created_at": self.created_at,
            "baseline_fingerprint": {"tracked": {}, "untracked": {}},
        }
        _ht._get_store().save_task(task)
        return task


class IntegrateOutcomeTest(IntegrateOutcomeBase):
    def test_rebase_conflict_fails_once_with_reason(self):
        main_repo = self._build_origin_fixture(conflict=True)
        self._save_integrate_task(main_repo)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), patch.object(
            _ht, "project_for_workflow",
            return_value={
                "project_root": str(main_repo),
                "base_branch": "main",
            },
        ), self.assertRaises(SystemExit) as ctx:
            _ht.integrate_task("t-int")

        self.assertEqual(ctx.exception.code, 6)
        self.assertIn("[INTEGRATE REBASE CONFLICT]", buf.getvalue())
        self.assertIn("HERDR_INTEGRATE_RESULT=", buf.getvalue())
        stored = _ht._get_store().get_task("t-int")
        self.assertEqual(stored["status"], "committed")

    def test_clean_rebase_integrates_idempotently(self):
        main_repo = self._build_origin_fixture(conflict=False)
        self._save_integrate_task(main_repo)
        with patch.object(
            _ht, "project_for_workflow",
            return_value={
                "project_root": str(main_repo),
                "base_branch": "main",
            },
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _ht.integrate_task("t-int")
            first = _ht._get_store().get_task("t-int")
            with contextlib.redirect_stdout(io.StringIO()):
                _ht.integrate_task("t-int")
            second = _ht._get_store().get_task("t-int")

        self.assertEqual(first["status"], "integrated")
        self.assertEqual(second["status"], "integrated")
        self.assertEqual(first["integration_ref"], second["integration_ref"])
        self.assertIn("[INTEGRATED]", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
