"""Regression tests for Issue #250: Sentinel hard timeout watchdog gate verdict awareness.

Issue #250:
- Gate nodes (e.g. cleanroom_test / cleanroom_review) had already written gate-verdict
  files, but were prematurely failed by Sentinel's hard_timeout_watchdog.
- Sentinel must recognize that a valid gate-verdict file exists and suppress auto-failing
  the task.
"""

import importlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_sentinel():
    return importlib.import_module("services.herdr-sentinel")


class SentinelGateVerdictWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.verdicts_dir = self.tmp_dir / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_verdict_file_suppresses_hard_timeout(self):
        sentinel = _load_sentinel()
        task_id = "wf-test-gate-01-cleanroom_test-auto"
        verdict_path = self.verdicts_dir / f"{task_id}.json"
        verdict_path.write_text(
            json.dumps({"verdict": "blocked", "note": "D-1 blocker identified"}),
            encoding="utf-8",
        )

        now = 10000.0
        # updated_at was 4000s ago -> idle_seconds = 4000 > hard_timeout (3600)
        task = {
            "task_id": task_id,
            "workflow_id": "wf-test-gate-01",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "working",
            "updated_at": now - 4000.0,
            "version": 3,
        }

        state = {"stalls": {}, "timeouts": {}}
        mock_store = MagicMock()

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(sentinel, "_get_store", return_value=mock_store), \
             patch.object(sentinel, "update_statuses") as mock_update_statuses:
            changed = sentinel.check_task_stalls([task], state, now=now)

            # update_statuses must NOT have been called to fail this task
            mock_update_statuses.assert_not_called()
            # task must not be in timeouts failed record
            self.assertNotIn(task_id, state.get("timeouts", {}))

    def test_non_gate_task_still_times_out(self):
        sentinel = _load_sentinel()
        task_id = "wf-test-normal-impl"
        now = 10000.0
        task = {
            "task_id": task_id,
            "workflow_id": "wf-test-normal",
            "node": "implementation",
            "stage": "implementation",
            "status": "working",
            "updated_at": now - 4000.0,
            "version": 3,
        }

        state = {"stalls": {}, "timeouts": {}}
        mock_store = MagicMock()

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(sentinel, "_get_store", return_value=mock_store), \
             patch.object(sentinel, "update_statuses", return_value=True) as mock_update_statuses:
            changed = sentinel.check_task_stalls([task], state, now=now)

            mock_update_statuses.assert_called_once_with(
                {task_id: ("failed", "hard_timeout_watchdog")},
                expected={task_id: "working"},
                versions={task_id: 3},
            )
            self.assertTrue(changed)
            self.assertIn(task_id, state["timeouts"])
