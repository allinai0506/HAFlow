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

    def test_bound_task_with_null_supersedes_rejects_injecting_supersedes(self):
        """When an existing bound task has durable supersedes=None, a later save cannot inject supersedes."""
        wf_id = "wf-test-null-supersedes"
        task_id = "t-bound-null-sup"
        node = "impl"
        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        conn = state_db.get_db_connection(self.db_path)
        # Durable launch intent event with supersedes=None
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": 200,
                    "intent_id": "intent-200",
                    "execution_id": wf_id,
                    "supersedes": None,
                    "resources": {"run_id": "run-200"},
                }),
                node,
            ),
        )

        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "working",
            "dispatch_operation_id": 200,
            "run_id": "run-200",
            "execution_id": wf_id,
            "launch_intent_id": "intent-200",
            "supersedes": None,
        }
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'working', ?, 1000.0, 1000.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        task = state_db.get_task(task_id, self.db_path)
        # Attempt to inject a fake supersedes
        task["supersedes"] = "t-fake-predecessor"

        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

    def test_supersede_unbound_task_rejects_identity_tampering(self):
        """Superseding an unbound task cannot alter its proven launch intent identity."""
        wf_id = "wf-test-unbound-tamper"
        task_id = "t-unbound-tamper"
        node = "impl"
        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        conn = state_db.get_db_connection(self.db_path)
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": "intent-unbound-orig",
                    "execution_id": wf_id,
                    "resources": {"run_id": "run-unbound-orig"},
                }),
                node,
            ),
        )

        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "failed",
            "dispatch_operation_id": None,
            "run_id": "run-unbound-orig",
            "execution_id": wf_id,
            "launch_intent_id": "intent-unbound-orig",
        }
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'failed', ?, 1000.0, 1000.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "superseded"
        task["run_id"] = "run-tampered-unbound"  # Tamper with run_id on unbound task!

        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

    def test_latest_authoritative_launch_intent_selected_across_allocating_and_resource_events(self):
        """save_task selects the latest source='launch' event so run_id is preserved and ordinary status progression succeeds."""
        wf_id = "wf-test-multi-intent"
        task_id = "t-multi-intent"
        node = "impl"
        intent_id = "intent-multi-123"
        run_id = "run-authoritative-456"

        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        conn = state_db.get_db_connection(self.db_path)
        # 1. Allocating event without resources
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": intent_id,
                    "execution_id": wf_id,
                    "resources": {},
                }),
                node,
            ),
        )

        # 2. Irrelevant event with non-launch source
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'audit_probe', ?, 1001.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": intent_id,
                    "execution_id": wf_id,
                    "resources": {"run_id": "run-bogus"},
                }),
                node,
            ),
        )

        # 3. Authoritative resource-bearing event with source='launch'
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1002.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": intent_id,
                    "execution_id": wf_id,
                    "resources": {"run_id": run_id},
                }),
                node,
            ),
        )

        # 4. Registered task with authoritative run_id
        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "working",
            "dispatch_operation_id": None,
            "run_id": run_id,
            "execution_id": wf_id,
            "launch_intent_id": intent_id,
        }
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'working', ?, 1002.0, 1002.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        # Ordinary status progression must succeed and keep run_id intact
        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "completed"
        task["stage_verdict"] = "passed"
        state_db.save_task(task, self.db_path)

        updated = state_db.get_task(task_id, self.db_path)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["run_id"], run_id)

    def test_unbound_task_ordinary_status_progression_rejects_identity_tampering(self):
        """Advancing an unbound task to completed or cleaned must validate identity against proven intent."""
        wf_id = "wf-test-unbound-ordinary"
        task_id = "t-unbound-ordinary"
        node = "impl"
        intent_id = "intent-unbound-1"
        run_id = "run-unbound-1"

        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        conn = state_db.get_db_connection(self.db_path)
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": intent_id,
                    "execution_id": wf_id,
                    "supersedes": None,
                    "resources": {"run_id": run_id},
                }),
                node,
            ),
        )

        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "working",
            "dispatch_operation_id": None,
            "run_id": run_id,
            "execution_id": wf_id,
            "launch_intent_id": intent_id,
            "supersedes": None,
        }
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'working', ?, 1000.0, 1000.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        # 1. Advancing to completed but tampering with run_id is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "completed"
        task["run_id"] = "run-tampered"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

        # 2. Advancing to completed but tampering with execution_id is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "completed"
        task["execution_id"] = "exec-tampered"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

        # 3. Advancing to completed but injecting a predecessor supersedes is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "completed"
        task["supersedes"] = "t-fake-predecessor"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

        # 4. Advancing to completed with untampered identity succeeds
        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "completed"
        state_db.save_task(task, self.db_path)
        persisted = state_db.get_task(task_id, self.db_path)
        self.assertEqual(persisted["status"], "completed")

        # 5. Advancing from completed to cleaned but tampering with node is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "cleaned"
        task["node"] = "tampered-node"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

        # 6. Advancing to cleaned with untampered identity succeeds
        task = state_db.get_task(task_id, self.db_path)
        task["status"] = "cleaned"
        state_db.save_task(task, self.db_path)
        persisted = state_db.get_task(task_id, self.db_path)
        self.assertEqual(persisted["status"], "cleaned")

    def test_unbound_task_with_replacement_intent_supersedes_validation(self):
        """Unbound task with replacement intent allows pre-link None/intent_supersedes, rejects other and post-link changes."""
        wf_id = "wf-test-unbound-repl"
        task_id = "t-unbound-repl"
        node = "impl"
        intent_id = "intent-unbound-repl"
        run_id = "run-unbound-repl"

        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        conn = state_db.get_db_connection(self.db_path)
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": intent_id,
                    "execution_id": wf_id,
                    "supersedes": "t-old-valid",
                    "resources": {"run_id": run_id},
                }),
                node,
            ),
        )

        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "working",
            "dispatch_operation_id": None,
            "run_id": run_id,
            "execution_id": wf_id,
            "launch_intent_id": intent_id,
            "supersedes": None,
        }
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'working', ?, 1000.0, 1000.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        # 1. Pre-link: setting a wrong supersedes is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["supersedes"] = "t-wrong-predecessor"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

        # 2. Pre-link: setting matching supersedes or leaving it None succeeds
        task = state_db.get_task(task_id, self.db_path)
        task["supersedes"] = "t-old-valid"
        state_db.save_task(task, self.db_path)
        persisted = state_db.get_task(task_id, self.db_path)
        self.assertEqual(persisted["supersedes"], "t-old-valid")

        # 3. Post-link: stripping supersedes back to None is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["supersedes"] = None
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

    def test_unbound_task_nullable_identity_fields_exact_match(self):
        """When unbound intent has specified identities, tampering with any of them is rejected."""
        wf_id = "wf-test-unbound-nullable"
        task_id = "t-unbound-nullable"
        node = "impl"
        intent_id = "intent-unbound-null"
        orig_run = "run-orig-123"
        orig_exec = "exec-orig-456"

        state_db.save_workflow({"workflow_id": wf_id, "status": "running"}, self.db_path)

        conn = state_db.get_db_connection(self.db_path)
        conn.execute(
            """INSERT INTO events (workflow_id, task_id, event_type, source, payload_json, timestamp, node_id)
               VALUES (?, ?, 'launch_intent', 'launch', ?, 1000.0, ?)""",
            (
                wf_id,
                task_id,
                json.dumps({
                    "task_id": task_id,
                    "dispatch_operation_id": None,
                    "intent_id": intent_id,
                    "execution_id": orig_exec,
                    "supersedes": None,
                    "resources": {"run_id": orig_run},
                }),
                node,
            ),
        )

        payload = {
            "task_id": task_id,
            "workflow_id": wf_id,
            "node": node,
            "stage": node,
            "status": "working",
            "dispatch_operation_id": None,
            "run_id": orig_run,
            "execution_id": orig_exec,
            "launch_intent_id": intent_id,
            "supersedes": None,
        }
        conn.execute(
            """INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, payload_json, created_at, updated_at, version)
               VALUES (?, ?, ?, ?, 'agent', 'working', ?, 1000.0, 1000.0, 1)""",
            (task_id, wf_id, node, node, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

        # 1. Tampering with run_id is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["run_id"] = "run-tampered"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

        # 2. Tampering with execution_id is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["execution_id"] = "exec-tampered"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))

        # 3. Injecting a predecessor when intent had supersedes=None is rejected
        task = state_db.get_task(task_id, self.db_path)
        task["supersedes"] = "t-fake-pred"
        with self.assertRaises(ValueError) as ctx:
            state_db.save_task(task, self.db_path)
        self.assertIn("cannot change dispatch task registration identity", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
