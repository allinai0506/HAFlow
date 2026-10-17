import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from herdr import state_db


class TestSupersedeUnboundIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        state_db.init_db(self.db_path)

    def tearDown(self):
        Path(self.db_path).unlink(missing_ok=True)

    def test_supersede_allows_task_with_proven_newer_launch_intent(self):
        """When a task was rewritten with a newer unbound intent, supersede must succeed without conflict ValueError."""
        wf_id = "wf-test-1017"
        task_id = "t3-unbound"
        node = "implementation"

        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        conn = state_db.get_db_connection(self.db_path)
        # 1. Historical op epoch intent event
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": 107188,
                    "intent_id": "intent-d940",
                    "execution_id": wf_id,
                    "resources": {"run_id": "run-c120"},
                }),
                node,
            ),
        )

        # 2. Later unbound intent event
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 2000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": "intent-5c4e",
                    "execution_id": wf_id,
                    "resources": {"run_id": "run-5236"},
                }),
                node,
            ),
        )

        # 3. Existing task row has the later unbound intent identity
        state_db.save_task(
            {
                "task_id": task_id,
                "workflow_id": wf_id,
                "node": node,
                "stage": node,
                "status": "failed",
                "launch_intent_id": "intent-5c4e",
                "run_id": "run-5236",
                "execution_id": wf_id,
                "dispatch_operation_id": None,
            },
            self.db_path,
        )

        # 4. Now attempt to supersede this failed task
        # This should succeed without raising ValueError: dispatch registration identity conflicts with durable launch intent
        task = state_db.get_task(task_id, self.db_path)
        self.assertEqual(task["status"], "failed")

        task["status"] = "superseded"
        task["superseded_by"] = "t3-r2"
        task["replacement_pending"] = False

        state_db.save_task(task, self.db_path)

        updated = state_db.get_task(task_id, self.db_path)
        self.assertEqual(updated["status"], "superseded")
        self.assertEqual(updated["superseded_by"], "t3-r2")

    def test_supersede_rejects_rebinding_dispatch_operation_id(self):
        """Superseding a task cannot change or rebind its dispatch_operation_id."""
        wf_id = "wf-test-rebind"
        task_id = "t-bound-1"
        node = "impl"
        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "failed",
            "dispatch_operation_id": 100,
            "run_id": "run-100",
            "execution_id": wf_id,
            "launch_intent_id": "intent-100",
        }
        conn = state_db.get_db_connection(self.db_path)
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'failed', ?, 1000.0, 1000.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "superseded"
        task["dispatch_operation_id"] = 200  # Attempt to rebind

        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot rebind an existing task to another dispatch operation", str(ctx.exception))

    def test_supersede_rejects_changing_identity_keys(self):
        """Superseding a task cannot change workflow, node, run_id, execution_id, or launch_intent_id."""
        wf_id = "wf-test-identity"
        task_id = "t-bound-2"
        node = "impl"
        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "failed",
            "dispatch_operation_id": 100,
            "run_id": "run-100",
            "execution_id": wf_id,
            "launch_intent_id": "intent-100",
        }
        conn = state_db.get_db_connection(self.db_path)
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'failed', ?, 1000.0, 1000.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "superseded"
        task["run_id"] = "run-tampered"  # Tamper with run_id

        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
