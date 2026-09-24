"""test-t3 FAIL probe regression matrix (H-1~H-4, M-1~M-3, L-1~L-4).

Solidifies the 9 FAIL probes from the test-t3 execution record
(note n-1790209008033-6c26) as real-git negative cases so the gaps can
never silently pass again. All git fixtures use temporary repositories;
no origin remote is touched and no history is rewritten.
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
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

_TMP_ATTN = tempfile.TemporaryDirectory(prefix="herdr-t3-attn-")
os.environ["HERDR_ATTENTION_FILE"] = str(Path(_TMP_ATTN.name) / "attention.json")


def _load_herdr_task(name="herdr_task_t3_probes"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "bin" / "herdr-task"),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_controller(name="ctrl_t3_probes"):
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

from herdr.git_adoption import (
    classify_commit_state,
    is_internal_path,
    is_task_branch,
)


def _commit_dict(sha, ts, parents=None, paths=None):
    item = {"sha": sha, "committer_ts": float(ts)}
    if parents is not None:
        item["parents"] = list(parents)
    if paths is not None:
        item["paths"] = list(paths)
    return item


class GitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-t3-")
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
            text=True,
            capture_output=True,
            check=True,
            env=env,
        )

    def _rev(self, rev):
        return subprocess.run(
            ["git", "-C", str(self.clone), "rev-parse", rev],
            text=True, capture_output=True, check=True,
        ).stdout.strip()

    def _save_task(self, task_id, **extra):
        task = {
            "task_id": task_id,
            "workflow_id": "wf-t3",
            "run_id": "run-t3",
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

    def _direct_commit(self, name, content, ts=None):
        (self.clone / name).write_text(content, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-m", f"direct: {name}",
                  env=self._date_env(ts if ts is not None else self.now - 600))


class BranchGuardTest(unittest.TestCase):
    """H-1 D-2 naming-formula guard (pure)."""

    def test_task_branch_accepted(self):
        self.assertTrue(is_task_branch("agent/opencode/docs-t1", "t1"))
        self.assertTrue(
            is_task_branch("agent/opencode/test-t-legacy", "t-legacy")
        )
        self.assertTrue(is_task_branch("agent/claude/docs-wf-x", "wf_x"))

    def test_non_task_branch_rejected(self):
        self.assertFalse(is_task_branch("fix/haflow-some-other-pr", "t1"))
        self.assertFalse(is_task_branch("fix/other-pr", "t1"))
        self.assertFalse(is_task_branch("", "t1"))
        self.assertFalse(is_task_branch(None, "t1"))
        self.assertFalse(is_task_branch("agent/opencode/docs-t1", None))

    def test_classifier_refuses_legacy_branch_not_task_branch(self):
        # Probe g2 shape.
        verdict, detail = classify_commit_state(
            head="head",
            branch="fix/other-pr",
            task_id="t1",
            created_at=1000,
            head_history=[
                _commit_dict("head", 1200),
                _commit_dict("c1", 1100),
                _commit_dict("old", 500),
            ],
        )
        self.assertEqual(verdict, "refused")
        self.assertEqual(detail["reason"], "legacy_branch_not_task_branch")


class H1LegacyBranchE2E(GitBase):
    """H-1 probe f4: non-task branch must not adopt чужой commit."""

    def test_foreign_branch_refused(self):
        self._save_task(
            "t-h1",
            baseline_commit=None,
            branch="fix/haflow-some-other-pr",
        )
        self._direct_commit("foreign.txt", "foreign\n", self.now - 600)
        code, output = self._run_commit("t-h1")
        self.assertEqual(code, 4)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "refused")
        self.assertEqual(payload["reason"], "legacy_branch_not_task_branch")
        stored = _ht._get_store().get_task("t-h1")
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored.get("commit_result"), "refused")


class H2MergeCommitE2E(GitBase):
    """H-2 probe f2: interval merge commit must REFUSE."""

    def test_merge_in_range_refused(self):
        task_id = "t-h2"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch="agent/opencode/docs-t-h2")
        self._git("checkout", "-b", "side")
        (self.clone / "side.txt").write_text("side\n", encoding="utf-8")
        self._git("add", "side.txt")
        self._git("commit", "-m", "side work")
        self._git("checkout", "main")
        # Task branch must exist for adoption; use it explicitly.
        self._git("checkout", "-b", "agent/opencode/docs-t-h2")
        self._git("merge", "--no-ff", "side", "-m", "merge side")
        code, output = self._run_commit(task_id)
        self.assertEqual(code, 4)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "refused")
        self.assertEqual(payload["reason"], "merge_commit_in_range")

    def test_classifier_merge_pure(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[
                _commit_dict("c1", 1100, parents=["p0"], paths=["a.txt"]),
                _commit_dict("m1", 1200, parents=["c1", "s1"],
                             paths=["a.txt"]),
            ],
            baseline_is_ancestor=True,
            branch="agent/opencode/docs-t1",
            current_branch="agent/opencode/docs-t1",
        )
        self.assertEqual(verdict, "refused")
        self.assertEqual(detail["reason"], "merge_commit_in_range")


class H3InternalPathE2E(GitBase):
    """H-3 probe f3: internal-file penetration must REFUSE."""

    def test_internal_path_in_range_refused(self):
        task_id = "t-h3"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch="agent/opencode/docs-t-h3")
        self._git("checkout", "-b", "agent/opencode/docs-t-h3")
        gate = self.clone / ".herdr-loop" / "GATE.json"
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text("{}", encoding="utf-8")
        self._git("add", "-f", ".herdr-loop/GATE.json")
        self._git("commit", "-m", "internal penetration")
        code, output = self._run_commit(task_id)
        self.assertEqual(code, 4)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "refused")
        self.assertEqual(payload["reason"], "internal_path_in_range")

    def test_internal_path_parity(self):
        for path in (".agent-task-context", ".herdr-loop",
                     ".herdr-loop/GATE.json", ".herdr/x",
                     "delivery.txt", "src/a.py"):
            self.assertEqual(
                is_internal_path(path),
                _ht._is_internal_untracked(path),
                msg=path,
            )


class H4VerifyBaselineContract(GitBase):
    """H-4: machine contract + path-level union."""

    def test_real_delivery_reports_committed_changes(self):
        task_id = "t-h4a"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch="agent/opencode/docs-t-h4a")
        self._git("checkout", "-b", "agent/opencode/docs-t-h4a")
        self._direct_commit("delivery.txt", "deliver\n")
        code, output = self._run_verify(task_id)
        self.assertEqual(code, 0)
        self.assertIn("TASK_CHANGED", output)
        payload = self._payload(output, "HERDR_BASELINE_RESULT")
        self.assertFalse(payload["baseline_match"])
        committed = [c for c in payload["changes"]
                     if c.get("type") == "committed"]
        self.assertTrue(committed)
        self.assertIn("delivery.txt",
                      [c["path"] for c in committed])
        self.assertFalse(payload["worktree_changed"])
        self.assertEqual(payload["commits_ahead"], 1)
        self.assertEqual(payload["baseline_commit"], self.baseline)
        self.assertEqual(payload["head"], self._rev("HEAD"))
        self.assertEqual(payload["basis"], "baseline_commit")

    def test_allow_empty_only_stays_match(self):
        task_id = "t-h4b"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch="agent/opencode/docs-t-h4b")
        self._git("checkout", "-b", "agent/opencode/docs-t-h4b")
        self._git("commit", "--allow-empty", "-m", "empty")
        code, output = self._run_commit(task_id)
        self.assertEqual(code, 3)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "empty")
        code, output = self._run_verify(task_id)
        self.assertEqual(code, 0)
        self.assertIn("BASELINE_MATCH", output)
        result = self._payload(output, "HERDR_BASELINE_RESULT")
        self.assertTrue(result["baseline_match"])
        self.assertEqual(result["changes"], [])


class M1LegacyVerifyClosed(GitBase):
    """M-1/AC3-8: anchor-less verify stays worktree-only."""

    def test_legacy_allow_empty_stays_match(self):
        self._save_task("t-m1", baseline_commit=None,
                         branch="agent/opencode/docs-t-m1")
        self._git("commit", "--allow-empty", "-m", "empty",
                  env=self._date_env(self.now - 600))
        code, output = self._run_verify("t-m1")
        self.assertEqual(code, 0)
        self.assertIn("BASELINE_MATCH", output)
        payload = self._payload(output, "HERDR_BASELINE_RESULT")
        self.assertTrue(payload["baseline_match"])
        self.assertEqual(payload["commits_ahead"], 0)
        self.assertEqual(payload["basis"], "absent")


class L1ResultDomain(GitBase):
    """L-1 probes i/j: frozen result enum + noop_commits."""

    def test_staged_path_reports_created(self):
        task_id = "t-l1a"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch="agent/opencode/docs-t-l1a")
        self._git("checkout", "-b", "agent/opencode/docs-t-l1a")
        (self.clone / "staged.txt").write_text("staged\n", encoding="utf-8")
        code, output = self._run_commit(task_id)
        self.assertEqual(code, 0)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "created")
        stored = _ht._get_store().get_task(task_id)
        self.assertEqual(stored.get("commit_result"), "created")

    def test_adopted_payload_carries_noop_commits(self):
        task_id = "t-l1b"
        self._save_task(task_id, baseline_commit=self.baseline,
                         branch="agent/opencode/docs-t-l1b")
        self._git("checkout", "-b", "agent/opencode/docs-t-l1b")
        self._direct_commit("one.txt", "one\n")
        code, output = self._run_commit(task_id)
        self.assertEqual(code, 0)
        payload = self._payload(output, "HERDR_COMMIT_RESULT")
        self.assertEqual(payload["result"], "adopted")
        self.assertIn("noop_commits", payload)


class M3CloseWorkflowGate(unittest.TestCase):
    """M-3/L-10: escalated git tasks block close without human confirm.

    Behavior assertion (not source grep): an escalated completed+git task
    aborts close_workflow, while --accept-escalated/--force/--abandon or
    supersede clears the gate.
    """

    def _tasks(self, escalated=True):
        return {
            "tasks": [
                {
                    "task_id": "t-m3",
                    "workflow_id": "wf-m3",
                    "status": "completed",
                    "integration_mode": "git",
                    "finalize_escalated": escalated,
                }
            ]
        }

    def test_escalated_blocks_close_without_confirm(self):
        with patch.object(_ht, "load_tasks",
                          return_value=self._tasks(True)), \
             patch.object(_ht, "_load_workflow_entry",
                          return_value=(None, {})), \
             patch.object(_ht, "load_workflows",
                          return_value={"workflows": {}}):
            with self.assertRaises(SystemExit) as cm:
                _ht.close_workflow("wf-m3")
            self.assertEqual(cm.exception.code, 2)

    def test_accept_escalated_or_force_or_abandon_closes(self):
        for kwargs in ({"accept_escalated": True}, {"force": True},
                       {"abandon": True}):
            with patch.object(_ht, "load_tasks",
                              return_value=self._tasks(True)), \
                 patch.object(_ht, "_load_workflow_entry",
                              return_value=(None, {})), \
                 patch.object(_ht, "_finalize_one",
                              return_value={"task_id": "t-m3",
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
                    _ht.close_workflow("wf-m3", **kwargs)
                self.assertIn("[CLOSE REPORT]", buf.getvalue())

    def test_superseded_escalated_does_not_block(self):
        tasks = {"tasks": [
            {"task_id": "t-m3", "workflow_id": "wf-m3",
             "status": "superseded", "integration_mode": "git",
             "finalize_escalated": True}]}
        with patch.object(_ht, "load_tasks", return_value=tasks), \
             patch.object(_ht, "_load_workflow_entry",
                          return_value=(None, {})), \
             patch.object(_ht, "_finalize_one",
                          return_value={"task_id": "t-m3",
                                        "status": "superseded",
                                        "action": "retained"}), \
             patch.object(_ht, "_workflow_stage_tabs",
                          return_value={"tab_ids": [],
                                        "workspace_id": None,
                                        "coordinator_pane": None,
                                        "owned_pane_ids": set()}), \
             patch.object(_ht, "_mark_workflow_completed"), \
             patch.object(_ht, "stage_reset"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _ht.close_workflow("wf-m3")
            self.assertIn("[CLOSE REPORT]", buf.getvalue())


class M2RetryAccounting(unittest.TestCase):
    """M-2/AC4-2/AC4-3: rc=3/4 never consume budget; rc=4 never logs retry."""

    def setUp(self):
        self.ctrl = _ctrl
        self.task = {
            "task_id": "t-m2",
            "workflow_id": "wf-m2",
            "status": "completed",
            "integration_mode": "git",
        }

    def tearDown(self):
        self.ctrl.attention_clear("t-m2:finalize")
        self.ctrl._finalize_retry_exhausted_logged.discard("t-m2")

    def _drive(self, rc, payload):
        stdout_lines = [
            "Task has no changes to commit: t-m2",
            "HERDR_COMMIT_RESULT=" + json.dumps(payload),
        ]
        commit_proc = subprocess.CompletedProcess(
            [], rc, "\n".join(stdout_lines) + "\n", "")
        store = MagicMock()

        def _fake_run(cmd, **kwargs):
            if "commit" in cmd:
                return commit_proc
            return subprocess.CompletedProcess([], 0, "", "")

        get_seq = [dict(self.task), dict(self.task)]

        def _fake_get(_tid):
            if len(get_seq) > 1:
                return get_seq.pop(0)
            return dict(self.task)

        with patch.object(self.ctrl.subprocess, "run",
                           side_effect=_fake_run), \
            patch.object(self.ctrl, "get_task",
                         side_effect=_fake_get), \
            patch.object(self.ctrl, "set_task_status",
                         return_value=True), \
            patch.object(self.ctrl, "_get_store",
                         return_value=store), \
            patch.object(self.ctrl, "enqueue_stage_advance"), \
            patch.object(self.ctrl, "ensure_no_git_processes",
                         return_value=None), \
            patch("herdr.kernel.update_task_metadata",
                  return_value={}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.ctrl._check_finalize_retry(
                    dict(self.task), "completed", 1700000000.0)
        episode = self.ctrl.attention_get("t-m2:finalize") or {}
        return buf.getvalue(), int(episode.get("attempts") or 0)

    def test_refused_consumes_no_budget_and_no_retry_log(self):
        output, attempts = self._drive(
            4, {"task_id": "t-m2", "result": "refused",
                "reason": "merge_commit_in_range"})
        self.assertEqual(attempts, 0)
        self.assertNotIn("commit_retry", output)
        self.assertIn("[FINALIZE REFUSED]", output)

    def test_empty_consumes_no_budget(self):
        _output, attempts = self._drive(
            3, {"task_id": "t-m2", "result": "empty",
                "head": "abc", "basis": "baseline_commit"})
        self.assertEqual(attempts, 0)

    def test_busy_consumes_budget(self):
        output, attempts = self._drive(
            75, {"task_id": "t-m2", "result": "empty", "head": "abc"})
        # 75 is transient: retryable, budget consumed on unchanged status.
        self.assertEqual(attempts, 1)
        self.assertIn("commit_retry", output)


class L2EmptyReleasable(unittest.TestCase):
    def test_time_basis_releasable_without_anchor(self):
        # H-3: only legacy anchor-less time-basis empties release.
        self.assertTrue(
            _ctrl._empty_auto_releasable({"commit_basis": "time"}))

    def test_anchored_empty_requires_human(self):
        # H-3: anchored EMPTY (the only form commit_task persists for
        # modern git tasks) never auto-releases to cleanup_ready.
        self.assertFalse(
            _ctrl._empty_auto_releasable({"baseline_commit": "abc"}))
        self.assertFalse(
            _ctrl._empty_auto_releasable(
                {"baseline_commit": "abc", "commit_basis": "baseline_commit"}))

    def test_basis_absent_escalates(self):
        self.assertFalse(_ctrl._empty_auto_releasable({}))


class L3IntegratedCommit(GitBase):
    """L-3/AR-4: rebase writes integrated_commit without covering commit."""

    def _build_origin(self):
        origin = self.root / "origin.git"
        seed = self.root / "seed"
        main_repo = self.root / "mrepo"
        self.clone = self.root / "tclone"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                       text=True, capture_output=True, check=True)
        seed.mkdir()
        self._git("init", "-b", "main", cwd=seed)
        self._git("config", "user.email", "test@example.com", cwd=seed)
        self._git("config", "user.name", "herdr-test", cwd=seed)
        (seed / "file.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "file.txt", cwd=seed)
        self._git("commit", "-m", "seed", cwd=seed)
        self._git("remote", "add", "origin", str(origin), cwd=seed)
        self._git("push", "-u", "origin", "main", cwd=seed)
        subprocess.run(["git", "clone", str(origin), str(self.clone)],
                       text=True, capture_output=True, check=True)
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "herdr-test")
        self._git("checkout", "-b", "agent/opencode/docs-t-l3")
        (self.clone / "file.txt").write_text("task\n", encoding="utf-8")
        self._git("add", "file.txt")
        self._git("commit", "-m", "task work")
        (seed / "other.txt").write_text("origin\n", encoding="utf-8")
        self._git("add", "other.txt", cwd=seed)
        self._git("add", "file.txt", cwd=seed)
        self._git("commit", "-m", "origin advance", cwd=seed)
        self._git("push", "origin", "main", cwd=seed)
        subprocess.run(["git", "clone", str(origin), str(main_repo)],
                       text=True, capture_output=True, check=True)
        return main_repo

    def test_integrate_records_integrated_commit(self):
        main_repo = self._build_origin()
        head_before = self._rev("HEAD")
        task = {
            "task_id": "t-l3",
            "workflow_id": "wf-l3",
            "run_id": "run-l3",
            "status": "committed",
            "stage": "implementation",
            "node": "implementation",
            "agent": "opencode",
            "clone_path": str(self.clone),
            "branch": "agent/opencode/docs-t-l3",
            "base_branch": "main",
            "source_repo": str(main_repo),
            "integration_mode": "git",
            "commit": head_before,
            "created_at": self.created_at,
            "baseline_fingerprint": {"tracked": {}, "untracked": {}},
        }
        _ht._get_store().save_task(task)
        with patch.object(_ht, "project_for_workflow",
                           return_value={"project_root": str(main_repo),
                                         "base_branch": "main"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _ht.integrate_task("t-l3")
        stored = _ht._get_store().get_task("t-l3")
        self.assertEqual(stored["status"], "integrated")
        self.assertEqual(stored["commit"], head_before)
        self.assertIn("integrated_commit", stored)
        self.assertIn("integration_head_before_rebase", stored)
        self.assertEqual(stored["integration_head_before_rebase"],
                         head_before)


if __name__ == "__main__":
    unittest.main()
