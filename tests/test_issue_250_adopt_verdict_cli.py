"""Regression tests for Issue #250: herdr-task adopt-verdict and failed -> completed transition.

Issue #250:
- When a task failed due to watchdog, but has a valid gate-verdict file,
  herdr-task adopt-verdict (and herdr-task set completed --verdict) must allow
  controlled adoption from failed -> completed.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent


class AdoptVerdictCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.verdicts_dir = self.tmp_dir / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True)
        self.tasks_file = self.tmp_dir / "tasks.json"
        self.db_path = self.tmp_dir / "state.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_adopt_verdict_from_failed_task(self):
        from herdr import state_store
        store = state_store.SQLiteStateStore(self.db_path)
        task_id = "wf-1-cleanroom_test-auto"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "created_at": 1000.0,
            "updated_at": 2000.0,
            "version": 4,
        }
        store.save_task(task)

        # Write gate verdict file
        verdict_file = self.verdicts_dir / f"{task_id}.json"
        verdict_file.write_text(
            json.dumps({"verdict": "blocked", "note": "D-1 blocker identified"}),
            encoding="utf-8",
        )

        herdr_task_bin = HERDR_ROOT / "bin" / "herdr-task"
        env = {
            **os.environ,
            "TASKS_FILE": str(self.tasks_file),
            "HERDR_STATE_DB": str(self.db_path),
            "HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir),
            "PYTHONPATH": str(HERDR_ROOT),
        }

        # Run adopt-verdict
        result = subprocess.run(
            [sys.executable, str(herdr_task_bin), "adopt-verdict", task_id],
            text=True,
            capture_output=True,
            env=env,
        )

        self.assertEqual(result.returncode, 0, f"adopt-verdict failed: {result.stderr}\n{result.stdout}")
        self.assertIn("[GATE VERDICT ADOPTED]", result.stdout)

        # Verify task is now completed with stage_verdict
        updated_task = store.get_task(task_id)
        self.assertEqual(updated_task.get("status"), "completed")
        self.assertEqual(updated_task.get("stage_verdict"), "blocked")
        self.assertEqual(updated_task.get("stage_verdict_note"), "D-1 blocker identified")

    def test_set_completed_with_verdict_allows_failed_transition(self):
        from herdr import state_store
        store = state_store.SQLiteStateStore(self.db_path)
        task_id = "wf-1-cleanroom_review-auto"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "cleanroom_review",
            "stage": "cleanroom_review",
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "created_at": 1000.0,
            "updated_at": 2000.0,
            "version": 4,
        }
        store.save_task(task)

        herdr_task_bin = HERDR_ROOT / "bin" / "herdr-task"
        verdict_file = self.verdicts_dir / f"{task_id}.json"
        verdict_file.write_text(
            json.dumps({"verdict": "blocked", "note": "F-1 review blocker"}),
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "TASKS_FILE": str(self.tasks_file),
            "HERDR_STATE_DB": str(self.db_path),
            "HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir),
            "PYTHONPATH": str(HERDR_ROOT),
        }

        result = subprocess.run(
            [sys.executable, str(herdr_task_bin), "set", task_id, "completed",
             "--verdict", "blocked", "--note", "F-1 review blocker"],
            text=True,
            capture_output=True,
            env=env,
        )

        self.assertEqual(result.returncode, 0, f"set failed: {result.stderr}\n{result.stdout}")
        updated_task = store.get_task(task_id)
        self.assertEqual(updated_task.get("status"), "completed")
        self.assertEqual(updated_task.get("stage_verdict"), "blocked")
        self.assertEqual(updated_task.get("stage_verdict_note"), "F-1 review blocker")
