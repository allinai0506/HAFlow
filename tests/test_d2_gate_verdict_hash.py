"""Tests for D2 gate object decomposition, single scheduling authority, and evidence snapshot hash convergence (ADR-0006 & OEP-0002)."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from herdr import kernel, state_db
from herdr.state_store import SQLiteStateStore
from herdr.direct_dispatch import gate_verdict_path
from herdr.task_cli_verification import _authenticate_gate_verdict_evidence
from services.controller import verdict_evidence


class TestD2GateVerdictHashConvergence(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="haflow-d2-test-"))
        self.db_path = self.tmp_dir / "state.db"
        self.verdicts_dir = self.tmp_dir / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True, exist_ok=True)
        self.clone_dir = self.tmp_dir / "work-clone"
        self.clone_dir.mkdir(parents=True, exist_ok=True)

        self.orig_env = dict(os.environ)
        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        os.environ["HERDR_GATE_VERDICT_DIR"] = str(self.verdicts_dir)
        os.environ["TASKS_FILE"] = str(self.tmp_dir / "tasks.json")
        os.environ["WORKFLOWS_FILE"] = str(self.tmp_dir / "workflows.json")
        os.environ["STAGE_STATE_FILE"] = str(self.tmp_dir / "stage-state.json")

        (self.tmp_dir / "tasks.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
        (self.tmp_dir / "workflows.json").write_text(json.dumps({"workflows": []}), encoding="utf-8")
        (self.tmp_dir / "stage-state.json").write_text("{}", encoding="utf-8")

        state_db.init_db(self.db_path)
        self.store = SQLiteStateStore(self.db_path)
        self.store.save_workflow({"workflow_id": "wf-d2", "title": "D2 Test Workflow", "status": "running"})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_task(self, task_id, node="test", status="agent_done"):
        task = {
            "task_id": task_id,
            "workflow_id": "wf-d2",
            "node": node,
            "stage": node,
            "status": status,
            "agent": "tester",
            "integration_mode": "none",
            "clone_path": "",
            "created_at": 1000.0,
            "updated_at": 1000.0,
            "status_history": [],
        }
        self.store.save_task(task)
        return self.store.get_task(task_id)

    def test_verdict_persists_evidence_snapshot_hash(self):
        tid = "task-d2-01"
        self._create_task(tid, node="test", status="agent_done")

        # Worker writes verdict file
        v_path = Path(gate_verdict_path(tid))
        v_data = {"verdict": "pass", "note": "all 42 test suites passed cleanly"}
        v_bytes = json.dumps(v_data, indent=2, ensure_ascii=False).encode("utf-8")
        v_path.write_bytes(v_bytes)
        expected_hash = hashlib.sha256(v_bytes).hexdigest()

        # Execute herdr-task set completed --verdict pass
        cmd = [
            sys.executable, "bin/herdr-task", "set", tid, "completed",
            "--verdict", "pass", "--note", "all 42 test suites passed cleanly"
        ]
        res = subprocess.run(cmd, env=os.environ, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"CLI error: {res.stderr}\n{res.stdout}")

        # Check DB task row
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["stage_verdict"], "pass")
        self.assertEqual(task.get("evidence_snapshot_hash"), expected_hash)

        # Check event payload
        events = self.store.list_events(task_id=tid)
        transition_events = [e for e in events if e.get("event_type") == "task_transition"]
        self.assertTrue(len(transition_events) > 0)
        self.assertEqual(transition_events[-1]["payload"].get("evidence_snapshot_hash"), expected_hash)

        # Re-authenticate against disk file: matches expected hash
        ok, auth = _authenticate_gate_verdict_evidence(task, expected_verdict="pass")
        self.assertTrue(ok)
        self.assertEqual(auth.snapshot_hash, expected_hash)

    def test_tampered_gate_verdict_file_refused_fail_closed(self):
        tid = "task-d2-tamper"
        self._create_task(tid, node="test", status="agent_done")

        # Initial legitimate verdict
        v_path = Path(gate_verdict_path(tid))
        v_data = {"verdict": "pass", "note": "initial pass"}
        v_bytes = json.dumps(v_data, indent=2, ensure_ascii=False).encode("utf-8")
        v_path.write_bytes(v_bytes)
        legit_hash = hashlib.sha256(v_bytes).hexdigest()

        cmd = [sys.executable, "bin/herdr-task", "set", tid, "completed", "--verdict", "pass"]
        res = subprocess.run(cmd, env=os.environ, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

        task = self.store.get_task(tid)
        self.assertEqual(task.get("evidence_snapshot_hash"), legit_hash)

        # Attacker tampers with the verdict file on disk
        tampered_data = {"verdict": "blocked", "note": "malicious tampering"}
        v_path.write_text(json.dumps(tampered_data), encoding="utf-8")

        # 1. Direct authentication fails closed
        ok, err = _authenticate_gate_verdict_evidence(task, expected_verdict="pass")
        self.assertFalse(ok)
        self.assertIn("hash mismatch", err)
        self.assertIn("tampered or corrupted", err)

        # 2. Controller fresh check returns False
        class MockPorts:
            hashlib = hashlib
            json = json
            os = os
        is_fresh = verdict_evidence._gate_verdict_file_is_fresh(MockPorts(), str(v_path), task)
        self.assertFalse(is_fresh)

        # 3. CLI adopt-verdict or re-set is rejected
        cmd2 = [sys.executable, "bin/herdr-task", "adopt-verdict", tid]
        res2 = subprocess.run(cmd2, env=os.environ, capture_output=True, text=True)
        self.assertNotEqual(res2.returncode, 0)
        self.assertIn("No valid gate verdict found", res2.stderr)

    def test_corrupted_gate_verdict_file_refused_fail_closed(self):
        tid = "task-d2-corrupt"
        self._create_task(tid, node="test", status="working")

        # Corrupted file on disk (not valid JSON)
        v_path = Path(gate_verdict_path(tid))
        v_path.write_bytes(b"\x00\xff\xfe corrupted binary junk {verdict: ")

        # Attempt to set verdict when candidate file is corrupted
        cmd = [sys.executable, "bin/herdr-task", "set", tid, "completed", "--verdict", "pass"]
        res = subprocess.run(cmd, env=os.environ, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("[VERDICT REFUSED]", res.stderr)

    def test_bypass_task_gate_computes_and_binds_evidence_hash(self):
        tid = "task-d2-bypass"
        self._create_task(tid, node="test", status="blocked")

        res = kernel.bypass_task_gate(
            tid,
            reason="Approved hotfix by architect",
            operator="lead_dev",
            note="Jira-9999",
            store=self.store,
        )
        task = self.store.get_task(tid)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["stage_verdict"], "pass")
        self.assertTrue(task["gate_bypassed"])

        snapshot_hash = task.get("evidence_snapshot_hash")
        self.assertTrue(bool(snapshot_hash))

        # Check that file exists on disk and its SHA256 matches DB exactly
        v_path = Path(gate_verdict_path(tid))
        self.assertTrue(v_path.exists())
        file_bytes = v_path.read_bytes()
        self.assertEqual(hashlib.sha256(file_bytes).hexdigest(), snapshot_hash)

        # Authenticate evidence
        ok, auth = _authenticate_gate_verdict_evidence(task, expected_verdict="pass")
        self.assertTrue(ok)
        self.assertEqual(auth.snapshot_hash, snapshot_hash)

    def test_fix_loop_multi_generation_isolation(self):
        # Generation 1: r1 gate task produces blocked verdict
        tid_r1 = "wf-d2-test-r1"
        self._create_task(tid_r1, node="test", status="agent_done")

        v_path_r1 = Path(gate_verdict_path(tid_r1))
        v_data_r1 = {"verdict": "blocked", "note": "syntax error in module_x"}
        v_bytes_r1 = json.dumps(v_data_r1, indent=2, ensure_ascii=False).encode("utf-8")
        v_path_r1.write_bytes(v_bytes_r1)
        hash_r1 = hashlib.sha256(v_bytes_r1).hexdigest()

        cmd1 = [sys.executable, "bin/herdr-task", "set", tid_r1, "completed", "--verdict", "blocked", "--note", "syntax error in module_x"]
        res1 = subprocess.run(cmd1, env=os.environ, capture_output=True, text=True)
        self.assertEqual(res1.returncode, 0)

        task_r1 = self.store.get_task(tid_r1)
        self.assertEqual(task_r1["stage_verdict"], "blocked")
        self.assertEqual(task_r1.get("evidence_snapshot_hash"), hash_r1)

        tid_r2 = "wf-d2-test-r2"
        # Fix-loop invalidation normalizes completed -> cleanup_ready -> cleaned -> superseded
        subprocess.run([sys.executable, "bin/herdr-task", "finalize", tid_r1], env=os.environ, check=True)
        subprocess.run([sys.executable, "bin/herdr-task", "supersede", tid_r1, "--by", tid_r2, "--reason", "fix-loop"], env=os.environ, check=True)

        # Generation 2: r2 gate task produces pass verdict
        self._create_task(tid_r2, node="test", status="agent_done")

        v_path_r2 = Path(gate_verdict_path(tid_r2))
        v_data_r2 = {"verdict": "pass", "note": "module_x syntax fixed and verified"}
        v_bytes_r2 = json.dumps(v_data_r2, indent=2, ensure_ascii=False).encode("utf-8")
        v_path_r2.write_bytes(v_bytes_r2)
        hash_r2 = hashlib.sha256(v_bytes_r2).hexdigest()

        cmd2 = [sys.executable, "bin/herdr-task", "set", tid_r2, "completed", "--verdict", "pass", "--note", "module_x syntax fixed and verified"]
        res2 = subprocess.run(cmd2, env=os.environ, capture_output=True, text=True)
        self.assertEqual(res2.returncode, 0)

        task_r2 = self.store.get_task(tid_r2)
        self.assertEqual(task_r2["stage_verdict"], "pass")
        self.assertEqual(task_r2.get("evidence_snapshot_hash"), hash_r2)

        # Crucial invariant: Generation 1 verdict & evidence snapshot MUST NOT be overwritten
        task_r1_after = self.store.get_task(tid_r1)
        self.assertEqual(task_r1_after["stage_verdict"], "blocked")
        self.assertEqual(task_r1_after.get("evidence_snapshot_hash"), hash_r1)
        self.assertTrue(v_path_r1.exists())
        self.assertEqual(hashlib.sha256(v_path_r1.read_bytes()).hexdigest(), hash_r1)

        # Generation 2 has its own distinct verdict and evidence
        self.assertNotEqual(hash_r1, hash_r2)
        self.assertTrue(v_path_r2.exists())
        self.assertEqual(hashlib.sha256(v_path_r2.read_bytes()).hexdigest(), hash_r2)


if __name__ == "__main__":
    unittest.main()
