"""impl-fix4 regression: H-1/H-2/H-3/M-4 + M-6/M-7/L-8.

Real-git probes for the review-t1 blockers (note n-1790219330622-2050).
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

_TMP_ATTN = tempfile.TemporaryDirectory(prefix="herdr-fix4-attn-")
os.environ["HERDR_ATTENTION_FILE"] = str(Path(_TMP_ATTN.name) / "attention.json")


def _load_herdr_task(name="herdr_task_fix4"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "bin" / "herdr-task"),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_controller(name="ctrl_fix4"):
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


class Fix4GitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-fix4-")
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
            "workflow_id": "wf-fix4",
            "run_id": "run-fix4",
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

    def _run_commit(self, task_id):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                _ht.commit_task(task_id)
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

    def _make_origin_with_foreign(self):
        """Bare origin + seed advance producing foreign other.txt."""
        origin = self.root / "origin.git"
        seed = self.root / "seed"
        subprocess.run(["git", "init", "--bare", str(origin)],
                       text=True, capture_output=True, check=True)
        seed.mkdir()
        self._git("init", "-b", "main", cwd=seed)
        self._git("config", "user.email", "test@example.com", cwd=seed)
        self._git("config", "user.name", "herdr-test", cwd=seed)
        (seed / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "base.txt", cwd=seed)
        self._git("commit", "-m", "seed", cwd=seed)
        self._git("remote", "add", "origin", str(origin), cwd=seed)
        self._git("push", "-u", "origin", "main", cwd=seed)
        return origin, seed


class H2FetchRebaseProbe(Fix4GitBase):
    """H-2 S3: idle task + fetch + rebase origin/main must not ADOPT."""

    def test_fetch_rebase_foreign_refused(self):
        origin, seed = self._make_origin_with_foreign()
        # Task clone tracks origin at seed tip.
        task_clone = self.root / "tclone"
        subprocess.run(["git", "clone", str(origin), str(task_clone)],
                       text=True, capture_output=True, check=True)
        subprocess.run(["git", "-C", str(task_clone), "config",
                        "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(task_clone), "config",
                        "user.name", "herdr-test"], check=True)
        subprocess.run(["git", "-C", str(task_clone), "checkout", "-b",
                        "agent/opencode/docs-t-h2r"], check=True)
        baseline = subprocess.run(
            ["git", "-C", str(task_clone), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True).stdout.strip()
        created = time.time()
        # Other person advances origin/main.
        (seed / "other.txt").write_text("foreign\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "other.txt"], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "foreign"],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"],
                       check=True)
        # Idle task fetches + rebases.
        subprocess.run(["git", "-C", str(task_clone), "fetch", "origin"],
                       check=True)
        subprocess.run(["git", "-C", str(task_clone), "rebase",
                        "origin/main"], check=True)
        head = subprocess.run(
            ["git", "-C", str(task_clone), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True).stdout.strip()
        self.assertNotEqual(head, baseline)
        interval = _ht._git_interval_commits(str(task_clone), baseline, head)
        self.assertIsNotNone(interval)
        self.assertTrue(any(
            "other.txt" in (c.get("paths") or []) for c in interval))
        remote = _ht._git_remote_contained_shas(
            str(task_clone), [c.get("sha") for c in interval])
        verdict, detail = classify_commit_state(
            baseline_commit=baseline, head=head,
            branch="agent/opencode/docs-t-h2r", task_id="t_h2r",
            created_at=created, interval_commits=interval,
            baseline_is_ancestor=True, remote_shas=remote,
            current_branch="agent/opencode/docs-t-h2r",
        )
        self.assertIn(verdict, ("refused", "empty"))
        # Foreign deliverable must never be adopted: refused carries the
        # offending foreign sha, empty carries no paths.
        if verdict == "refused":
            self.assertEqual(detail.get("reason"), "foreign_commit_in_range")
            self.assertTrue(detail.get("offending"))
        else:
            self.assertNotIn("other.txt", detail.get("changed_paths") or [])


class H2FastForwardMergeProbe(Fix4GitBase):
    """H-2 S5: idle task + ff merge origin/main must not ADOPT."""

    def test_ff_merge_foreign_refused(self):
        origin, seed = self._make_origin_with_foreign()
        task_clone = self.root / "tclone2"
        subprocess.run(["git", "clone", str(origin), str(task_clone)],
                       text=True, capture_output=True, check=True)
        subprocess.run(["git", "-C", str(task_clone), "config",
                        "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(task_clone), "config",
                        "user.name", "herdr-test"], check=True)
        subprocess.run(["git", "-C", str(task_clone), "checkout", "-b",
                        "agent/opencode/docs-t-h2m"], check=True)
        baseline = subprocess.run(
            ["git", "-C", str(task_clone), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True).stdout.strip()
        created = time.time()
        (seed / "other.txt").write_text("foreign\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "other.txt"], check=True)
        subprocess.run(["git", "-C", str(seed), "commit", "-m", "foreign"],
                       check=True)
        subprocess.run(["git", "-C", str(seed), "push", "origin", "main"],
                       check=True)
        subprocess.run(["git", "-C", str(task_clone), "fetch", "origin"],
                       check=True)
        subprocess.run(["git", "-C", str(task_clone), "merge", "--ff-only",
                        "origin/main"], check=True)
        head = subprocess.run(
            ["git", "-C", str(task_clone), "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True).stdout.strip()
        interval = _ht._git_interval_commits(str(task_clone), baseline, head)
        remote = _ht._git_remote_contained_shas(
            str(task_clone), [c.get("sha") for c in interval or []])
        verdict, detail = classify_commit_state(
            baseline_commit=baseline, head=head,
            branch="agent/opencode/docs-t-h2m", task_id="t_h2m",
            created_at=created, interval_commits=interval,
            baseline_is_ancestor=True, remote_shas=remote,
            current_branch="agent/opencode/docs-t-h2m",
        )
        self.assertIn(verdict, ("refused", "empty"))
        if verdict == "refused":
            self.assertEqual(detail.get("reason"), "foreign_commit_in_range")
            self.assertTrue(detail.get("offending"))
        else:
            self.assertNotIn("other.txt", detail.get("changed_paths") or [])


class H2PureGuards(unittest.TestCase):
    """H-2 pure classifier: remote containment + author/committer skew."""

    def test_remote_contained_refused(self):
        now = time.time()
        verdict, detail = classify_commit_state(
            baseline_commit="base", head="foreign",
            branch="agent/opencode/docs-t-x", task_id="t_x",
            created_at=now,
            interval_commits=[{
                "sha": "foreign", "committer_ts": now, "author_ts": now,
                "parents": ["base"], "paths": ["other.txt"]}],
            baseline_is_ancestor=True, remote_shas={"foreign"},
            current_branch="agent/opencode/docs-t-x",
        )
        self.assertEqual(verdict, "refused")
        self.assertEqual(detail["reason"], "foreign_commit_in_range")
        self.assertTrue(detail.get("offending"))

    def test_rebased_author_skew_refused(self):
        now = time.time()
        verdict, detail = classify_commit_state(
            baseline_commit="base", head="rebased",
            branch="agent/opencode/docs-t-x", task_id="t_x",
            created_at=now,
            interval_commits=[{
                "sha": "rebased", "committer_ts": now,
                "author_ts": now - 7200, "parents": ["base"],
                "paths": ["other.txt"]}],
            baseline_is_ancestor=True, remote_shas=set(),
            current_branch="agent/opencode/docs-t-x",
        )
        self.assertEqual(verdict, "refused")
        self.assertEqual(detail["reason"], "commit_predates_task")


class H1CloseGate(unittest.TestCase):
    """H-1: escalated tasks need explicit human confirmation + revocable."""

    def _tasks(self):
        return {"tasks": [{
            "task_id": "t-h1", "workflow_id": "wf-h1", "status": "completed",
            "integration_mode": "git", "finalize_escalated": True}]}

    def test_close_aborts_without_confirm(self):
        with patch.object(_ht, "load_tasks", return_value=self._tasks()), \
             patch.object(_ht, "_load_workflow_entry",
                          return_value=(None, {})), \
             patch.object(_ht, "load_workflows",
                          return_value={"workflows": {}}):
            with self.assertRaises(SystemExit) as cm:
                _ht.close_workflow("wf-h1")
            self.assertEqual(cm.exception.code, 2)

    def test_close_passes_with_accept_escalated(self):
        with patch.object(_ht, "load_tasks", return_value=self._tasks()), \
             patch.object(_ht, "_load_workflow_entry",
                          return_value=(None, {})), \
             patch.object(_ht, "_finalize_one",
                          return_value={"task_id": "t-h1",
                                        "status": "completed",
                                        "action": "finalized"}), \
             patch.object(_ht, "_workflow_stage_tabs",
                          return_value={"tab_ids": [],
                                        "workspace_id": None,
                                        "coordinator_pane": None,
                                        "owned_pane_ids": set()}), \
             patch.object(_ht, "_mark_workflow_completed"), \
             patch.object(_ht, "stage_reset"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _ht.close_workflow("wf-h1", accept_escalated=True)
            self.assertIn("[CLOSE REPORT]", buf.getvalue())

    def test_controller_defers_auto_close_on_escalated(self):
        t = {"task_id": "t-h1", "workflow_id": "wf-h1", "status": "committed",
             "integration_mode": "git", "finalize_escalated": True}
        with patch.object(_ctrl, "load_tasks", return_value=[t]):
            self.assertEqual(_ctrl.git_escalated_tasks("wf-h1"), ["t-h1"])
            self.assertEqual(_ctrl.git_finalize_pending_tasks("wf-h1"), [])

    def test_clear_escalation_revokes(self):
        t = {"task_id": "t-h1", "workflow_id": "wf-h1", "status": "committed",
             "integration_mode": "git", "finalize_escalated": True,
             "finalize_escalate_reason": "retry_exhausted"}
        with patch.object(_ctrl, "get_task", return_value=dict(t)), \
             patch("herdr.kernel.update_task_metadata",
                   return_value={}) as meta, \
             patch.object(_ctrl, "attention_clear"), \
             patch.object(_ctrl, "_get_store", return_value=MagicMock()):
            self.assertTrue(_ctrl.clear_finalize_escalation("t-h1"))
            updates = meta.call_args.args[1]
            self.assertFalse(updates.get("finalize_escalated"))

    def test_integrate_rc5_deterministic_refused(self):
        task = {"task_id": "t-h1", "workflow_id": "wf-h1", "run_id": "r",
                "node": "implementation", "stage": "implementation",
                "status": "committed", "agent": "opencode",
                "integration_mode": "git"}
        integrate_proc = subprocess.CompletedProcess(
            [], 5, "Main repository has tracked changes\n"
            "HERDR_INTEGRATE_RESULT="
            + json.dumps({"task_id": "t-h1", "result": "main_dirty"}) + "\n",
            "")

        def _fake_run(cmd, **kwargs):
            return integrate_proc

        with patch.object(_ctrl.subprocess, "run",
                          side_effect=_fake_run), \
             patch.object(_ctrl, "get_task", return_value=dict(task)), \
             patch.object(_ctrl, "ensure_no_git_processes",
                          return_value=None), \
             patch.object(_ctrl, "_escalate_finalize") as esc, \
             patch.object(_ctrl, "_get_store",
                          return_value=MagicMock()), \
             patch.object(_ctrl, "enqueue_stage_advance"):
            out = _ctrl.finalize_completed_task("t-h1")
        self.assertFalse(out.get("retryable"))
        self.assertEqual(out.get("rc"), 5)
        esc.assert_called_once()
        self.assertEqual(esc.call_args.args[1], "integrate_main_dirty")


class H3EmptyRealDiskForm(unittest.TestCase):
    """H-3: anchored EMPTY in commit_task real form must not auto-release."""

    def test_anchored_empty_not_releasable(self):
        fresh = {"task_id": "t-h3", "baseline_commit": "abc123",
                 "commit_basis": "baseline_commit", "commit_head": "abc123",
                 "commit_result": "empty"}
        self.assertFalse(_ctrl._empty_auto_releasable(fresh))

    def test_legacy_time_releasable(self):
        self.assertTrue(
            _ctrl._empty_auto_releasable({"commit_basis": "time"}))

    def test_basis_absent_escalates(self):
        self.assertFalse(_ctrl._empty_auto_releasable({}))


class M4EnumerationFailed(unittest.TestCase):
    """M-4: git enumeration failure with head != baseline must REFUSE."""

    def test_anchor_none_interval_refused(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base", head="moved",
            branch="agent/opencode/docs-t-m4", task_id="t_m4",
            created_at=time.time(), interval_commits=None,
            baseline_is_ancestor=True,
            current_branch="agent/opencode/docs-t-m4",
        )
        self.assertEqual(verdict, "refused")
        self.assertEqual(detail["reason"], "enumeration_failed")

    def test_anchor_flag_refused(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base", head="moved",
            branch="agent/opencode/docs-t-m4", task_id="t_m4",
            created_at=time.time(),
            interval_commits=[{"sha": "x", "committer_ts": time.time(),
                               "parents": ["base"], "paths": ["a.txt"]}],
            baseline_is_ancestor=True, enumeration_failed=True,
            current_branch="agent/opencode/docs-t-m4",
        )
        self.assertEqual(verdict, "refused")
        self.assertEqual(detail["reason"], "enumeration_failed")

    def test_commit_paths_none_on_failure(self):
        with patch.object(_ht.subprocess, "run",
                          return_value=subprocess.CompletedProcess(
                              [], 128, "", "fatal")):
            self.assertIsNone(_ht._git_commit_paths("/nope", "abc"))

    def test_interval_none_on_log_failure(self):
        with patch.object(_ht, "_git_log_commits", return_value=None):
            self.assertIsNone(_ht._git_interval_commits("/nope", "b", "h"))


class M6CalledProcessError(unittest.TestCase):
    """M-6: CalledProcessError must escalate with budget, not spin."""

    def test_subprocess_error_escalates(self):
        task = {"task_id": "t-m6", "workflow_id": "wf-m6",
                "status": "completed", "integration_mode": "git"}
        with patch.object(_ctrl, "should_retry_finalize",
                          return_value=(True, "commit_retry", False)), \
             patch.object(_ctrl, "finalize_completed_task",
                          side_effect=subprocess.CalledProcessError(
                              1, ["git"], "boom")), \
             patch.object(_ctrl, "get_task", return_value=dict(task)), \
             patch.object(_ctrl, "workflow_closed", return_value=False), \
             patch.object(_ctrl, "_escalate_finalize") as esc:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reached = _ctrl._check_finalize_retry(
                    dict(task), "completed", 1700000000.0)
            self.assertTrue(reached)
            esc.assert_called_once()
            self.assertIn("[FINALIZE SUBPROCESS ERROR]", buf.getvalue())
        _ctrl.attention_clear("t-m6:finalize")
        _ctrl._finalize_retry_exhausted_logged.discard("t-m6")


class M7IntegrateParser(unittest.TestCase):
    """M-7: integrate payload parsed from HERDR_INTEGRATE_RESULT."""

    def test_parse_integrate_result(self):
        payload = {"task_id": "t-m7", "result": "remote_diverged"}
        out = ("noise\nHERDR_INTEGRATE_RESULT=" + json.dumps(payload) + "\n")
        self.assertEqual(_ctrl._parse_integrate_result(out), payload)
        self.assertEqual(_ctrl._parse_commit_result(out), {})

    def test_rc4_splits_reasons(self):
        task = {"task_id": "t-m7", "workflow_id": "wf-m7", "run_id": "r",
                "node": "implementation", "stage": "implementation",
                "status": "committed", "agent": "opencode",
                "integration_mode": "git"}

        def _drive(payload, stdout_extra=""):
            proc = subprocess.CompletedProcess(
                [], 4, stdout_extra
                + "HERDR_INTEGRATE_RESULT=" + json.dumps(payload) + "\n", "")
            with patch.object(_ctrl.subprocess, "run",
                              return_value=proc), \
                 patch.object(_ctrl, "get_task",
                              return_value=dict(task)), \
                 patch.object(_ctrl, "ensure_no_git_processes",
                              return_value=None), \
                 patch.object(_ctrl, "_escalate_finalize") as esc, \
                 patch.object(_ctrl, "_get_store",
                              return_value=MagicMock()), \
                 patch.object(_ctrl, "enqueue_stage_advance"):
                return _ctrl.finalize_completed_task("t-m7"), esc

        _out, esc = _drive({"task_id": "t-m7", "result": "remote_diverged"})
        self.assertEqual(esc.call_args.args[1], "integrate_remote_diverged")
        self.assertNotEqual(esc.call_args.args[2], {})
        _out2, esc2 = _drive({"task_id": "t-m7",
                              "result": "not_based_cleanly"})
        self.assertEqual(esc2.call_args.args[1],
                         "integrate_not_based_cleanly")


class L8Parity(unittest.TestCase):
    """L-8: duplicated helpers stay byte-identical in behavior."""

    def test_coerce_epoch_parity(self):
        from herdr.git_adoption import _coerce_epoch as pure_coerce
        for value in (None, "", "abc", 0, 1700000000, "1700000000.5",
                      float("nan")):
            left = _ht._coerce_epoch(value)
            right = pure_coerce(value)
            if left is None or right is None:
                self.assertEqual(left, right)
            elif isinstance(left, float) and isinstance(right, float) \
                    and (math.isnan(left) or math.isnan(right)):
                self.assertTrue(math.isnan(left) and math.isnan(right))
            else:
                self.assertEqual(left, right)

    def test_skew_parity(self):
        from herdr.git_adoption import adoption_skew_seconds
        for env in (None, "120", "0", "-5", "abc", ""):
            old = os.environ.get("HERDR_ADOPT_SKEW_SECONDS")
            try:
                if env is None:
                    os.environ.pop("HERDR_ADOPT_SKEW_SECONDS", None)
                else:
                    os.environ["HERDR_ADOPT_SKEW_SECONDS"] = env
                self.assertEqual(_ht._adoption_skew_seconds(),
                                 adoption_skew_seconds())
            finally:
                if old is None:
                    os.environ.pop("HERDR_ADOPT_SKEW_SECONDS", None)
                else:
                    os.environ["HERDR_ADOPT_SKEW_SECONDS"] = old

    def test_dead_code_removed(self):
        source = (HERDR_ROOT / "bin" / "herdr-task").read_text(
            encoding="utf-8")
        self.assertNotIn("def _git_diff_name_only", source)


if __name__ == "__main__":
    unittest.main()
