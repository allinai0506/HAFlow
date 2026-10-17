import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.state_store import get_state_store


class TestTeardownCleanedCommit(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.db_path = self.tmp_dir / "state.db"
        self.tasks_file = self.tmp_dir / "tasks.json"
        self.store = get_state_store(self.db_path)

        cli_path = HERDR_ROOT / "bin" / "herdr-task"
        spec = importlib.util.spec_from_loader(
            "herdr_task_cli",
            importlib.machinery.SourceFileLoader("herdr_task_cli", str(cli_path)),
        )
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleaned_task_can_commit_uncommitted_output_and_clear_guard(self):
        """A cleaned task with uncommitted output can create a preservation commit, clearing teardown guard."""
        repo = self.tmp_dir / "clone"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "base.txt").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")

        # Uncommitted doc output produced by agent
        (repo / "plan.md").write_text("# Plan doc")

        task = {
            "task_id": "task-cleaned-1",
            "workflow_id": "wf-1",
            "node": "plan",
            "stage": "plan",
            "status": "cleaned",
            "clone_path": str(repo),
            "integration_mode": "none",
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        # 1. Before commit: teardown output guard triggers unpreserved_worktree_output
        guard_before = self.cli._teardown_output_guard(task)
        self.assertEqual(guard_before["reason"], "unpreserved_worktree_output")
        # Remediation command must not mention integrate
        self.assertNotIn("integrate", guard_before.get("remediation_cmd", ""))
        self.assertIn("herdr-task commit task-cleaned-1", guard_before.get("remediation_cmd", ""))

        # 2. Commit the uncommitted output
        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["TASKS_FILE"] = str(self.tasks_file)
        self.cli.commit_task("task-cleaned-1", message="chore: preserve plan")

        # 3. Status remains cleaned, commit metadata recorded
        updated = self.store.get_task("task-cleaned-1")
        self.assertEqual(updated["status"], "cleaned")
        self.assertIsNotNone(updated.get("commit"))

        # 4. Teardown output guard is now cleared
        guard_after = self.cli._teardown_output_guard(updated)
        self.assertEqual(guard_after["reason"], "")

    def test_integrated_task_with_uncommitted_output_is_rejected(self):
        """An integrated task with uncommitted output must reject commit and preserve candidate commit and status."""
        repo = self.tmp_dir / "clone-integrated"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "base.txt").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")
        base_commit = git("rev-parse", "HEAD").stdout.strip()

        # Dirty output created after integration
        (repo / "leak.txt").write_text("untracked leak")

        task = {
            "task_id": "task-integrated-1",
            "workflow_id": "wf-1",
            "node": "impl",
            "stage": "impl",
            "status": "integrated",
            "clone_path": str(repo),
            "commit": base_commit,
            "integrated_commit": base_commit,
            "integration_mode": "git",
        }
        self.store.save_task(task)

        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["TASKS_FILE"] = str(self.tasks_file)

        with self.assertRaises(SystemExit) as ctx:
            self.cli.commit_task("task-integrated-1")
        self.assertEqual(ctx.exception.code, 2)

        persisted = self.store.get_task("task-integrated-1")
        self.assertEqual(persisted["status"], "integrated")
        self.assertEqual(persisted["commit"], base_commit)

    def test_integrated_task_clean_is_idempotent(self):
        """An integrated task with clean worktree returns existing commit idempotently."""
        repo = self.tmp_dir / "clone-integrated-clean"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "base.txt").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")
        base_commit = git("rev-parse", "HEAD").stdout.strip()

        task = {
            "task_id": "task-integrated-clean",
            "workflow_id": "wf-1",
            "node": "impl",
            "stage": "impl",
            "status": "integrated",
            "clone_path": str(repo),
            "commit": base_commit,
            "integrated_commit": base_commit,
            "integration_mode": "git",
        }
        self.store.save_task(task)

        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["TASKS_FILE"] = str(self.tasks_file)

        # Should not raise SystemExit
        self.cli.commit_task("task-integrated-clean")

        persisted = self.store.get_task("task-integrated-clean")
        self.assertEqual(persisted["status"], "integrated")
        self.assertEqual(persisted["commit"], base_commit)

    def test_cleaned_git_task_with_uncommitted_output_is_rejected(self):
        """A cleaned git task with uncommitted output must reject commit."""
        repo = self.tmp_dir / "clone-cleaned-git"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "base.txt").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")
        base_commit = git("rev-parse", "HEAD").stdout.strip()

        (repo / "extra.txt").write_text("extra")

        task = {
            "task_id": "task-cleaned-git",
            "workflow_id": "wf-1",
            "node": "impl",
            "stage": "impl",
            "status": "cleaned",
            "clone_path": str(repo),
            "commit": base_commit,
            "integration_mode": "git",
        }
        self.store.save_task(task)

        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["TASKS_FILE"] = str(self.tasks_file)

        with self.assertRaises(SystemExit) as ctx:
            self.cli.commit_task("task-cleaned-git")
        self.assertEqual(ctx.exception.code, 2)

    def test_finalize_none_mode_preservation_commit_failure_is_retryable(self):
        """When preservation commit fails for mode=none, controller leaves task retryable in completed status."""
        spec = importlib.util.spec_from_file_location(
            "herdr_controller",
            str(HERDR_ROOT / "services" / "herdr-controller.py"),
        )
        ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ctl)

        repo = self.tmp_dir / "clone-finalize-fail"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "base.txt").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")

        # Dirty file
        (repo / "untracked.md").write_text("dirty")

        task_id = "task-finalize-fail"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "doc",
            "stage": "doc",
            "status": "completed",
            "clone_path": str(repo),
            "integration_mode": "none",
        }
        self.store.save_task(task)

        # Mock TASK_MANAGER commit to fail
        real_subprocess_run = subprocess.run
        def failing_subprocess_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[1] == "commit":
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="hook error: pre-commit failed")
            return real_subprocess_run(cmd, *args, **kwargs)

        orig_run = ctl.subprocess.run
        ctl.subprocess.run = failing_subprocess_run
        try:
            os.environ["HERDR_STATE_DB"] = str(self.db_path)
            res = ctl.finalize_completed_task(task_id)
            self.assertTrue(res.get("retryable"))
            self.assertEqual(res.get("kind"), "error")
            self.assertEqual(res.get("rc"), 1)

            persisted = self.store.get_task(task_id)
            self.assertEqual(persisted["status"], "completed")  # Did not advance to cleanup_ready!

            events = self.store.list_events(task_id=task_id, event_type="finalize_commit_error")
            self.assertEqual(len(events), 1)
            self.assertIn("hook error: pre-commit failed", events[0]["payload"]["detail"])
        finally:
            ctl.subprocess.run = orig_run

    def test_finalize_rejects_failed_worktree_status_inventory(self):
        """A failure in git status during mode=none finalization must fail-closed and return a retryable error."""
        spec = importlib.util.spec_from_file_location(
            "herdr_controller",
            str(HERDR_ROOT / "services" / "herdr-controller.py"),
        )
        ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ctl)

        repo = self.tmp_dir / "clone-status-fail"
        repo.mkdir()

        task_id = "task-status-fail"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "doc",
            "stage": "doc",
            "status": "completed",
            "clone_path": str(repo),
            "integration_mode": "none",
        }
        self.store.save_task(task)

        real_subprocess_run = subprocess.run
        def failing_git_status_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 4 and cmd[0] == "git" and cmd[3] == "status":
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: not a git repository (or any of the parent directories)")
            return real_subprocess_run(cmd, *args, **kwargs)

        orig_run = ctl.subprocess.run
        ctl.subprocess.run = failing_git_status_run
        try:
            os.environ["HERDR_STATE_DB"] = str(self.db_path)
            res = ctl.finalize_completed_task(task_id)
            self.assertTrue(res.get("retryable"))
            self.assertEqual(res.get("kind"), "error")
            self.assertEqual(res.get("rc"), 128)

            persisted = self.store.get_task(task_id)
            self.assertEqual(persisted["status"], "completed")  # Did not advance to cleanup_ready or cleaned!

            events = self.store.list_events(task_id=task_id, event_type="finalize_status_error")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["rc"], 128)
            self.assertIn("not a git repository", events[0]["payload"]["detail"])
        finally:
            ctl.subprocess.run = orig_run

    def test_finalize_context_mode_plain_workspace_bypasses_git_status(self):
        """A context-mode plain workspace without .git metadata bypasses git status and cleanly reaches cleanup_ready."""
        spec = importlib.util.spec_from_file_location(
            "herdr_controller",
            str(HERDR_ROOT / "services" / "herdr-controller.py"),
        )
        ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ctl)

        context_dir = self.tmp_dir / "context-plain-dir"
        context_dir.mkdir()
        (context_dir / "context_output.json").write_text('{"summary": "ok"}')

        task_id = "task-context-plain"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "context-extract",
            "stage": "context-extract",
            "status": "completed",
            "clone_path": str(context_dir),
            "execution_mode": "context",
            "integration_mode": "none",
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        git_status_called = []
        real_subprocess_run = subprocess.run
        def spy_subprocess_run(cmd, *args, **kwargs):
            if isinstance(cmd, (list, tuple)) and "status" in cmd:
                git_status_called.append(cmd)
            return real_subprocess_run(cmd, *args, **kwargs)

        orig_run = ctl.subprocess.run
        ctl.subprocess.run = spy_subprocess_run
        try:
            os.environ["HERDR_STATE_DB"] = str(self.db_path)
            res = ctl.finalize_completed_task(task_id)
            self.assertEqual(res, {"retryable": False, "kind": "finalized"})
            self.assertEqual(git_status_called, [])

            persisted = self.store.get_task(task_id)
            self.assertEqual(persisted["status"], "cleaned")
            self.assertTrue(context_dir.exists())
            self.assertTrue((context_dir / "context_output.json").exists())
        finally:
            ctl.subprocess.run = orig_run

    def test_check_finalize_retry_drives_none_mode_preservation_retry(self):
        """_check_finalize_retry drives retries for mode=none tasks rather than discarding immediately."""
        spec = importlib.util.spec_from_file_location(
            "herdr_controller",
            str(HERDR_ROOT / "services" / "herdr-controller.py"),
        )
        ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ctl)

        task_id = "task-retry-none"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "doc",
            "stage": "doc",
            "status": "completed",
            "integration_mode": "none",
        }
        self.store.save_task(task)

        finalize_called = []
        def mock_finalize(tid):
            finalize_called.append(tid)
            return {"retryable": True, "kind": "error", "rc": 1}

        orig_finalize = ctl.finalize_completed_task
        ctl.finalize_completed_task = mock_finalize
        try:
            os.environ["HERDR_STATE_DB"] = str(self.db_path)
            now = 1000.0
            ret = ctl._check_finalize_retry(task, "completed", now)
            self.assertTrue(ret)
            self.assertEqual(finalize_called, [task_id])

            # Attention episode should have been noted with reason commit_retry
            ep = ctl.attention_get(f"{task_id}:finalize")
            self.assertIsNotNone(ep)
            self.assertEqual(ep.get("reason"), "commit_retry")
            self.assertEqual(ep.get("attempts"), 1)
        finally:
            ctl.finalize_completed_task = orig_finalize
            ctl.attention_clear(f"{task_id}:finalize")

    def test_completed_none_mode_task_commit_preserves_completed_status_for_cleanup_ready(self):
        """A completed mode=none task creating a preservation commit remains in completed status and can transition to cleanup_ready."""
        repo = self.tmp_dir / "clone-completed-none"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "base.txt").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")

        # Dirty output to preserve
        (repo / "report.txt").write_text("report output")

        task_id = "task-completed-none"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "audit",
            "stage": "audit",
            "status": "completed",
            "clone_path": str(repo),
            "integration_mode": "none",
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["TASKS_FILE"] = str(self.tasks_file)

        # Commit uncommitted output
        self.cli.commit_task(task_id, message="chore(closure): preserve worktree output")

        # Task MUST remain in completed status, not prematurely advanced to committed
        updated = self.store.get_task(task_id)
        self.assertEqual(updated["status"], "completed")
        self.assertIsNotNone(updated.get("commit"))

        # Now controller can successfully transition completed -> cleanup_ready
        from herdr.transitions import validate_task_transition
        self.assertTrue(validate_task_transition("completed", "cleanup_ready"))
        self.store.transition_task(task_id, "cleanup_ready", reason="finalize_complete")
        final = self.store.get_task(task_id)
        self.assertEqual(final["status"], "cleanup_ready")

    def test_completed_legacy_task_missing_integration_mode_preserves_completed_status(self):
        """A completed legacy task omitting integration_mode defaults to mode=none and preserves completed status on preservation commit."""
        repo = self.tmp_dir / "clone-completed-legacy"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (repo / "base.txt").write_text("base")
        git("add", ".")
        git("commit", "-m", "base")

        # Dirty output to preserve
        (repo / "report.txt").write_text("legacy report output")

        task_id = "task-completed-legacy"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "audit",
            "stage": "audit",
            "status": "completed",
            "clone_path": str(repo),
            # "integration_mode" intentionally omitted (legacy task)
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["TASKS_FILE"] = str(self.tasks_file)

        # Commit uncommitted output
        self.cli.commit_task(task_id, message="chore(closure): preserve worktree output")

        # Task MUST remain in completed status, not advanced to committed
        updated = self.store.get_task(task_id)
        self.assertEqual(updated["status"], "completed")
        self.assertIsNotNone(updated.get("commit"))


if __name__ == "__main__":
    unittest.main()
