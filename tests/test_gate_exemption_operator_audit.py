"""Regression tests for SC-010: Gate Exemption Operator Audit Contract.

Verifies operator identity validation and minimum format specifications across:
1. Pure logic validation:
   - validate_operator: rejects empty, whitespace, control chars, path traversal, injection,
     compound types, and placeholder tokens (none, null, undefined, unknown, anonymous).
   - Normalizes valid operator strings and enforces length boundaries (1-128 chars).
2. herdr.kernel & herdr.state_db:
   - bypass_task_gate: blocks invalid operators, records valid operator in receipt, notes, and audit events.
   - force_pass_gate: blocks invalid operators, records valid operator in task notes and workflow overrides.
3. Web Console endpoints:
   - _validate_force_pass_params: rejects invalid operators, defaults safely to 'human' when omitted.
   - api_task_force_review: rejects invalid operators, records valid operator in transition metadata.
4. CLI entrypoints:
   - bin/herdr-task bypass-gate: exits non-zero on empty/placeholder operator, succeeds on valid operator.
   - bin/herdr-factory force-pass: exits non-zero on empty/placeholder operator, succeeds on valid operator.
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from herdr import direct_dispatch, kernel, state_db
from herdr.state_store import SQLiteStateStore
from herdr.transitions import InvalidOperatorError, validate_operator


class TestOperatorValidationPure(unittest.TestCase):
    """Pure-logic verification of validate_operator function."""

    def test_valid_operators(self):
        valid_cases = [
            ("human", "human"),
            ("lead_alice", "lead_alice"),
            ("  admin_bob  ", "admin_bob"),
            ("audit@co.work", "audit@co.work"),
            ("user-42", "user-42"),
            ("operator.1", "operator.1"),
            ("张三", "张三"),
            ("release-manager_01", "release-manager_01"),
        ]
        for raw, expected in valid_cases:
            with self.subTest(raw=raw):
                self.assertEqual(validate_operator(raw), expected)

    def test_empty_or_whitespace_operator_rejected(self):
        invalid_cases = [
            None,
            "",
            "   ",
            "\t",
            "\n",
            " \r\n\t ",
        ]
        for val in invalid_cases:
            with self.subTest(val=val):
                with self.assertRaises(InvalidOperatorError):
                    validate_operator(val)

    def test_operator_length_limits(self):
        max_valid = "a" * 128
        self.assertEqual(validate_operator(max_valid), max_valid)

        too_long = "a" * 129
        with self.assertRaises(InvalidOperatorError):
            validate_operator(too_long)

    def test_control_characters_rejected(self):
        control_cases = [
            "lead\x00user",
            "admin\nroot",
            "user\rtester",
            "lead\toperator",
            "auditor\x1f",
        ]
        for val in control_cases:
            with self.subTest(val=val):
                with self.assertRaises(InvalidOperatorError):
                    validate_operator(val)

    def test_path_traversal_and_separators_rejected(self):
        separator_cases = [
            "../admin",
            "lead/alice",
            "domain\\user",
            "..",
            "user..name",
        ]
        for val in separator_cases:
            with self.subTest(val=val):
                with self.assertRaises(InvalidOperatorError):
                    validate_operator(val)

    def test_script_injection_characters_rejected(self):
        injection_cases = [
            "<script>",
            "user>admin",
            "operator<test>",
        ]
        for val in injection_cases:
            with self.subTest(val=val):
                with self.assertRaises(InvalidOperatorError):
                    validate_operator(val)

    def test_placeholder_and_anonymous_tokens_rejected(self):
        placeholder_cases = [
            "none",
            "None",
            "NONE",
            "null",
            "Null",
            "NULL",
            "nil",
            "Nil",
            "undefined",
            "UNDEFINED",
            "unknown",
            "Unknown",
            "UNKNOWN",
            "anonymous",
            "Anonymous",
            "ANONYMOUS",
            "  none  ",
            "  anonymous  ",
        ]
        for val in placeholder_cases:
            with self.subTest(val=val):
                with self.assertRaises(InvalidOperatorError):
                    validate_operator(val)

    def test_invalid_types_rejected(self):
        type_cases = [
            True,
            False,
            ["admin"],
            {"operator": "human"},
            ("lead",),
            {"alice"},
        ]
        for val in type_cases:
            with self.subTest(val=val):
                with self.assertRaises(InvalidOperatorError):
                    validate_operator(val)


class TestKernelAndStateDBOperatorAudit(unittest.TestCase):
    """Integration verification of operator validation in kernel and state_db."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self.workflow_id = "wf-sc010-audit"
        self.run_id = "run-sc010"
        self.now = time.time()

        self.store.save_workflow({
            "workflow_id": self.workflow_id,
            "title": "SC-010 Test Workflow",
            "status": "running",
            "template_name": "software-development-v1",
            "config": {
                "nodes": [
                    {"id": "gate_test", "label": "Test Gate"},
                ]
            },
            "created_at": self.now,
            "updated_at": self.now,
        })

    def tearDown(self):
        self.td.cleanup()

    def _seed_task(self, task_id: str, node: str = "gate_test", status: str = "blocked", version: int = 1):
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
        return task

    # -------------------------------------------------------------------------
    # bypass_task_gate tests
    # -------------------------------------------------------------------------
    def test_bypass_task_gate_rejects_invalid_operator(self):
        tid = "task-bypass-op-fail"
        self._seed_task(tid)

        invalid_ops = ["", "   ", "anonymous", "none", "unknown", "../root", "<admin>"]
        for op in invalid_ops:
            with self.subTest(op=op):
                with self.assertRaises(InvalidOperatorError):
                    kernel.bypass_task_gate(
                        tid,
                        reason="Legitimate emergency bypass",
                        operator=op,
                        store=self.store,
                    )
                # Verify task remains blocked and unchanged
                task = self.store.get_task(tid)
                self.assertEqual(task["status"], "blocked")
                self.assertEqual(task["version"], 1)

    def test_bypass_task_gate_accepts_valid_operator(self):
        tid = "task-bypass-op-ok"
        self._seed_task(tid)

        res = kernel.bypass_task_gate(
            tid,
            reason="Audited bypass by lead engineer",
            operator="lead_alice",
            note="Ticket SEC-404",
            store=self.store,
        )
        self.assertEqual(res.get("operator"), "lead_alice")
        self.assertEqual(res.get("status"), "completed")
        self.assertEqual(res.get("receipt", {}).get("operator"), "lead_alice")

        updated = self.store.get_task(tid)
        self.assertEqual(updated.get("status"), "completed")
        self.assertIn("[GATE BYPASS by lead_alice]", updated.get("stage_verdict_note", ""))

    # -------------------------------------------------------------------------
    # force_pass_gate tests (kernel & state_db)
    # -------------------------------------------------------------------------
    def test_force_pass_gate_rejects_invalid_operator(self):
        tid = "task-force-op-fail"
        self._seed_task(tid)

        invalid_ops = ["", "   ", "null", "undefined", "anonymous", "root/admin"]
        for op in invalid_ops:
            with self.subTest(op=op):
                with self.assertRaises(InvalidOperatorError):
                    kernel.force_pass_gate(
                        self.workflow_id,
                        "gate_test",
                        note="Manual force pass",
                        operator=op,
                        store=self.store,
                    )
                # Direct state_db test
                with self.assertRaises(InvalidOperatorError):
                    state_db.force_pass_gate(
                        self.workflow_id,
                        "gate_test",
                        note="Manual force pass",
                        operator=op,
                        db_path=self.db_path,
                    )

    def test_force_pass_gate_accepts_valid_operator(self):
        tid = "task-force-op-ok"
        self._seed_task(tid)

        res = kernel.force_pass_gate(
            self.workflow_id,
            "gate_test",
            note="Manual force pass by security auditor",
            operator="security_auditor",
            store=self.store,
        )
        self.assertTrue(res.get("ok"))
        self.assertIn(tid, res.get("updated_tasks", []))

        updated = self.store.get_task(tid)
        self.assertIn("[FORCE PASS by security_auditor]", updated.get("stage_verdict_note", ""))

        wf = self.store.get_workflow(self.workflow_id)
        overrides = wf.get("gate_overrides", {})
        self.assertIn("gate_test", overrides)
        self.assertEqual(overrides["gate_test"].get("operator"), "security_auditor")


