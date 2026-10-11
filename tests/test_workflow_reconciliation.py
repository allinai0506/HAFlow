import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from herdr import kernel, state_db
from herdr.reconciliation import reconcile_workflow_state
from herdr.state_store import SQLiteStateStore


class TestWorkflowReconciliation(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        # Create sample multi-node workflow
        self.workflow_id = "wf-recon-test-1"
        self.wf_config = {
            "name": "recon-pipeline",
            "nodes": [
                {
                    "id": "node_design",
                    "label": "Design Spec",
                    "depends_on": [],
                    "node_type": "agent",
                },
                {
                    "id": "node_impl",
                    "label": "Implementation",
                    "depends_on": ["node_design"],
                    "node_type": "agent",
                },
                {
                    "id": "node_gate",
                    "label": "Quality Gate",
                    "depends_on": ["node_impl"],
                    "node_type": "agent",
                    "gate": {"command": "echo pass"},
                },
            ],
        }
        self.wf_record = {
            "workflow_id": self.workflow_id,
            "title": "Reconciliation Test Workflow",
            "status": "running",
            "template_name": "general-v1",
            "config": self.wf_config,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.store.save_workflow(self.wf_record)

    def tearDown(self):
        self.td.cleanup()

    def test_reconcile_ready_nodes_initial_state(self):
        # In initial state, no tasks completed: node_design should be ready
        res = reconcile_workflow_state(self.workflow_id, store=self.store)
        self.assertTrue(res.get("reconciled"))
        self.assertEqual(res.get("workflow_id"), self.workflow_id)
        self.assertEqual(res.get("status"), "running")

        nodes = res.get("nodes", {})
        self.assertEqual(nodes.get("total"), 3)
        self.assertEqual(nodes.get("completed"), [])
        self.assertEqual(nodes.get("ready"), ["node_design"])
        self.assertEqual(nodes.get("blocked"), [])

    def test_reconcile_advances_ready_nodes_as_tasks_complete(self):
        # Complete task in node_design
        t_design = {
            "task_id": "task-design-1",
            "workflow_id": self.workflow_id,
            "node": "node_design",
            "status": "completed",
            "stage_verdict": "pass",
            "agent": "opencode",
        }
        self.store.save_task(t_design)

        res = reconcile_workflow_state(self.workflow_id, store=self.store)
        nodes = res.get("nodes", {})
        self.assertEqual(nodes.get("completed"), ["node_design"])
        self.assertEqual(nodes.get("ready"), ["node_impl"])

        # Now complete task in node_impl
        t_impl = {
            "task_id": "task-impl-1",
            "workflow_id": self.workflow_id,
            "node": "node_impl",
            "status": "completed",
            "stage_verdict": "pass",
            "agent": "opencode",
        }
        self.store.save_task(t_impl)

        res2 = reconcile_workflow_state(self.workflow_id, store=self.store)
        nodes2 = res2.get("nodes", {})
        self.assertEqual(nodes2.get("completed"), ["node_design", "node_impl"])
        self.assertEqual(nodes2.get("ready"), ["node_gate"])

    def test_reconcile_detects_blocked_gate_node(self):
        # Design & impl complete, gate is blocked
        self.store.save_task({
            "task_id": "t-des",
            "workflow_id": self.workflow_id,
            "node": "node_design",
            "status": "completed",
            "stage_verdict": "pass",
        })
        self.store.save_task({
            "task_id": "t-imp",
            "workflow_id": self.workflow_id,
            "node": "node_impl",
            "status": "completed",
            "stage_verdict": "pass",
        })
        self.store.save_task({
            "task_id": "t-gate",
            "workflow_id": self.workflow_id,
            "node": "node_gate",
            "status": "blocked",
            "stage_verdict": "blocked",
            "blocker": "Tests failed: 3 delta regressions",
        })

        res = reconcile_workflow_state(self.workflow_id, store=self.store)
        nodes = res.get("nodes", {})
        self.assertIn("node_gate", nodes.get("blocked", []))
        self.assertNotIn("node_gate", nodes.get("completed", []))

    def test_reconcile_self_heals_workflow_status_to_completed_when_all_nodes_done(self):
        for nid in ("node_design", "node_impl", "node_gate"):
            self.store.save_task({
                "task_id": f"t-{nid}",
                "workflow_id": self.workflow_id,
                "node": nid,
                "status": "completed",
                "stage_verdict": "pass",
            })

        res = reconcile_workflow_state(self.workflow_id, store=self.store)
        self.assertEqual(res.get("status"), "completed")
        self.assertEqual(len(res.get("nodes", {}).get("completed", [])), 3)

        # Verify DB record was updated
        updated_wf = self.store.get_workflow(self.workflow_id)
        self.assertEqual(updated_wf.get("status"), "completed")

    def test_reconcile_unknown_workflow_raises_error(self):
        with self.assertRaises(ValueError):
            reconcile_workflow_state("wf-nonexistent", store=self.store)

    def test_reconcile_cli_command(self):
        env = dict(os.environ, HERDR_STATE_DB=str(self.db_path))
        proc = subprocess.run(
            ["python3", "bin/herdr-workflow", "reconcile", self.workflow_id, "--json"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}, stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertTrue(data.get("reconciled"))
        self.assertEqual(data.get("workflow_id"), self.workflow_id)


if __name__ == "__main__":
    unittest.main()
