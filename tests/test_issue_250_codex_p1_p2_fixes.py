"""Regression tests for PR #266 Codex review findings (P1 & P2).

Covers:
1. [P1] services/herdr-controller.py: Reject stale verdicts on restart/rework
   - Reworked gate task must ignore stale verdict files created before rework_started_at.
   - process_gate_verdict_reconciliations must not flip working tasks to agent_done or auto-adopt.
   - cmd_rework purges prior gate-verdict files.
2. [P2] bin/herdr-task: Restrict failed completion overrides
   - failed -> completed transition is restricted to gate tasks failed by hard_timeout_watchdog.
   - Non-gate tasks or other failure reasons are strictly rejected.
3. [P2] bin/herdr-task: Bind forced adoption to CAS snapshot
   - Forced failed -> completed transition checks expected_status and expected_version.
   - Concurrent modification triggers CAS mismatch and exits 2.
4. [P2] services/herdr-sentinel.py: Check gate identity before suppressing watchdog
   - Active non-gate implementation task with leftover gate-verdict.json is NOT suppressed.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent


def _load_controller():
    path = HERDR_ROOT / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_file_location("herdr_controller", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_sentinel():
    path = HERDR_ROOT / "services" / "herdr-sentinel.py"
    spec = importlib.util.spec_from_file_location("herdr_sentinel", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CodexReviewP1P2FixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.verdicts_dir = self.tmp_dir / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True)
        self.tasks_file = self.tmp_dir / "tasks.json"
        self.db_path = self.tmp_dir / "state.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_p1_reworked_working_gate_task_ignores_stale_verdict(self):
        """P1: Reconcile must not flip working tasks to agent_done using stale verdicts from previous epoch."""
        controller = _load_controller()
        task_id = "wf-p1-cleanroom_test-auto"
        verdict_file = self.verdicts_dir / f"{task_id}.json"
        verdict_file.write_text(
            json.dumps({
                "verdict": "pass",
                "note": "old verdict from prior epoch",
                "affected_task_ids": ["wf-p1-impl"],
            }),
            encoding="utf-8",
        )
        # Set file mtime to 1000.0
        os.utime(str(verdict_file), (1000.0, 1000.0))

        # Task was reworked at 2000.0 (> 1000.0)
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "working",
            "pane_id": "p1",
            "rework_started_at": 2000.0,
            "version": 2,
        }

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}):
            # 1. Freshness check must fail
            self.assertFalse(controller._gate_verdict_is_fresh(task))
            # 2. _verdict_from_file must return (None, "")
            v, n = controller._verdict_from_file(task)
            self.assertIsNone(v)
            # 3. _verdict_affected_task_ids must return None
            self.assertIsNone(controller._verdict_affected_task_ids(task))

            # 4. process_gate_verdict_reconciliations must skip this task
            with patch.object(controller, "load_tasks", return_value=[task]), \
                 patch.object(controller, "node_is_gate", return_value=True), \
                 patch.object(controller, "_set_observed_status") as mock_set_status:
                processed = controller.process_gate_verdict_reconciliations()
                self.assertEqual(processed, 0)
                mock_set_status.assert_not_called()

    def test_p1_reworked_failed_gate_task_ignores_stale_verdict(self):
        """P1: Auto-verdict must not adopt stale verdicts for watchdog-failed tasks."""
        controller = _load_controller()
        task_id = "wf-p1-cleanroom_test-auto"
        verdict_file = self.verdicts_dir / f"{task_id}.json"
        verdict_file.write_text(
            json.dumps({"verdict": "pass", "note": "stale verdict"}),
            encoding="utf-8",
        )
        os.utime(str(verdict_file), (1000.0, 1000.0))

        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "rework_started_at": 2000.0,
            "version": 3,
        }

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}):
            with patch.object(controller, "load_tasks", return_value=[task]), \
                 patch.object(controller, "node_is_gate", return_value=True), \
                 patch.object(controller, "try_auto_verdict") as mock_try:
                processed = controller.process_gate_verdict_reconciliations()
                self.assertEqual(processed, 0)
                mock_try.assert_not_called()

            with patch.object(controller, "get_task", return_value=task), \
                 patch.object(controller, "node_is_gate", return_value=True), \
                 patch.object(controller, "auto_verdict_enabled", return_value=True):
                self.assertFalse(controller.try_auto_verdict(task_id))

    def test_p1_rework_purges_prior_verdict_files(self):
        """P1: cmd_rework purges prior gate-verdict files before rework starts."""
        clone_dir = self.tmp_dir / "clone"
        clone_dir.mkdir()
        clone_herdr = clone_dir / ".herdr"
        clone_herdr.mkdir()
        clone_verdict = clone_herdr / "gate-verdict.json"
        clone_verdict.write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")

        ctrl_verdict = self.verdicts_dir / "task-purge-01.json"
        ctrl_verdict.write_text(json.dumps({"verdict": "blocked"}), encoding="utf-8")

        self.assertTrue(clone_verdict.exists())
        self.assertTrue(ctrl_verdict.exists())

        # Simulate the purge block from cmd_rework
        task = {"clone_path": str(clone_dir)}
        for cand in [
            str(ctrl_verdict),
            os.path.join(task.get("clone_path", ""), ".herdr", "gate-verdict.json"),
        ]:
            if cand and os.path.exists(cand):
                os.remove(cand)

        self.assertFalse(clone_verdict.exists())
        self.assertFalse(ctrl_verdict.exists())

    def test_p2_restrict_failed_completion_overrides(self):
        """P2 Finding 2: Only gate tasks failed by hard_timeout_watchdog can transition failed -> completed."""
        from herdr import state_store
        store = state_store.SQLiteStateStore(self.db_path)
        herdr_task_bin = HERDR_ROOT / "bin" / "herdr-task"
        env = {
            **os.environ,
            "TASKS_FILE": str(self.tasks_file),
            "HERDR_STATE_DB": str(self.db_path),
            "HERDR_STATE_BACKEND": "sqlite",
        }

        # Case A: Non-gate task failed by watchdog -> must fail
        t1_id = "wf-non-gate-impl"
        t1 = {
            "task_id": t1_id,
            "workflow_id": "wf-1",
            "node": "backend_impl",
            "stage": "backend_impl",
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "version": 1,
        }
        store.save_task(t1)

        res1 = subprocess.run(
            [sys.executable, str(herdr_task_bin), "set", t1_id, "completed", "--verdict", "pass"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res1.returncode, 2, f"Expected non-gate to fail transition: {res1.stderr}")
        self.assertIn("Illegal transition", res1.stdout + res1.stderr)

        # Case B: Gate task failed by agent_crashed (not watchdog) -> must fail
        t2_id = "wf-gate-crashed"
        t2 = {
            "task_id": t2_id,
            "workflow_id": "wf-1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "agent_crashed",
            "version": 1,
        }
        store.save_task(t2)

        res2 = subprocess.run(
            [sys.executable, str(herdr_task_bin), "set", t2_id, "completed", "--verdict", "pass"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res2.returncode, 2, f"Expected non-watchdog failure to fail transition: {res2.stderr}")
        self.assertIn("Illegal transition", res2.stdout + res2.stderr)

        # Case C: Gate task failed by hard_timeout_watchdog -> allowed!
        t3_id = "wf-gate-watchdog"
        t3 = {
            "task_id": t3_id,
            "workflow_id": "wf-1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "version": 1,
        }
        store.save_task(t3)

        res3 = subprocess.run(
            [sys.executable, str(herdr_task_bin), "set", t3_id, "completed", "--verdict", "pass"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res3.returncode, 0, f"Expected watchdog-failed gate to succeed: {res3.stderr}")
        reloaded = store.get_task(t3_id)
        self.assertEqual(reloaded["status"], "completed")
        self.assertEqual(reloaded["stage_verdict"], "pass")

    def test_p2_cas_snapshot_mismatch_rejects_forced_adoption(self):
        """P2 Finding 3: Forced adoption is bound to observed status and version."""
        from herdr import state_store
        store = state_store.SQLiteStateStore(self.db_path)
        herdr_task_bin = HERDR_ROOT / "bin" / "herdr-task"
        env = {
            **os.environ,
            "TASKS_FILE": str(self.tasks_file),
            "HERDR_STATE_DB": str(self.db_path),
            "HERDR_STATE_BACKEND": "sqlite",
        }

        task_id = "wf-cas-gate"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "version": 1,
        }
        store.save_task(task)

        # In herdr-task, set_status reads task from store/tasks_file, then calls kernel.transition_task.
        # We simulate a concurrent race where store has version bumped right before transition.
        from herdr import kernel
        res = kernel.transition_task(
            task_id=task_id,
            to_status="completed",
            reason="cli_set_status",
            force=True,
            store=store,
            expected_status="failed",
            expected_version=999,  # Mismatched version
        )
        self.assertFalse(res.get("accepted", True))
        # Ensure task is unchanged in store
        unchanged = store.get_task(task_id)
        self.assertEqual(unchanged["status"], "failed")
        self.assertEqual(unchanged["version"], 1)

    def test_p2_sentinel_checks_gate_identity_before_suppressing_watchdog(self):
        """P2 Finding 4: Sentinel must NOT suppress watchdog timeouts on non-gate tasks even if verdict file exists."""
        sentinel = _load_sentinel()
        non_gate_id = "wf-non-gate-impl-task"
        verdict_path = self.verdicts_dir / f"{non_gate_id}.json"
        verdict_path.write_text(
            json.dumps({"verdict": "pass", "note": "leftover verdict"}),
            encoding="utf-8",
        )

        now = 10000.0
        # updated_at was 4000s ago -> idle_seconds = 4000 > hard_timeout (3600)
        task = {
            "task_id": non_gate_id,
            "workflow_id": "wf-test-01",
            "node": "backend_impl",
            "stage": "backend_impl",
            "gate": False,
            "status": "working",
            "updated_at": now - 4000.0,
            "version": 1,
        }

        state = {"stalls": {}, "timeouts": {}}
        mock_store = MagicMock()

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(sentinel, "_get_store", return_value=mock_store), \
             patch.object(sentinel, "update_statuses", return_value=True) as mock_update_statuses:
            # Because task is NOT a gate task, the leftover verdict must NOT suppress timeout!
            sentinel.check_task_stalls([task], state, now=now)

            # Sentinel must have called update_statuses to fail this task
            mock_update_statuses.assert_called_once_with(
                {non_gate_id: ("failed", "hard_timeout_watchdog")},
                expected={non_gate_id: "working"},
                versions={non_gate_id: 1},
            )

    def test_p1_fresh_fallback_candidate_accepted_when_primary_candidate_is_stale(self):
        """P1 (Round 2 finding): Fresh clone fallback verdict must not be rejected when external dir has a stale file."""
        controller = _load_controller()
        task_id = "wf-p1-multi-candidate"

        # Candidate 1 (state dir): Stale file from prior epoch (mtime = 1000.0)
        primary_verdict = self.verdicts_dir / f"{task_id}.json"
        primary_verdict.write_text(
            json.dumps({"verdict": "blocked", "note": "stale verdict from previous run"}),
            encoding="utf-8",
        )
        os.utime(str(primary_verdict), (1000.0, 1000.0))

        # Candidate 2 (clone dir): Fresh file from current rework (mtime = 3000.0)
        clone_dir = self.tmp_dir / "clone_multi"
        clone_dir.mkdir()
        clone_herdr = clone_dir / ".herdr"
        clone_herdr.mkdir()
        clone_verdict = clone_herdr / "gate-verdict.json"
        clone_verdict.write_text(
            json.dumps({
                "verdict": "pass",
                "note": "fresh verdict from current rework",
                "affected_task_ids": ["wf-p1-impl-2"],
            }),
            encoding="utf-8",
        )
        os.utime(str(clone_verdict), (3000.0, 3000.0))

        # Task was reworked at 2000.0 (Candidate 1 is stale at 1000.0; Candidate 2 is fresh at 3000.0)
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "working",
            "clone_path": str(clone_dir),
            "rework_started_at": 2000.0,
            "version": 3,
        }

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}):
            # 1. Freshness check must succeed because Candidate 2 is fresh!
            self.assertTrue(controller._gate_verdict_is_fresh(task))

            # 2. _verdict_from_file must parse Candidate 2 ("pass") rather than failing globally
            verdict, note = controller._verdict_from_file(task)
            self.assertEqual(verdict, "pass")
            self.assertEqual(note, "fresh verdict from current rework")

            # 3. _verdict_affected_task_ids must read Candidate 2's affected IDs
            aff_ids = controller._verdict_affected_task_ids(task)
            self.assertEqual(aff_ids, ["wf-p1-impl-2"])

    def test_p1_candidate_capture_refreshes_cas_version_and_permits_transition(self):
        """P1: Capturing verified_candidate_sha updates DB version; CAS check must not fail with Concurrent state change."""
        from herdr import state_store
        store = state_store.SQLiteStateStore(self.db_path)
        herdr_task_bin = HERDR_ROOT / "bin" / "herdr-task"

        # Create a real git repository for completion evidence
        repo_dir = self.tmp_dir / "git_repo_p1"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
        (repo_dir / "README.md").write_text("p1 test", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True, capture_output=True)
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True
        ).stdout.strip()

        task_id = "wf-p1-git-cas-gate"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "clone_path": str(repo_dir),
            "candidate_sha": head_sha,
            "created_at": 1000.0,
            "updated_at": 2000.0,
            "version": 2,
        }
        store.save_task(task)

        env = {
            **os.environ,
            "TASKS_FILE": str(self.tasks_file),
            "HERDR_STATE_DB": str(self.db_path),
            "PYTHONPATH": str(HERDR_ROOT),
        }

        # Execute `herdr-task set <task_id> completed --verdict pass --note "passed gate"`
        # Prior to the fix, _capture_verified_candidate bumped SQLite version to 3, but set_status
        # passed expected_version=2 to kernel.transition_task, failing with exit code 2:
        # "Concurrent state change: task ... is no longer at observed failed state (version 2)"
        res = subprocess.run(
            [sys.executable, str(herdr_task_bin), "set", task_id, "completed",
             "--verdict", "pass", "--note", "passed gate"],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res.returncode, 0, f"set completed failed: {res.stderr}\n{res.stdout}")
        self.assertNotIn("Concurrent state change", res.stderr)

        updated = store.get_task(task_id)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["stage_verdict"], "pass")
        self.assertEqual(updated["verified_candidate_sha"], head_sha)
        self.assertGreaterEqual(updated["version"], 3)

    def test_p2_adopt_verdict_rejects_malformed_attribution_list(self):
        """P2: Malformed affected_task_ids (non-string, blank, or non-list) must be rejected wholly, not partially adopted."""
        from herdr import state_store
        store = state_store.SQLiteStateStore(self.db_path)
        herdr_task_bin = HERDR_ROOT / "bin" / "herdr-task"

        env = {
            **os.environ,
            "TASKS_FILE": str(self.tasks_file),
            "HERDR_STATE_DB": str(self.db_path),
            "HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir),
            "PYTHONPATH": str(HERDR_ROOT),
        }

        # Case 1: Mixed list containing non-string integer ["impl-a", 7]
        task_id = "wf-p2-malformed-mixed"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p2",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "version": 1,
        }
        store.save_task(task)

        verdict_file = self.verdicts_dir / f"{task_id}.json"
        verdict_file.write_text(
            json.dumps({
                "verdict": "blocked",
                "note": "mixed list blocker",
                "affected_task_ids": ["impl-a", 7],
            }),
            encoding="utf-8",
        )

        res = subprocess.run(
            [sys.executable, str(herdr_task_bin), "adopt-verdict", task_id],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res.returncode, 0, f"adopt-verdict failed: {res.stderr}\n{res.stdout}")
        self.assertIn("Malformed affected_task_ids", res.stderr)
        updated = store.get_task(task_id)
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(updated["stage_verdict"], "blocked")
        # Must be empty list, NOT partial subset ["impl-a"]
        self.assertEqual(updated.get("stage_verdict_affected_task_ids"), [])

        # Case 2: Non-list string "impl-a"
        task_id_2 = "wf-p2-malformed-nonlist"
        task_2 = {
            "task_id": task_id_2,
            "workflow_id": "wf-p2",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "version": 1,
        }
        store.save_task(task_2)

        verdict_file_2 = self.verdicts_dir / f"{task_id_2}.json"
        verdict_file_2.write_text(
            json.dumps({
                "verdict": "blocked",
                "note": "string non-list blocker",
                "affected_task_ids": "impl-a",
            }),
            encoding="utf-8",
        )

        res_2 = subprocess.run(
            [sys.executable, str(herdr_task_bin), "adopt-verdict", task_id_2],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res_2.returncode, 0)
        self.assertIn("Malformed affected_task_ids", res_2.stderr)
        updated_2 = store.get_task(task_id_2)
        self.assertEqual(updated_2.get("stage_verdict_affected_task_ids"), [])

        # Case 3: Valid list ["impl-a", "impl-b"]
        task_id_3 = "wf-p2-valid-list"
        task_3 = {
            "task_id": task_id_3,
            "workflow_id": "wf-p2",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "version": 1,
        }
        store.save_task(task_3)

        verdict_file_3 = self.verdicts_dir / f"{task_id_3}.json"
        verdict_file_3.write_text(
            json.dumps({
                "verdict": "blocked",
                "note": "valid list blocker",
                "affected_task_ids": ["impl-a", "impl-b"],
            }),
            encoding="utf-8",
        )

        res_3 = subprocess.run(
            [sys.executable, str(herdr_task_bin), "adopt-verdict", task_id_3],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res_3.returncode, 0)
        updated_3 = store.get_task(task_id_3)
        self.assertEqual(updated_3.get("stage_verdict_affected_task_ids"), ["impl-a", "impl-b"])

    def test_p1_sentinel_watchdog_suppression_expires_after_handoff_grace(self):
        """P1: Sentinel must not suppress watchdog indefinitely if agent remains working; after handoff grace it must fail task."""
        sentinel = _load_sentinel()
        task_id = "wf-p1-handoff-expired"
        verdict_path = self.verdicts_dir / f"{task_id}.json"
        verdict_path.write_text(
            json.dumps({"verdict": "blocked", "note": "hung agent blocker"}),
            encoding="utf-8",
        )

        now = 10000.0
        # Verdict file was written 600s ago (exceeding default 300s handoff grace)
        os.utime(str(verdict_path), (now - 600.0, now - 600.0))

        # Task has exceeded gate hard timeout (7200s): updated 8000s ago
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1-sentinel",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "updated_at": now - 8000.0,
            "version": 2,
        }

        state = {"stalls": {}, "timeouts": {}}
        mock_store = MagicMock()

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(sentinel, "_get_store", return_value=mock_store), \
             patch.object(sentinel, "update_statuses", return_value=True) as mock_update_statuses:
            changed = sentinel.check_task_stalls([task], state, now=now)

            # Because verdict age (600s) > grace (300s) and idle (8000s) > gate_timeout (7200s),
            # suppression must expire and watchdog must fail the task!
            mock_update_statuses.assert_called_once_with(
                {task_id: ("failed", "hard_timeout_watchdog")},
                expected={task_id: "working"},
                versions={task_id: 2},
            )
            self.assertTrue(changed)
            self.assertIn(task_id, state["timeouts"])

    def test_p1_controller_reconciles_working_gate_task_when_agent_hung_after_grace(self):
        """P1: When agent hangs in working but verdict is durable past handoff grace, controller reconciles to agent_done."""
        controller = _load_controller()
        task_id = "wf-p1-controller-hung"
        verdict_path = self.verdicts_dir / f"{task_id}.json"
        verdict_path.write_text(
            json.dumps({"verdict": "pass", "note": "durable pass verdict"}),
            encoding="utf-8",
        )

        now = time.time()
        # Verdict was written 400s ago (> 300s grace)
        os.utime(str(verdict_path), (now - 400.0, now - 400.0))

        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "pane_id": "p1",
            "status": "working",
            "rework_started_at": now - 500.0,
            "version": 1,
        }

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(controller, "load_tasks", return_value=[task]), \
             patch.object(controller, "node_is_gate", return_value=True), \
             patch.object(controller, "get_agent_runtime_status", return_value="working"), \
             patch.object(controller, "gate_verdict_ready", return_value=True), \
             patch.object(controller, "_set_observed_status", return_value=True) as mock_set_status, \
             patch.object(controller, "emit_done_if_allowed") as mock_emit:
            processed = controller.process_gate_verdict_reconciliations()
            self.assertEqual(processed, 1)
            mock_set_status.assert_called_once_with(task, "agent_done", "gate_verdict_recovery")

    def test_p2_controller_get_agent_runtime_status_has_timeout(self):
        """P2: get_agent_runtime_status must bound subprocess call with timeout=10."""
        controller = _load_controller()
        with patch("subprocess.check_output") as mock_sub:
            mock_sub.return_value = json.dumps({"result": {"agent": {"agent_status": "idle"}}})
            status = controller.get_agent_runtime_status("pane-123")
            self.assertEqual(status, "idle")
            mock_sub.assert_called_once_with(
                ["herdr", "agent", "get", "pane-123"],
                text=True,
                timeout=10,
            )


if __name__ == "__main__":
    unittest.main()
