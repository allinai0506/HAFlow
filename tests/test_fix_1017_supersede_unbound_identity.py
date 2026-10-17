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


if __name__ == "__main__":
    unittest.main()
