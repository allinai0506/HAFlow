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


if __name__ == "__main__":
    unittest.main()
