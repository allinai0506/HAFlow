"""Unit and regression tests for ADR-0005 / OEP-0002 D1 Privileged Transitions Convergence.

Verifies:
1. Pure validation contract: PRIVILEGED_TASK_TRANSITIONS and validate_task_transition.
2. StateStore / state_db transition_task with intent recording and CAS protection.
3. kernel.bypass_task_gate elimination of raw save_task and integration with operator-bypass.
4. console.task_api.api_task_force_review elimination of raw save_task and integration with console-force-review.
"""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from herdr import kernel, state_db
from herdr.state_store import SQLiteStateStore
from herdr.transitions import (
    InvalidTransitionError,
    PRIVILEGED_TASK_TRANSITIONS,
    validate_task_transition,
)
from console import task_api


class TestD1PrivilegedTransitionsPure(unittest.TestCase):
    def test_privileged_transitions_contract(self):
        # 1. Delivery Repair
        self.assertIn(("completed", "rework"), PRIVILEGED_TASK_TRANSITIONS["delivery-repair"])
        self.assertTrue(validate_task_transition("completed", "rework", intent="delivery-repair"))

        # 2. Operator Bypass
        self.assertIn(("blocked", "completed"), PRIVILEGED_TASK_TRANSITIONS["operator-bypass"])
        self.assertIn(("paused", "working"), PRIVILEGED_TASK_TRANSITIONS["operator-bypass"])
        self.assertTrue(validate_task_transition("blocked", "completed", intent="operator-bypass"))
        self.assertTrue(validate_task_transition("paused", "working", intent="operator-bypass"))

        # 3. Rollback
        self.assertTrue(validate_task_transition("working", "superseded", intent="rollback"))
        self.assertTrue(validate_task_transition("blocked", "superseded", intent="rollback"))

        # 4. Console Force Review
        self.assertTrue(validate_task_transition("working", "agent_done", intent="console-force-review"))
        self.assertTrue(validate_task_transition("blocked", "agent_done", intent="console-force-review"))
        self.assertTrue(validate_task_transition("paused", "agent_done", intent="console-force-review"))

    def test_illegal_intent_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            validate_task_transition("working", "agent_done", intent="unauthorized-intent")

    def test_illegal_transition_for_declared_intent_rejected(self):
        # operator-bypass does NOT allow arbitrary jumps like completed -> working
        with self.assertRaises(InvalidTransitionError):
            validate_task_transition("completed", "working", intent="operator-bypass")

        # console-force-review does NOT allow jumping to completed
        with self.assertRaises(InvalidTransitionError):
            validate_task_transition("working", "completed", intent="console-force-review")

        # console-force-review does NOT allow jumping from terminal states
        with self.assertRaises(InvalidTransitionError):
            validate_task_transition("superseded", "agent_done", intent="console-force-review")


class TestD1StateDBPrivilegedTransitions(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self.task_id = "task-priv-001"
        self.task = {
            "task_id": self.task_id,
            "workflow_id": "wf-priv",
            "run_id": "run-priv",
            "node": "exec",
            "stage": "exec",
            "status": "blocked",
            "agent": "codex",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.store.save_task(self.task)

    def tearDown(self):
        self.td.cleanup()

    def test_operator_bypass_transition_appends_event_and_history(self):
        res = self.store.compare_and_set_task_transition(
            task_id=self.task_id,
            to_status="completed",
            reason="gate bypassed by lead",
            source="operator-bypass",
            intent="operator-bypass",
            metadata={"stage_verdict": "pass"},
            expected_status="blocked",
            expected_version=1,
        )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("old_status"), "blocked")
        self.assertEqual(res.get("new_status"), "completed")

        # Verify DB row
        updated = self.store.get_task(self.task_id)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated.get("stage_verdict"), "pass")

        # Verify task_transition event in events table
        events = self.store.list_events(workflow_id="wf-priv", event_type="task_transition")
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.get("source"), "operator-bypass")
        payload = ev.get("payload") or {}
        self.assertEqual(payload.get("from_status"), "blocked")
        self.assertEqual(payload.get("to_status"), "completed")
        self.assertEqual(payload.get("intent"), "operator-bypass")

    def test_cas_mismatch_fails_closed(self):
        res = self.store.compare_and_set_task_transition(
            task_id=self.task_id,
            to_status="completed",
            reason="stale bypass",
            source="operator-bypass",
            intent="operator-bypass",
            expected_status="blocked",
            expected_version=99,  # Mismatch
        )
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("reason"), "cas_mismatch")

        # Ensure status was not mutated
        current = self.store.get_task(self.task_id)
        self.assertEqual(current["status"], "blocked")
        self.assertEqual(current["version"], 1)


class TestD1KernelBypassGateConvergence(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self.task_id = "task-bypass-gw-01"
        self.task = {
            "task_id": self.task_id,
            "workflow_id": "wf-bypass-gw",
            "run_id": "run-bypass-gw",
            "node": "verify",
            "stage": "verify",
            "status": "blocked",
            "stage_verdict": "blocked",
            "agent": "gemini",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.store.save_task(self.task)

    def tearDown(self):
        self.td.cleanup()

    def test_bypass_task_gate_uses_transition_gateway(self):
        res = kernel.bypass_task_gate(
            self.task_id,
            reason="Production hotfix emergency exception",
            operator="lead_alice",
            store=self.store,
        )
        self.assertEqual(res.get("status"), "completed")

        # Verify task in DB has updated version and gate_bypassed flag
        task = self.store.get_task(self.task_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["version"], 2)
        self.assertTrue(task.get("gate_bypassed"))
        self.assertEqual(task.get("stage_verdict"), "pass")

        # Verify workflow_events has task_transition event
        events = self.store.list_events(workflow_id="wf-bypass-gw", event_type="task_transition")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("source"), "operator-bypass")
        self.assertEqual(events[0].get("payload", {}).get("intent"), "operator-bypass")


class TestD1ConsoleForceReviewConvergence(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        # Mock herdr_kernel get_state_store for console
        self._orig_store = getattr(task_api.herdr_kernel, "get_state_store", None)
        task_api.herdr_kernel.get_state_store = lambda: self.store

        self.task_id = "task-console-gw-01"
        self.task = {
            "task_id": self.task_id,
            "workflow_id": "wf-console-gw",
            "run_id": "run-console-gw",
            "node": "code",
            "stage": "code",
            "status": "working",
            "agent": "claude",
            "version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.store.save_task(self.task)

    def tearDown(self):
        if self._orig_store is not None:
            task_api.herdr_kernel.get_state_store = self._orig_store
        self.td.cleanup()

    def test_api_task_force_review_uses_transition_gateway(self):
        req = {"task_id": self.task_id}
        resp = task_api.api_task_force_review(req)
        self.assertEqual(resp.get("status"), "agent_done")

        # Verify DB updated to agent_done via CAS with version increment
        task = self.store.get_task(self.task_id)
        self.assertEqual(task["status"], "agent_done")
        self.assertEqual(task["version"], 2)

        # Verify workflow_events recorded task_transition with console-force-review intent
        events = self.store.list_events(workflow_id="wf-console-gw", event_type="task_transition")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("source"), "console-force-review")
        self.assertEqual(events[0].get("payload", {}).get("intent"), "console-force-review")
