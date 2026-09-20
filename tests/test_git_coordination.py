import importlib
import tempfile
import unittest
from pathlib import Path


git_coordination = importlib.import_module("herdr.git_coordination")


class GitCoordinationTest(unittest.TestCase):
    def test_active_task_cannot_reuse_branch(self):
        tasks = [
            {"task_id": "task-a", "status": "committed", "branch": "agent/opencode/feat/shared"},
            {"task_id": "task-b", "status": "working", "branch": "agent/opencode/feat/other"},
        ]

        with self.assertRaises(git_coordination.BranchOwnershipError):
            git_coordination.ensure_branch_available(
                "agent/opencode/feat/shared", tasks, task_id="task-b"
            )

    def test_same_task_may_reuse_its_owned_branch(self):
        tasks = [
            {"task_id": "task-a", "status": "committed", "branch": "agent/opencode/feat/shared"},
        ]

        git_coordination.ensure_branch_available(
            "agent/opencode/feat/shared", tasks, task_id="task-a"
        )

    def test_git_operation_lock_is_shared_by_same_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "clone"
            repo.mkdir()
            first = git_coordination.GitOperationLock(repo, lock_root=Path(tmp) / "locks")
            second = git_coordination.GitOperationLock(repo, lock_root=Path(tmp) / "locks")
            with first:
                self.assertFalse(second.try_acquire())
            self.assertTrue(second.try_acquire())
            second.release()

    def test_git_process_check_blocks_retry_when_process_is_present(self):
        with self.assertRaises(git_coordination.GitProcessActiveError):
            git_coordination.ensure_no_git_processes(
                "/tmp/example-clone",
                process_provider=lambda: [("123", "git -C /tmp/example-clone rebase origin/dev")],
            )


if __name__ == "__main__":
    unittest.main()
