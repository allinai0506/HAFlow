"""Regression tests for:
1. Finalize non-retryable failure when base ref is missing, and fallback to local main repo.
2. Sentinel watchdog pre-check: commit activity in clone protects tasks from false hard timeout kills.
3. Dangling replacement obligation convergence on implementation nodes when node delivery is settled.
"""

import importlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from herdr.state_store import SQLiteStateStore

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_controller(name="ctrl_finalize_closure_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_sentinel(name="sentinel_closure_test"):
    path = HERDR_ROOT / "services" / "herdr-sentinel.py"
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestFinalizeMissingBaseRef(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller()

    def test_finalize_remote_ref_missing_fails_fast_as_non_retryable(self):
        """When integrate fails because remote ref is missing, finalize must NOT retry 5x."""
        task = {
            "task_id": "t-console-missing-ref",
            "workflow_id": "wf-1029",
            "run_id": "run-1",
            "node": "implementation",
            "stage": "implementation",
            "status": "committed",
            "agent": "codex",
            "integration_mode": "git",
            "commit": "5836a0ffc0e456f49fca79afb89806d33ca2cbf1",
        }

        # Simulate herdr-task integrate output when remote ref doesn't exist
        mock_proc = subprocess.CompletedProcess(
            args=["herdr-task", "integrate", "t-console-missing-ref"],
            returncode=7,
            stdout='[INTEGRATE REFUSED] base_ref_missing: docs/workflow-decision-audit-v1\nHERDR_INTEGRATE_RESULT={"result": "base_ref_missing", "base_branch": "docs/workflow-decision-audit-v1"}\n',
            stderr="fatal: couldn't find remote ref docs/workflow-decision-audit-v1\n",
        )

        calls = []

        def _fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock_proc

        with patch.object(self.ctrl.subprocess, "run", side_effect=_fake_run), \
             patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(self.ctrl, "_escalate_finalize") as mock_esc, \
             patch.object(self.ctrl, "set_task_status") as mock_set_status, \
             patch.object(self.ctrl, "_get_store"), \
             patch.object(self.ctrl, "ensure_no_git_processes", return_value=None):

            buf = io.StringIO()
            with redirect_stdout(buf):
                res = self.ctrl.finalize_completed_task("t-console-missing-ref")

            # Must be non-retryable and escalated immediately!
            self.assertFalse(res.get("retryable"), "Missing base ref must be non-retryable")
            self.assertEqual(res.get("kind"), "refused")
            self.assertEqual(res.get("reason"), "integrate_base_ref_missing")
            mock_esc.assert_called_once()
            self.assertEqual(mock_esc.call_args[0][1], "integrate_base_ref_missing")
            mock_set_status.assert_not_called()

    def test_finalize_unhandled_remote_ref_error_string_fails_fast(self):
        """Even with generic exit code 1, 'fatal: couldn't find remote ref' must fail fast."""
        task = {
            "task_id": "t-console-err",
            "workflow_id": "wf-1029",
            "run_id": "run-1",
            "node": "implementation",
            "stage": "implementation",
            "status": "committed",
            "agent": "codex",
            "integration_mode": "git",
            "commit": "5836a0ffc0e456f49fca79afb89806d33ca2cbf1",
        }

        mock_proc = subprocess.CompletedProcess(
            args=["herdr-task", "integrate", "t-console-err"],
            returncode=1,
            stdout="",
            stderr="fatal: couldn't find remote ref docs/workflow-decision-audit-v1\nTraceback...\n",
        )

        with patch.object(self.ctrl.subprocess, "run", return_value=mock_proc), \
             patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(self.ctrl, "_escalate_finalize") as mock_esc, \
             patch.object(self.ctrl, "_get_store"), \
             patch.object(self.ctrl, "ensure_no_git_processes", return_value=None):

            buf = io.StringIO()
            with redirect_stdout(buf):
                res = self.ctrl.finalize_completed_task("t-console-err")

            self.assertFalse(res.get("retryable"), "Error containing 'couldn't find remote ref' must be non-retryable")
            self.assertEqual(res.get("reason"), "integrate_base_ref_missing")
            mock_esc.assert_called_once()


class TestSentinelHardTimeoutCommitProtection(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.sentinel = _load_sentinel()
        notification_patch = patch.object(self.sentinel, "notify_stall")
        self.notify_stall = notification_patch.start()
        self.addCleanup(notification_patch.stop)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_sentinel_hard_timeout_respects_recent_git_commit_in_clone(self):
        """If updated_at is old but a git commit occurred recently in clone, idle time resets."""
        now = 10000.0
        # Initialize a real git repo for the clone
        clone_dir = self.root / "clone-qoder"
        clone_dir.mkdir()
        subprocess.run(["git", "init"], cwd=clone_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=clone_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=clone_dir, check=True)

        test_file = clone_dir / "code.py"
        test_file.write_text("print('hello')")
        subprocess.run(["git", "add", "."], cwd=clone_dir, check=True)
        # Commit at now - 300s (9700)
        env = {**os.environ, "GIT_AUTHOR_DATE": "@9700 +0000", "GIT_COMMITTER_DATE": "@9700 +0000"}
        subprocess.run(["git", "commit", "-m", "recent work"], cwd=clone_dir, env=env, check=True)

        task = {
            "task_id": "t-qoder-active",
            "workflow_id": "wf-1",
            "status": "working",
            "updated_at": now - 3700.0,  # 3700s ago > 3600s
            "clone_path": str(clone_dir),
            "version": 1,
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        state = {}
        with patch.object(self.sentinel, "_get_store", return_value=self.store), \
             patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT": "3600"}):
            self.sentinel.check_task_stalls([task], state, now=now)

        # Task MUST NOT be failed because recent commit was at 9700 (300s ago)
        reloaded = self.store.get_task("t-qoder-active")
        self.assertEqual(reloaded["status"], "working", "Task with recent commit must remain working")

    def test_sentinel_hard_timeout_preserves_work_when_commit_produced(self):
        """When hard timeout expires but the task produced a new commit ahead of baseline, do not kill destructively."""
        now = 10000.0
        clone_dir = self.root / "clone-qoder-finished"
        clone_dir.mkdir()
        subprocess.run(["git", "init"], cwd=clone_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=clone_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=clone_dir, check=True)

        # Baseline commit at 5000
        test_file = clone_dir / "code.py"
        test_file.write_text("initial")
        subprocess.run(["git", "add", "."], cwd=clone_dir, check=True)
        env0 = {**os.environ, "GIT_AUTHOR_DATE": "@5000 +0000", "GIT_COMMITTER_DATE": "@5000 +0000"}
        subprocess.run(["git", "commit", "-m", "initial baseline"], cwd=clone_dir, env=env0, check=True)
        base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone_dir, text=True).strip()

        # Work commit at 6000 (now is 10000, idle 4000s)
        test_file.write_text("finished work")
        subprocess.run(["git", "add", "."], cwd=clone_dir, check=True)
        env1 = {**os.environ, "GIT_AUTHOR_DATE": "@6000 +0000", "GIT_COMMITTER_DATE": "@6000 +0000"}
        subprocess.run(["git", "commit", "-m", "completed code and tests"], cwd=clone_dir, env=env1, check=True)
        work_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone_dir, text=True).strip()

        task = {
            "task_id": "t-qoder-done",
            "workflow_id": "wf-1",
            "status": "working",
            "updated_at": now - 4000.0,
            "baseline_commit": base_commit,
            "clone_path": str(clone_dir),
            "version": 1,
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        state = {}
        # Pane agent is idle (finished its job)
        with patch.object(self.sentinel, "_get_store", return_value=self.store), \
             patch.object(self.sentinel, "_pane_delivery_evidence", return_value={"agent_status": "idle"}), \
             patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT": "3600"}):
            self.sentinel.check_task_stalls([task], state, now=now)

        reloaded = self.store.get_task("t-qoder-done")
        # Should record attention / transition to agent_done or record commit, NOT fail with loss of work
        self.assertNotEqual(reloaded["status"], "failed", "Task with completed commits should not be blindly marked failed")
        self.assertEqual(reloaded.get("commit") or reloaded.get("candidate_sha") or work_commit, work_commit)


class TestReplacementObligationConvergence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.ctrl = _load_controller()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_converge_dangling_obligation_on_implementation_node(self):
        """A superseded task in an implementation node converges when the node's deliverable is settled."""
        from services.controller import infra_recovery

        wf_id = "wf-impl-1"
        # PR #339 评审遗留：证据与义务都必须绑定当前 execution。
        self.store.save_workflow({
            "workflow_id": wf_id,
            "status": "running",
            "candidate_sha": "a" * 40,
            "execution_id": "exec-impl-1",
        })

        # Old superseded task with dangling replacement_pending=True
        self.store.save_task({
            "task_id": "impl-old",
            "workflow_id": wf_id,
            "node": "implementation",
            "stage": "implementation",
            "status": "superseded",
            "replacement_pending": True,
            "superseded_by": None,
            "execution_id": "exec-impl-1",
        })

        # Another task in the same implementation node reached integrated/cleaned
        self.store.save_task({
            "task_id": "impl-settled",
            "workflow_id": wf_id,
            "node": "implementation",
            "stage": "implementation",
            "status": "cleaned",
            "candidate_sha": "a" * 40,
            "execution_id": "exec-impl-1",
        })

        ports = MagicMock()
        ports._get_store.return_value = self.store
        ports.load_tasks.return_value = self.store.list_tasks(workflow_id=wf_id)
        ports.workflow_closed.return_value = False
        ports._scheduler_current_frozen_candidate_sha.return_value = "a" * 40

        converged = infra_recovery.converge_dangling_replacement_obligations(ports, wf_id)
        self.assertTrue(converged, "Dangling obligation on implementation node must converge")

        reloaded = self.store.get_task("impl-old")
        self.assertFalse(reloaded.get("replacement_pending"), "replacement_pending must be cleared")

        events = self.store.list_events(workflow_id=wf_id, event_type="replacement_obligation_auto_resolved")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["task_id"], "impl-old")

    def test_converge_rejects_historical_delivery_on_different_candidate(self):
        """历史终态交付 candidate 与当前冻结候选不一致时不得清掉替换义务（PR #360 评审遗留）。"""
        from services.controller import infra_recovery

        wf_id = "wf-impl-2"
        self.store.save_workflow({"workflow_id": wf_id, "status": "running",
                                  "execution_id": "exec-impl-2"})

        self.store.save_task({
            "task_id": "impl-old",
            "workflow_id": wf_id,
            "node": "implementation",
            "stage": "implementation",
            "status": "superseded",
            "replacement_pending": True,
            "superseded_by": None,
            "execution_id": "exec-impl-2",
        })
        # 旧的 cleaned 交付落在另一个 candidate 上：它不能替新义务完成交付
        self.store.save_task({
            "task_id": "impl-settled",
            "execution_id": "exec-impl-2",
            "workflow_id": wf_id,
            "node": "implementation",
            "stage": "implementation",
            "status": "cleaned",
            "candidate_sha": "b" * 40,
        })

        ports = MagicMock()
        ports._get_store.return_value = self.store
        ports.load_tasks.return_value = self.store.list_tasks(workflow_id=wf_id)
        ports.workflow_closed.return_value = False
        ports._scheduler_current_frozen_candidate_sha.return_value = "a" * 40

        converged = infra_recovery.converge_dangling_replacement_obligations(ports, wf_id)
        self.assertFalse(converged, "Stale-candidate delivery must not clear the replacement obligation")
        reloaded = self.store.get_task("impl-old")
        self.assertTrue(reloaded.get("replacement_pending"), "replacement_pending must stay pending")
        events = self.store.list_events(workflow_id=wf_id, event_type="replacement_obligation_auto_resolved")
        self.assertEqual(len(events), 0)


class TestIntegrateBaseRefResolution(unittest.TestCase):
    def test_integrate_task_falls_back_to_main_repo_when_remote_base_missing(self):
        """When base_branch does not exist on origin, integrate fetches it from main_repo."""
        from herdr.task_cli_integration import integrate_task

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main_repo = tmp_path / "main"
            main_repo.mkdir()
            subprocess.run(["git", "init"], cwd=main_repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=main_repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=main_repo, check=True)
            (main_repo / "README.md").write_text("main")
            subprocess.run(["git", "add", "."], cwd=main_repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=main_repo, check=True)

            # Create local-only base branch in main_repo
            subprocess.run(["git", "branch", "docs/workflow-decision-audit-v1"], cwd=main_repo, check=True)

            # Clone repo
            clone = tmp_path / "clone"
            subprocess.run(["git", "clone", str(main_repo), str(clone)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=clone, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=clone, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=clone, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=clone, check=True)
            # Create a task branch in clone
            subprocess.run(["git", "checkout", "-b", "agent/codex/task-branch"], cwd=clone, check=True, capture_output=True)
            (clone / "work.txt").write_text("work done")
            subprocess.run(["git", "add", "."], cwd=clone, check=True)
            subprocess.run(["git", "commit", "-m", "task commit"], cwd=clone, check=True)
            task_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone, text=True).strip()

            # Point origin to a bare empty repo that does NOT have docs/workflow-decision-audit-v1
            fake_remote = tmp_path / "remote.git"
            subprocess.run(["git", "init", "--bare", str(fake_remote)], check=True, capture_output=True)
            subprocess.run(["git", "remote", "set-url", "origin", str(fake_remote)], cwd=clone, check=True)

            task = {
                "task_id": "t-integrate-fallback",
                "workflow_id": "wf-1",
                "status": "committed",
                "commit": task_commit,
                "base_branch": "docs/workflow-decision-audit-v1",
                "branch": "agent/codex/task-branch",
                "clone_path": str(clone),
                "source_repo": str(main_repo),
                "integration_mode": "git",
                "baseline_fingerprint": {"tracked": {}, "untracked": []},
            }

            store = SQLiteStateStore(tmp_path / "state.db")
            store.save_workflow({"workflow_id": "wf-1", "config": {}, "execution_id": "wf-1"})
            store.save_task(task)
            project = {"repo_path": str(main_repo)}

            # Running integrate_task should succeed using main_repo fallback instead of crashing with CalledProcessError
            buf = io.StringIO()
            with redirect_stdout(buf):
                integrate_task(
                    "t-integrate-fallback",
                    load_tasks=lambda: {"tasks": [task]},
                    find_task=lambda data, tid: task,
                    get_store=lambda: store,
                    ensure_branch_available=lambda *a, **k: None,
                    branch_ownership_error=lambda *a, **k: None,
                    set_status=lambda *a, **k: True,
                    clear_finalize_escalation=lambda *a: None,
                    publish_integrated_task=lambda *a: None,
                    git_head_sha=lambda path: subprocess.check_output(["git", "-C", path, "rev-parse", "HEAD"], text=True).strip(),
                    git_is_ancestor=lambda path, a, b: subprocess.run(["git", "-C", path, "merge-base", "--is-ancestor", a, b]).returncode == 0,
                    current_workspace_fingerprint=lambda path: {"tracked": {}, "untracked": []},
                    project_for_workflow=lambda wid: project,
                )

            self.assertIn("[INTEGRATED]", buf.getvalue())

    def test_integrate_task_exits_7_when_base_ref_missing_everywhere(self):
        """When base_branch does not exist on origin nor in main_repo, integrate exits with rc=7."""
        from herdr.task_cli_integration import integrate_task

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            main_repo = tmp_path / "main"
            main_repo.mkdir()
            subprocess.run(["git", "init"], cwd=main_repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=main_repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=main_repo, check=True)
            (main_repo / "README.md").write_text("main")
            subprocess.run(["git", "add", "."], cwd=main_repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=main_repo, check=True)

            clone = tmp_path / "clone"
            subprocess.run(["git", "clone", str(main_repo), str(clone)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=clone, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=clone, check=True)

            # Point origin to empty bare remote
            fake_remote = tmp_path / "remote.git"
            subprocess.run(["git", "init", "--bare", str(fake_remote)], check=True, capture_output=True)
            subprocess.run(["git", "remote", "set-url", "origin", str(fake_remote)], cwd=clone, check=True)

            # Create a task branch in clone so symbolic-ref matches
            subprocess.run(["git", "checkout", "-b", "agent/codex/task-branch"], cwd=clone, check=True, capture_output=True)
            (clone / "work.txt").write_text("work done")
            subprocess.run(["git", "add", "."], cwd=clone, check=True)
            subprocess.run(["git", "commit", "-m", "task commit"], cwd=clone, check=True)
            task_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=clone, text=True).strip()

            task = {
                "task_id": "t-integrate-missing",
                "workflow_id": "wf-1",
                "status": "committed",
                "commit": task_commit,
                "base_branch": "non-existent-base-branch",
                "branch": "agent/codex/task-branch",
                "clone_path": str(clone),
                "source_repo": str(main_repo),
                "integration_mode": "git",
                "baseline_fingerprint": {"tracked": {}, "untracked": []},
            }

            store = SQLiteStateStore(tmp_path / "state.db")
            store.save_workflow({"workflow_id": "wf-1", "config": {}, "execution_id": "wf-1"})
            store.save_task(task)
            project = {"repo_path": str(main_repo)}

            with self.assertRaises(SystemExit) as ctx:
                integrate_task(
                    "t-integrate-missing",
                    load_tasks=lambda: {"tasks": [task]},
                    find_task=lambda data, tid: task,
                    get_store=lambda: store,
                    ensure_branch_available=lambda *a, **k: None,
                    branch_ownership_error=lambda *a, **k: None,
                    set_status=lambda *a, **k: True,
                    clear_finalize_escalation=lambda *a: None,
                    publish_integrated_task=lambda *a: None,
                    git_head_sha=lambda path: "abcdef",
                    git_is_ancestor=lambda path, a, b: False,
                    current_workspace_fingerprint=lambda path: {"tracked": {}, "untracked": []},
                    project_for_workflow=lambda wid: project,
                )

            self.assertEqual(ctx.exception.code, 7, "Missing base ref must exit with rc=7")


if __name__ == "__main__":
    unittest.main()
