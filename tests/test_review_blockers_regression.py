"""Review blockers regression: P1 branch ownership, P2 integrated_commit,
F-3 commit-paths-unknown, F-1 own-branch push scope.

Real-git probes for the NEEDS_FIXES verdict on PR #94: adoption must verify
the actually checked-out branch, verify-baseline must recognize the
post-rebase integrated_commit, uninspectable commits must fail closed, and
a legitimate push of the task branch must not read as foreign.
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

_TMP_ATTN = tempfile.TemporaryDirectory(prefix="herdr-blockers-attn-")
os.environ["HERDR_ATTENTION_FILE"] = str(Path(_TMP_ATTN.name) / "attention.json")


def _load_herdr_task(name="herdr_task_blockers"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "bin" / "herdr-task"),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_controller(name="ctrl_blockers"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py"),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _load_herdr_task()
_ctrl = _load_controller()

from herdr.git_adoption import classify_commit_state


class GitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-blockers-")
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
        self._git("commit", "-m", "baseline",
                  env=self._date_env(self.now - 7200))
        self.baseline = self._rev("HEAD")
        self.created_at = self.now - 3600

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
        env = dict(os.environ)
        stamp = str(int(ts))
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
        return env

    def _git(self, *args, env=None, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.clone),
            text=True, capture_output=True, check=True, env=env,
        )

    def _rev(self, rev, cwd=None):
        return subprocess.run(
            ["git", "-C", str(cwd or self.clone), "rev-parse", rev],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

    def _save_task(self, task_id, **extra):
        task = {
            "task_id": task_id,
            "workflow_id": "wf-blockers",
            "run_id": "run-blockers",
            "status": "completed",
            "stage": "implementation",
            "node": "implementation",
            "agent": "opencode",
            "clone_path": str(self.clone),
            "branch": "agent/opencode/docs-" + task_id.lower().replace("_", "-"),
            "integration_mode": "git",
            "created_at": self.created_at,
            "baseline_fingerprint": {"tracked": {}, "untracked": {}},
        }
        task.update(extra)
        _ht._get_store().save_task(task)
        return task

    def _direct_commit(self, name, content, ts=None):
        (self.clone / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-m", f"direct: {name}",
                  env=self._date_env(ts if ts is not None else self.now - 600))

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

    def _payload(self, output, marker):
        line = next(
            line.split("=", 1)[1]
            for line in output.splitlines()
            if line.startswith(marker + "=")
        )
        return json.loads(line)


class P1CurrentBranchE2E(GitBase):
    """P1: adoption on the wrong checkout must REFUSE, never record it."""

    def test_commit_on_other_branch_refused(self):
        task_id = "t-p1"
        branch = "agent/opencode/docs-t-p1"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch=branch)
        self._git("checkout", "-b", branch)
        self._direct_commit("work.txt", "work\n")
        # Agent strays: new branch, new commit, then herdr-task commit.
        self._git("checkout", "-b", "other")
        self._direct_commit("other.txt", "other\n")
        other_head = self._rev("HEAD")

        code, output = self._run_commit(task_id)

        self.assertEqual(code, 4)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "refused")
        self.assertEqual(payload["reason"], "current_branch_mismatch")
        stored = _ht._get_store().get_task(task_id)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored.get("commit_result"), "refused")
        # The foreign HEAD must never be recorded as the deliverable.
        self.assertNotEqual(stored.get("commit"), other_head)

    def test_commit_on_task_branch_still_adopts(self):
        task_id = "t-p1b"
        branch = "agent/opencode/docs-t-p1b"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch=branch)
        self._git("checkout", "-b", branch)
        self._direct_commit("work.txt", "work\n")

        code, output = self._run_commit(task_id)

        self.assertEqual(code, 0)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "adopted")

    def test_current_branch_probe_failure_refuses(self):
        self.assertIsNone(_ht._git_current_branch(str(self.root / "nope")))
        verdict, detail = classify_commit_state(
            baseline_commit="base", head="head",
            branch="agent/opencode/docs-t1", task_id="t1",
            created_at=self.now,
            interval_commits=[{
                "sha": "c1", "committer_ts": self.now,
                "parents": ["base"], "paths": ["a.txt"]}],
            baseline_is_ancestor=True, current_branch=None,
        )
        self.assertEqual(verdict, "refused")
        self.assertEqual(detail["reason"], "current_branch_mismatch")


class P2IntegratedCommitVerify(GitBase):
    """P2: post-rebase HEAD (integrated_commit) is registered deliverable."""

    def _adopt_then_rewrite(self, task_id, branch):
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch=branch)
        self._git("checkout", "-b", branch)
        self._direct_commit("work.txt", "work\n")
        code, _ = self._run_commit(task_id)
        self.assertEqual(code, 0)
        adopted = self._rev("HEAD")
        # Simulate integrate's rebase rewrite: same content, new sha.
        self._direct_commit("work2.txt", "work2\n")
        rewritten = self._rev("HEAD")
        self.assertNotEqual(adopted, rewritten)
        return adopted, rewritten

    def test_verify_match_after_rebase_rewrite(self):
        task_id = "t-p2"
        branch = "agent/opencode/docs-t-p2"
        _adopted, rewritten = self._adopt_then_rewrite(task_id, branch)
        stored = _ht._get_store().get_task(task_id)
        stored["status"] = "integrated"
        stored["integrated_commit"] = rewritten
        _ht._get_store().save_task(stored)

        code, output = self._run_verify(task_id)

        self.assertEqual(code, 0)
        self.assertIn("BASELINE_MATCH", output)
        payload = self._payload(output, "HERDR_BASELINE_RESULT")
        self.assertTrue(payload["baseline_match"])

    def test_verify_changed_without_integrated_registration(self):
        # Same rewritten HEAD but no integrated_commit recorded: the
        # deliverable moved without registration -> TASK_CHANGED.
        task_id = "t-p2n"
        branch = "agent/opencode/docs-t-p2n"
        _adopted, _rewritten = self._adopt_then_rewrite(task_id, branch)

        code, output = self._run_verify(task_id)

        self.assertEqual(code, 0)
        self.assertIn("TASK_CHANGED", output)
        payload = self._payload(output, "HERDR_BASELINE_RESULT")
        self.assertFalse(payload["baseline_match"])


class F3PathsUnknownE2E(GitBase):
    """F-3: uninspectable interval commits fail closed, never ADOPT."""

    def test_diff_tree_failure_refuses(self):
        task_id = "t-f3"
        branch = "agent/opencode/docs-t-f3"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch=branch)
        self._git("checkout", "-b", branch)
        self._direct_commit("work.txt", "work\n")

        with patch.object(_ht, "_git_commit_paths", return_value=None):
            code, output = self._run_commit(task_id)

        self.assertEqual(code, 4)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "refused")
        self.assertEqual(payload["reason"], "commit_paths_unknown")
        stored = _ht._get_store().get_task(task_id)
        self.assertIsNone(stored.get("commit"))

    def test_verify_marks_unknown_paths_changed(self):
        task_id = "t-f3v"
        branch = "agent/opencode/docs-t-f3v"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch=branch)
        self._git("checkout", "-b", branch)
        self._direct_commit("work.txt", "work\n")

        with patch.object(_ht, "_git_commit_paths", return_value=None):
            code, output = self._run_verify(task_id)

        self.assertEqual(code, 0)
        self.assertIn("TASK_CHANGED", output)
        payload = self._payload(output, "HERDR_BASELINE_RESULT")
        self.assertFalse(payload["baseline_match"])
        self.assertIn("__commit_paths_unknown__",
                      [c["path"] for c in payload["changes"]])


class F1OwnBranchPushScope(GitBase):
    """F-1: pushing the task branch is publication, not foreign history."""

    def _origin_clone(self):
        origin = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                       text=True, capture_output=True, check=True)
        task_clone = self.root / "tclone"
        subprocess.run(["git", "clone", str(self.clone), str(task_clone)],
                       text=True, capture_output=True, check=True)
        for repo in (task_clone,):
            subprocess.run(["git", "-C", str(repo), "config",
                            "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config",
                            "user.name", "herdr-test"], check=True)
        subprocess.run(["git", "-C", str(task_clone), "remote",
                        "set-url", "origin", str(origin)], check=True)
        subprocess.run(["git", "-C", str(task_clone), "push", "origin",
                        "main"], check=True)
        return origin, task_clone

    def test_own_push_excluded_from_foreign(self):
        _origin, task_clone = self._origin_clone()
        branch = "agent/opencode/docs-t-f1"
        subprocess.run(["git", "-C", str(task_clone), "checkout",
                        "-b", branch], check=True)
        (task_clone / "work.txt").write_text("work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(task_clone), "add", "work.txt"],
                       check=True)
        subprocess.run(["git", "-C", str(task_clone), "commit", "-m",
                        "task work"], check=True)
        sha = subprocess.run(
            ["git", "-C", str(task_clone), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(task_clone), "push", "origin",
                        branch], check=True)

        unscoped = _ht._git_remote_contained_shas(str(task_clone), [sha])
        scoped = _ht._git_remote_contained_shas(
            str(task_clone), [sha], exclude_branches=[branch])
        self.assertIn(sha, unscoped)
        self.assertEqual(scoped, set())

        # End-to-end: pushed task branch still adopts (not foreign).
        verdict, detail = classify_commit_state(
            baseline_commit=self.baseline, head=sha,
            branch=branch, task_id="t_f1",
            created_at=self.created_at,
            interval_commits=[{
                "sha": sha, "committer_ts": self.now - 600,
                "author_ts": self.now - 600, "parents": [self.baseline],
                "paths": ["work.txt"]}],
            baseline_is_ancestor=True, remote_shas=scoped,
            current_branch=branch,
        )
        self.assertEqual(verdict, "adopt")
        self.assertEqual(detail["reason"], "attributable_commits")


class EpisodeStoreCoherence(unittest.TestCase):
    """EpisodeStore instances in different processes see each other's writes."""

    def test_cross_instance_upsert_and_clear_visible(self):
        from herdr.liveness import EpisodeStore
        path = Path(tempfile.mkdtemp(prefix="herdr-episode-")) / "attention.json"
        s1 = EpisodeStore(str(path))
        s2 = EpisodeStore(str(path))
        s1.upsert("t-x:finalize",
                  {"task_id": "t-x", "attempts": 5, "reason": "retry_exhausted"})
        self.assertEqual(s2.get("t-x:finalize")["attempts"], 5)
        s2.upsert("t-x:finalize", {"attempts": 6})
        self.assertEqual(s1.get("t-x:finalize")["attempts"], 6)
        s1.clear("t-x:finalize")
        self.assertIsNone(s2.get("t-x:finalize"))


