"""Tests for SC-017: Verification and elimination of concurrent task overwrite on herdr-task add."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
REAL_PYTHON = sys.executable


class TaskAddOverwriteVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="task-add-sc017-"))
        self.env = dict(os.environ)
        self.env["HERDR_STATE_DB"] = str(self.tmp / "state.db")
        self.env["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task_cli(self, *args):
        return subprocess.run(
            [REAL_PYTHON, str(HERDR_ROOT / "bin" / "herdr-task"), *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def _get_task(self, task_id):
        code = (
            "import os, sys, json; sys.path.insert(0, r'%s');"
            "from pathlib import Path; from herdr import state_db;"
            "t = state_db.get_task('%s', db_path=Path(os.environ['HERDR_STATE_DB']));"
            "print(json.dumps(t))"
        ) % (HERDR_ROOT, task_id)
        res = subprocess.run([REAL_PYTHON, "-c", code], env=self.env, check=True, capture_output=True, text=True)
        import json
        return json.loads(res.stdout.strip())

    def _seed_workflow_and_task(self, task_id, status="working"):
        code = (
            "import os, sys; sys.path.insert(0, r'%s');"
            "from pathlib import Path; from herdr import state_db;"
            "db = Path(os.environ['HERDR_STATE_DB']);"
            "state_db.save_workflow({'workflow_id':'wf-test','project_id':'proj1','title':'t','status':'running','config':{}}, db_path=db);"
            "state_db.save_task({'task_id':'%s','workflow_id':'wf-test','node':'n1','stage':'n1',"
            "'agent':'claude','status':'%s','goal':'initial goal','payload_json':{}}, db_path=db)"
        ) % (HERDR_ROOT, task_id, status)
        subprocess.run([REAL_PYTHON, "-c", code], env=self.env, check=True, capture_output=True)

    def test_add_task_does_not_overwrite_or_regress_existing_task_status(self):
        """SC-017 core regression: add_task must only write the new task.

        Existing tasks that have transitioned to working/completed must NEVER be
        overwritten or have their status regressed by a stale snapshot in add_task.
        """
        # 1. Seed an existing task already in 'working' status
        self._seed_workflow_and_task("T-EXISTING", status="working")
        before = self._get_task("T-EXISTING")
        self.assertEqual(before["status"], "working")
        initial_version = before.get("version", 1)

        # 2. Add a new task via herdr-task add
        res = self._task_cli(
            "add",
            "--task-id", "T-NEW",
            "--workflow-id", "wf-test",
            "--node", "n2",
            "--workspace", "ws1",
            "--pane", "p1",
            "--agent", "codex",
            "--goal", "new task goal",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Task registered: T-NEW", res.stdout)

        # 3. Verify T-NEW was successfully registered in 'pending' status
        new_task = self._get_task("T-NEW")
        self.assertEqual(new_task["status"], "pending")
        self.assertEqual(new_task["goal"], "new task goal")

        # 4. Verify T-EXISTING status and version remain completely intact
        after = self._get_task("T-EXISTING")
        self.assertEqual(after["status"], "working", "Existing task status regressed!")
        self.assertEqual(after.get("version", 1), initial_version, "Existing task version was modified!")

    def test_add_task_rejects_duplicate_task_id(self):
        """herdr-task add must fail-closed if the task_id already exists."""
        self._seed_workflow_and_task("T-DUP", status="working")

        # Attempt to add with the same task_id
        res = self._task_cli(
            "add",
            "--task-id", "T-DUP",
            "--workflow-id", "wf-test",
            "--node", "n3",
            "--workspace", "ws1",
            "--pane", "p1",
            "--agent", "codex",
            "--goal", "duplicate goal attempt",
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("Task already exists: T-DUP", res.stdout + res.stderr)

        # Verify original task is untouched
        dup_task = self._get_task("T-DUP")
        self.assertEqual(dup_task["status"], "working")
        self.assertEqual(dup_task["goal"], "initial goal")

    def test_concurrent_add_task_interleaving_is_atomic(self):
        """Codex P1 Gate regression: concurrent add_task processes for same task_id.

        When two processes attempt to register the exact same new task_id concurrently,
        exactly one must succeed (returncode 0) and the other must be atomically rejected
        with 'Task already exists' (returncode 1). Neither can overwrite the other.
        """
        import concurrent.futures

        # Seed the workflow
        self._seed_workflow_and_task("T-BASE", status="working")

        target_task_id = "T-CONCURRENT"
        args_1 = [
            "add",
            "--task-id", target_task_id,
            "--workflow-id", "wf-test",
            "--node", "n1",
            "--workspace", "ws1",
            "--pane", "p1",
            "--agent", "codex",
            "--goal", "process 1 goal",
        ]
        args_2 = [
            "add",
            "--task-id", target_task_id,
            "--workflow-id", "wf-test",
            "--node", "n1",
            "--workspace", "ws2",
            "--pane", "p2",
            "--agent", "claude",
            "--goal", "process 2 goal",
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(self._task_cli, *args_1)
            f2 = executor.submit(self._task_cli, *args_2)
            res1 = f1.result()
            res2 = f2.result()

        results = [res1, res2]
        returncodes = sorted([r.returncode for r in results])
        self.assertEqual(returncodes, [0, 1], f"Expected exactly one success and one failure: {res1.stdout + res1.stderr} vs {res2.stdout + res2.stderr}")

        success_res = [r for r in results if r.returncode == 0][0]
        failure_res = [r for r in results if r.returncode == 1][0]

        self.assertIn(f"Task registered: {target_task_id}", success_res.stdout)
        self.assertIn(f"Task already exists: {target_task_id}", failure_res.stdout + failure_res.stderr)

        # Database must have exactly one record for target_task_id
        final_task = self._get_task(target_task_id)
        self.assertEqual(final_task["task_id"], target_task_id)
        self.assertEqual(final_task["status"], "pending")
        self.assertIn(final_task["goal"], ["process 1 goal", "process 2 goal"])


if __name__ == "__main__":
    unittest.main()
