"""Regression tests for SC-015: Gate Exemption Version Guards.

Verifies optimistic concurrency (CAS) version guards on both CLI entrypoints:
1. herdr-task bypass-gate:
   - Matching --expected-version successfully bypasses gate and increments version.
   - Mismatched --expected-version fails closed, aborts with non-zero exit code, leaving state intact.
2. herdr-factory force-pass:
   - Single task with matching --expected-version and --task-id passes gate.
   - Single task with mismatched --expected-version and --task-id fails closed.
   - Node-level pass with single --expected-version without --task-id is strictly rejected.
   - Multi-task node with matching --expected-task-versions JSON succeeds.
   - Multi-task node with mismatched --expected-task-versions JSON fails closed.
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from herdr import kernel, state_db
from herdr.state_store import SQLiteStateStore


class TestGateExemptionVersionGuard(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self.workflow_id = "wf-sc015-guard"
        self.run_id = "run-sc015"
        self.now = time.time()

        self.store.save_workflow({
            "workflow_id": self.workflow_id,
            "title": "SC-015 Test Workflow",
            "status": "running",
            "template_name": "software-development-v1",
            "config": {
                "nodes": [
                    {"id": "gate_test", "label": "Test Gate"},
                    {"id": "gate_multi", "label": "Multi Task Gate"},
                ]
            },
            "created_at": self.now,
            "updated_at": self.now,
        })

    def tearDown(self):
        self.td.cleanup()

    def _seed_task(self, task_id: str, node: str, status: str = "blocked", version: int = 1):
        task = {
            "task_id": task_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "node": node,
            "stage": node,
            "status": status,
            "stage_verdict": "blocked" if status == "blocked" else "",
            "stage_verdict_note": "Gate blocked",
            "agent": "gemini",
            "version": version,
            "created_at": self.now,
            "updated_at": self.now,
        }
        self.store.save_task(task)
        if version != 1:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            conn.execute("UPDATE tasks SET version = ? WHERE task_id = ?", (version, task_id))
            conn.commit()
            conn.close()
        return task

    def _env(self):
        return dict(
            os.environ,
            HERDR_STATE_DB=str(self.db_path),
            TASKS_FILE=str(self.db_path.parent / "tasks.json"),
            WORKFLOWS_FILE=str(self.db_path.parent / "workflows.json"),
        )

    # -------------------------------------------------------------------------
    # 1. herdr-task bypass-gate CLI tests
    # -------------------------------------------------------------------------
    def test_task_bypass_gate_cli_matching_expected_version_succeeds(self):
        tid = "task-bypass-match"
        self._seed_task(tid, "gate_test", status="blocked", version=3)

        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-task",
                "bypass-gate",
                tid,
                "--reason",
                "Approved hotfix bypass",
                "--operator",
                "lead_alice",
                "--expected-version",
                "3",
                "--json",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}, stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("status"), "completed")
        self.assertEqual(data.get("stage_verdict"), "pass")

        # Verify DB version incremented
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["version"], 4)

    def test_task_bypass_gate_cli_mismatched_expected_version_fails_closed(self):
        tid = "task-bypass-stale"
        self._seed_task(tid, "gate_test", status="blocked", version=5)

        # Caller provides stale version 4
        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-task",
                "bypass-gate",
                tid,
                "--reason",
                "Stale bypass attempt",
                "--operator",
                "lead_alice",
                "--expected-version",
                "4",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("任务版本已在写入边界发生变化", proc.stderr)

        # Verify DB row is untouched
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["version"], 5)

    def test_kernel_bypass_task_gate_matching_and_mismatch(self):
        tid = "task-kernel-bypass"
        self._seed_task(tid, "gate_test", status="blocked", version=2)

        # Mismatch
        with self.assertRaises(ValueError) as ctx:
            kernel.bypass_task_gate(
                tid,
                reason="Stale kernel call",
                store=self.store,
                expected_version=1,
            )
        self.assertIn("任务版本已在写入边界发生变化", str(ctx.exception))

        # Status and version untouched
        t = self.store.get_task(tid)
        self.assertEqual(t["status"], "blocked")
        self.assertEqual(t["version"], 2)

        # Matching succeeds
        res = kernel.bypass_task_gate(
            tid,
            reason="Fresh kernel call",
            store=self.store,
            expected_version=2,
        )
        self.assertEqual(res["status"], "completed")
        t_after = self.store.get_task(tid)
        self.assertEqual(t_after["status"], "completed")
        self.assertEqual(t_after["version"], 3)

    # -------------------------------------------------------------------------
    # 2. herdr-factory force-pass CLI tests
    # -------------------------------------------------------------------------
    def test_factory_force_pass_matching_expected_version_succeeds(self):
        tid = "task-fp-match"
        self._seed_task(tid, "gate_test", status="blocked", version=2)

        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-factory",
                "force-pass",
                self.workflow_id,
                "gate_test",
                "--task-id",
                tid,
                "--expected-version",
                "2",
                "--note",
                "Approved single task force pass",
                "--operator",
                "lead_charlie",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}, stderr: {proc.stderr}")
        self.assertIn("marked as passed", proc.stdout)

        # Verify DB
        task = self.store.get_task(tid)
        self.assertEqual(task.get("stage_verdict"), "pass")
        self.assertEqual(task.get("version"), 3)

    def test_factory_force_pass_mismatched_expected_version_fails_closed(self):
        tid = "task-fp-mismatch"
        self._seed_task(tid, "gate_test", status="blocked", version=7)

        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-factory",
                "force-pass",
                self.workflow_id,
                "gate_test",
                "--task-id",
                tid,
                "--expected-version",
                "6",  # Stale
                "--note",
                "Stale single task force pass",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("任务版本已在写入边界发生变化", proc.stdout + proc.stderr)

        # Verify DB untouched
        task = self.store.get_task(tid)
        self.assertEqual(task.get("stage_verdict"), "blocked")
        self.assertEqual(task.get("version"), 7)

    def test_factory_force_pass_node_level_single_expected_version_rejected(self):
        # Node-level pass without --task-id must refuse single --expected-version
        tid = "task-fp-node-reject"
        self._seed_task(tid, "gate_test", status="blocked", version=1)

        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-factory",
                "force-pass",
                self.workflow_id,
                "gate_test",
                "--expected-version",
                "1",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("节点级放行禁止使用单一 expected_version", proc.stdout + proc.stderr)

    def test_factory_force_pass_expected_task_versions_mapping(self):
        tid1 = "task-multi-1"
        tid2 = "task-multi-2"
        self._seed_task(tid1, "gate_multi", status="blocked", version=2)
        self._seed_task(tid2, "gate_multi", status="blocked", version=4)

        # 1. Mismatch version in JSON
        stale_map = json.dumps({tid1: 2, tid2: 99})
        proc_fail = subprocess.run(
            [
                "python3",
                "bin/herdr-factory",
                "force-pass",
                self.workflow_id,
                "gate_multi",
                "--expected-task-versions",
                stale_map,
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc_fail.returncode, 0)
        self.assertIn("版本已在写入边界发生变化", proc_fail.stdout + proc_fail.stderr)

        # 2. Matching version in JSON
        matching_map = json.dumps({tid1: 2, tid2: 4})
        proc_ok = subprocess.run(
            [
                "python3",
                "bin/herdr-factory",
                "force-pass",
                self.workflow_id,
                "gate_multi",
                "--expected-task-versions",
                matching_map,
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_ok.returncode, 0, f"stdout: {proc_ok.stdout}, stderr: {proc_ok.stderr}")
        self.assertIn("marked as passed", proc_ok.stdout)

        # Verify DB tasks both passed and incremented version
        t1 = self.store.get_task(tid1)
        t2 = self.store.get_task(tid2)
        self.assertEqual(t1.get("stage_verdict"), "pass")
        self.assertEqual(t1.get("version"), 3)
        self.assertEqual(t2.get("stage_verdict"), "pass")
        self.assertEqual(t2.get("version"), 5)


if __name__ == "__main__":
    unittest.main()
