import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from herdr import direct_dispatch, kernel, state_db
from herdr.state_store import SQLiteStateStore


class TestTaskBypassGate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        # Create sample workflow and blocked gate task
        self.workflow_id = "wf-bypass-test"
        self.task_id = "task-gate-blocked-1"
        self.run_id = "run-001"

        self.wf_record = {
            "workflow_id": self.workflow_id,
            "title": "Bypass Test Workflow",
            "status": "running",
            "template_name": "software-development-v1",
            "config": {"nodes": [{"id": "gate_cleanroom", "label": "Gate"}]},
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.store.save_workflow(self.wf_record)

        self.task_record = {
            "task_id": self.task_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "node": "gate_cleanroom",
            "stage": "gate_cleanroom",
            "status": "blocked",
            "stage_verdict": "blocked",
            "stage_verdict_note": "Lint failed with 12 errors",
            "blocker": "Static checks blocked",
            "agent": "claude",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.store.save_task(self.task_record)

    def tearDown(self):
        self.td.cleanup()

    def test_bypass_blocked_task_success(self):
        reason = "Emergency hotfix approved by Tech Lead"
        operator = "lead_bob"
        note = "Jira Ticket #992"

        res = kernel.bypass_task_gate(
            self.task_id,
            reason=reason,
            operator=operator,
            note=note,
            store=self.store,
        )

        self.assertEqual(res.get("task_id"), self.task_id)
        self.assertEqual(res.get("status"), "completed")
        self.assertEqual(res.get("stage_verdict"), "pass")
        self.assertEqual(res.get("previous_verdict"), "blocked")
        self.assertEqual(res.get("previous_status"), "blocked")
        self.assertEqual(res.get("reason"), reason)
        self.assertEqual(res.get("operator"), operator)

        receipt = res.get("receipt", {})
        self.assertEqual(receipt.get("schema_version"), 1)
        self.assertTrue(receipt.get("bypass_id").startswith("bypass_"))
        self.assertEqual(receipt.get("workflow_id"), self.workflow_id)
        self.assertEqual(receipt.get("node_id"), "gate_cleanroom")
        self.assertEqual(receipt.get("note"), note)

        # Verify persisted task record in DB
        updated = self.store.get_task(self.task_id)
        self.assertEqual(updated.get("status"), "completed")
        self.assertEqual(updated.get("stage_verdict"), "pass")
        self.assertIn(f"[GATE BYPASS by {operator}] {reason}", updated.get("stage_verdict_note"))
        self.assertIsNone(updated.get("blocker"))
        self.assertTrue(updated.get("gate_bypassed"))
        self.assertEqual(updated.get("gate_bypass", {}).get("bypass_id"), receipt["bypass_id"])
        self.assertEqual(updated.get("version"), 2)

        # Verify gate verdict file on disk
        v_path = Path(direct_dispatch.gate_verdict_path(self.task_id))
        if v_path.exists():
            v_data = json.loads(v_path.read_text(encoding="utf-8"))
            self.assertEqual(v_data.get("verdict"), "pass")
            self.assertIn("GATE BYPASS", v_data.get("note"))

    def test_bypass_paused_task_unpauses(self):
        paused_tid = "task-paused-1"
        self.store.save_task({
            "task_id": paused_tid,
            "workflow_id": self.workflow_id,
            "node": "node_impl",
            "status": "paused",
            "agent": "opencode",
        })

        res = kernel.bypass_task_gate(paused_tid, reason="Resuming after review", store=self.store)
        self.assertEqual(res.get("status"), "working")
        self.assertEqual(res.get("stage_verdict"), "pass")

    def test_empty_reason_raises_error(self):
        with self.assertRaises(ValueError):
            kernel.bypass_task_gate(self.task_id, reason="", store=self.store)
        with self.assertRaises(ValueError):
            kernel.bypass_task_gate(self.task_id, reason="   \t", store=self.store)

    def test_unknown_task_raises_error(self):
        with self.assertRaises(ValueError):
            kernel.bypass_task_gate("task-nonexistent", reason="some reason", store=self.store)

    def test_bypass_gate_cli_command(self):
        env = dict(
            os.environ,
            HERDR_STATE_DB=str(self.db_path),
            TASKS_FILE=str(self.db_path.parent / "tasks.json"),
        )
        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-task",
                "bypass-gate",
                self.task_id,
                "--reason",
                "Audited bypass via CLI test",
                "--operator",
                "cli_tester",
                "--json",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}, stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("task_id"), self.task_id)
        self.assertEqual(data.get("stage_verdict"), "pass")
        self.assertEqual(data.get("operator"), "cli_tester")
        self.assertEqual(data.get("reason"), "Audited bypass via CLI test")


if __name__ == "__main__":
    unittest.main()