class TestConsoleOperatorPolicyAudit(unittest.TestCase):
    """Integration verification of operator validation in Web Console."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.old_env_db = os.environ.get("HERDR_STATE_DB")
        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        self.store = SQLiteStateStore(self.db_path)

        self.workflow_id = "wf-console-op"
        self.run_id = "run-console-op"
        self.now = time.time()

        self.wf_dict = {
            "workflow_id": self.workflow_id,
            "title": "Console Operator Test",
            "status": "running",
            "config": {
                "nodes": [
                    {"id": "gate_review", "label": "Review Gate"},
                ]
            },
            "created_at": self.now,
            "updated_at": self.now,
        }
        self.store.save_workflow(self.wf_dict)

    def tearDown(self):
        if self.old_env_db is not None:
            os.environ["HERDR_STATE_DB"] = self.old_env_db
        else:
            os.environ.pop("HERDR_STATE_DB", None)
        self.td.cleanup()

    def test_validate_force_pass_params_operator_validation(self):
        from console.workflow_policy import _validate_force_pass_params

        base_payload = {
            "confirmed": True,
            "reason": "Authorized emergency override",
            "gate_node_id": "gate_review",
            "expected_task_versions": {},
        }

        # 1. Invalid operators raise RuntimeError
        invalid_ops = ["", "   ", "anonymous", "none", "null", "user/admin", "<script>"]
        for op in invalid_ops:
            with self.subTest(op=op):
                bad_payload = dict(base_payload, operator=op)
                with self.assertRaises(RuntimeError) as cm:
                    _validate_force_pass_params(bad_payload, self.workflow_id, wf=self.wf_dict)
                self.assertIn("无效的操作人标识", str(cm.exception))

        # 2. Omitted operator safely defaults to 'human'
        omitted_payload = dict(base_payload)
        gate, reason, op = _validate_force_pass_params(omitted_payload, self.workflow_id, wf=self.wf_dict)
        self.assertEqual(op, "human")

        # 3. Valid operator passes through
        valid_payload = dict(base_payload, operator="lead_bob")
        gate, reason, op = _validate_force_pass_params(valid_payload, self.workflow_id, wf=self.wf_dict)
        self.assertEqual(op, "lead_bob")

    def test_api_task_force_review_operator_validation(self):
        from console.task_api import api_task_force_review

        tid = "task-console-review"
        self.store.save_task({
            "task_id": tid,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "node": "gate_review",
            "stage": "gate_review",
            "status": "working",
            "agent": "gemini",
            "version": 1,
            "created_at": self.now,
            "updated_at": self.now,
        })

        import herdr.kernel as herdr_kernel
        orig_get_store = getattr(herdr_kernel, "get_state_store", None)
        herdr_kernel.get_state_store = lambda: self.store

        try:
            # 1. Invalid operator raises RuntimeError
            invalid_review_ops = ["", "   ", "anonymous", "none", "null", "invalid/user"]
            for op in invalid_review_ops:
                with self.subTest(op=op):
                    with self.assertRaises(RuntimeError) as cm:
                        api_task_force_review({
                            "task_id": tid,
                            "operator": op,
                            "reason": "Force review requested",
                            "expected_version": 1,
                        })
                    self.assertIn("无效的操作人标识", str(cm.exception))
                    # Ensure status remains 'working'
                    self.assertEqual(self.store.get_task(tid)["status"], "working")

            # 2. Valid operator succeeds and is recorded
            res = api_task_force_review({
                "task_id": tid,
                "operator": "lead_carol",
                "reason": "Legitimate force review by lead",
                "expected_version": 1,
            })
            self.assertEqual(res.get("status"), "agent_done")
            updated = self.store.get_task(tid)
            self.assertEqual(updated["status"], "agent_done")
        finally:
            if orig_get_store:
                herdr_kernel.get_state_store = orig_get_store


class TestCLIOperatorAudit(unittest.TestCase):
    """End-to-end CLI verification of operator audit requirements."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = Path(self.td.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)

        self.workflow_id = "wf-cli-op"
        self.run_id = "run-cli-op"
        self.now = time.time()

        self.store.save_workflow({
            "workflow_id": self.workflow_id,
            "title": "CLI Operator Audit Test",
            "status": "running",
            "template_name": "software-development-v1",
            "config": {
                "nodes": [
                    {"id": "gate_cleanroom", "label": "Cleanroom Gate"},
                ]
            },
            "created_at": self.now,
            "updated_at": self.now,
        })

        self.tid = "task-cli-op"
        self.store.save_task({
            "task_id": self.tid,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "node": "gate_cleanroom",
            "stage": "gate_cleanroom",
            "status": "blocked",
            "stage_verdict": "blocked",
            "stage_verdict_note": "Lint errors",
            "agent": "gemini",
            "version": 1,
            "created_at": self.now,
            "updated_at": self.now,
        })

    def tearDown(self):
        self.td.cleanup()

    def _env(self):
        return dict(
            os.environ,
            HERDR_STATE_DB=str(self.db_path),
            TASKS_FILE=str(self.db_path.parent / "tasks.json"),
            WORKFLOWS_FILE=str(self.db_path.parent / "workflows.json"),
        )

    def test_herdr_task_bypass_gate_cli_rejects_invalid_operator(self):
        invalid_ops = ["", "anonymous", "none", "unknown"]
        for op in invalid_ops:
            with self.subTest(op=op):
                proc = subprocess.run(
                    [
                        "python3",
                        "bin/herdr-task",
                        "bypass-gate",
                        self.tid,
                        "--reason",
                        "Emergency fix bypass",
                        "--operator",
                        op,
                    ],
                    env=self._env(),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(proc.returncode, 0, f"Expected non-zero exit code for operator '{op}'")
                task = self.store.get_task(self.tid)
                self.assertEqual(task["status"], "blocked")

    def test_herdr_task_bypass_gate_cli_accepts_valid_operator(self):
        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-task",
                "bypass-gate",
                self.tid,
                "--reason",
                "Emergency fix bypass audited",
                "--operator",
                "lead_alice",
                "--json",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}, stderr: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("operator"), "lead_alice")
        self.assertEqual(data.get("status"), "completed")

        task = self.store.get_task(self.tid)
        self.assertEqual(task["status"], "completed")
        self.assertIn("[GATE BYPASS by lead_alice]", task["stage_verdict_note"])

    def test_herdr_factory_force_pass_cli_rejects_invalid_operator(self):
        invalid_ops = ["", "anonymous", "none", "unknown"]
        for op in invalid_ops:
            with self.subTest(op=op):
                proc = subprocess.run(
                    [
                        "python3",
                        "bin/herdr-factory",
                        "force-pass",
                        self.workflow_id,
                        "gate_cleanroom",
                        "--note",
                        "Manual force pass",
                        "--operator",
                        op,
                    ],
                    env=self._env(),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(proc.returncode, 0, f"Expected non-zero exit code for operator '{op}'")

    def test_herdr_factory_force_pass_cli_accepts_valid_operator(self):
        proc = subprocess.run(
            [
                "python3",
                "bin/herdr-factory",
                "force-pass",
                self.workflow_id,
                "gate_cleanroom",
                "--note",
                "Manual force pass audited",
                "--operator",
                "security_auditor",
            ],
            env=self._env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}, stderr: {proc.stderr}")
        task = self.store.get_task(self.tid)
        self.assertIn("[FORCE PASS by security_auditor]", task["stage_verdict_note"])


if __name__ == "__main__":
    unittest.main()
