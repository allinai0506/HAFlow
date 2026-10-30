"""Regression tests for Issue #250: Controller gate verdict recovery for receipt-v1 and adoption from failed.

Issue #250:
1. In `services/herdr-controller.py` `handle_event`:
   `if task.get("completion_protocol") == "receipt-v1": return` executed before
   `gate_verdict_ready(task)`, preventing idle gate workers from reaching `agent_done`
   and auto-adopting the verdict.
2. In `try_auto_verdict`:
   Tasks failed by watchdog with valid gate verdicts were rejected because
   `status != "agent_done"`.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent


def _load_controller(name="ctrl_issue_250_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ControllerGateVerdictRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller()
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.verdicts_dir = self.tmp_dir / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_handle_event_recovers_receipt_v1_gate_task_when_verdict_ready(self):
        task_id = "wf-1-cleanroom_test-auto"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "working",
            "completion_protocol": "receipt-v1",
            "pane_id": "p1",
            "version": 4,
        }

        # Mock gate_verdict_ready -> True and _gate_verdict_is_fresh -> True
        with patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(self.ctrl, "_completion_marker_snapshot", return_value=(False, "")), \
             patch.object(self.ctrl, "_gate_verdict_is_fresh", return_value=True), \
             patch.object(self.ctrl, "gate_verdict_ready", return_value=True), \
             patch.object(self.ctrl, "_set_observed_status", return_value=True) as mock_set_status, \
             patch.object(self.ctrl, "emit_done_if_allowed") as mock_emit_done:

            self.ctrl.handle_event(task_id, "idle")

            mock_set_status.assert_called_once_with(task, "agent_done", "gate_verdict_recovery")
            mock_emit_done.assert_called_once()

    def test_try_auto_verdict_adopts_from_failed_watchdog_task(self):
        task_id = "wf-1-cleanroom_test-auto"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "version": 5,
        }

        # Write gate verdict file to make it fresh on disk
        verdict_file = self.verdicts_dir / f"{task_id}.json"
        verdict_file.write_text(
            json.dumps({"verdict": "blocked", "note": "D1 blocker"}),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(self.ctrl, "get_task", return_value=task), \
             patch.object(self.ctrl, "auto_verdict_enabled", return_value=True), \
             patch.object(self.ctrl, "node_is_gate", return_value=True), \
             patch.object(self.ctrl, "read_gate_verdict", return_value=("blocked", "D1 blocker", "file")), \
             patch.object(self.ctrl, "_verdict_affected_task_ids", return_value=None), \
             patch.object(
                 self.ctrl.subprocess,
                 "run",
                 return_value=subprocess.CompletedProcess([], 0, "ok", ""),
             ) as mock_run:

            res = self.ctrl.try_auto_verdict(task_id)

            self.assertTrue(res)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertIn("set", cmd)
            self.assertIn(task_id, cmd)
            self.assertIn("completed", cmd)
            self.assertIn("--verdict", cmd)
            self.assertIn("blocked", cmd)
            self.assertIn("D1 blocker", cmd)