class ClearEscalationRecovery(GitBase):
    """P1: `clear-escalation` must truly resume finalize after retry_exhausted."""

    def tearDown(self):
        _ctrl._finalize_retry_exhausted_logged.discard("t-clr")
        _ctrl.attention_clear("t-clr:finalize")
        super().tearDown()

    def _escalated_task(self):
        task = self._save_task(
            "t-clr",
            baseline_commit=self.baseline,
            branch="agent/opencode/docs-t-clr",
            status="completed",
            finalize_escalated=True,
            finalize_escalate_reason="retry_exhausted",
        )
        _ctrl.attention_note(
            "t-clr:finalize", task, "finalize",
            reason="commit_retry", attempts=5,
        )
        _ctrl._finalize_retry_exhausted_logged.add("t-clr")
        return task

    def test_cli_clear_removes_flags_and_attention(self):
        self._escalated_task()
        self.assertIsNotNone(_ctrl.attention_get("t-clr:finalize"))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _ht.clear_finalize_escalation("t-clr")

        stored = _ht._get_store().get_task("t-clr")
        self.assertFalse(stored.get("finalize_escalated"))
        self.assertIsNone(stored.get("finalize_escalate_reason"))
        # A *different* EpisodeStore instance (the controller's own) must
        # observe the removal: otherwise the next sweep re-escalates.
        self.assertIsNone(_ctrl.attention_get("t-clr:finalize"))
        self.assertIn("[ESCALATION CLEARED]", buf.getvalue())
        self.assertIn("attention_cleared=True", buf.getvalue())

    def test_sweep_redrives_finalize_after_clear(self):
        self._escalated_task()
        with contextlib.redirect_stdout(io.StringIO()):
            _ht.clear_finalize_escalation("t-clr")
        task = _ht._get_store().get_task("t-clr")
        progressed = dict(task, status="committed")
        with patch.object(_ctrl, "should_retry_finalize",
                          return_value=(True, "commit_retry", False)), \
             patch.object(_ctrl, "finalize_completed_task",
                          return_value={"retryable": True,
                                        "kind": "test"}) as fin, \
             patch.object(_ctrl, "get_task",
                          return_value=progressed):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reached = _ctrl._check_finalize_retry(
                    dict(task), "completed", time.time())
        self.assertTrue(reached)
        fin.assert_called_once_with("t-clr")
        self.assertNotIn("t-clr", _ctrl._finalize_retry_exhausted_logged)

    def test_reexhaustion_reescalates_after_clear(self):
        # The stale in-memory latch must not swallow a later genuine
        # re-exhaustion: with the episode cleared, a fresh exhausted
        # verdict logs and escalates again instead of stalling silently.
        self._escalated_task()
        with contextlib.redirect_stdout(io.StringIO()):
            _ht.clear_finalize_escalation("t-clr")
        # Latch is still set in this process (simulating the running
        # controller that the CLI could not IPC); the episode is gone.
        _ctrl._finalize_retry_exhausted_logged.add("t-clr")
        task = _ht._get_store().get_task("t-clr")
        with patch.object(_ctrl, "should_retry_finalize",
                          return_value=(False, "retry_exhausted", True)), \
             patch.object(_ctrl, "get_task",
                          return_value=dict(task)), \
             patch.object(_ctrl, "_escalate_finalize") as esc:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reached = _ctrl._check_finalize_retry(
                    dict(task), "completed", time.time())
        self.assertTrue(reached)
        esc.assert_called_once()
        self.assertEqual(esc.call_args.args[1], "retry_exhausted")
        self.assertIn("[FINALIZE RETRY EXHAUSTED]", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
