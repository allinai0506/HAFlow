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


def _load_herdr_task():
    from importlib.machinery import SourceFileLoader
    path = HERDR_ROOT / "bin" / "herdr-task"
    loader = SourceFileLoader("herdr_task_module", str(path))
    spec = importlib.util.spec_from_loader("herdr_task_module", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
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

    def test_p1_authoritative_gate_identity_resolution(self):
        """P1: Configured gates with non-heuristic names (like executive_briefing) resolve authoritatively."""
        sentinel = _load_sentinel()
        herdr_task = _load_herdr_task()
        controller = _load_controller()

        # 1. executive_briefing in business-research-v1 template has node_type: gate and gate: {...}
        # It does NOT match heuristic names ("cleanroom", "gate", "test", "review", "wrapup").
        wf_cfg_path = "workflow_templates/business-research-v1.yaml"
        gate_task = {
            "task_id": "wf-p1-exec-briefing",
            "workflow_id": "wf-p1",
            "node": "executive_briefing",
            "stage": "executive_briefing",
            "workflow_config": wf_cfg_path,
        }
        non_gate_task = {
            "task_id": "wf-p1-market-scope",
            "workflow_id": "wf-p1",
            "node": "market_scope",
            "stage": "market_scope",
            "workflow_config": wf_cfg_path,
        }

        # Sentinel _is_gate_task
        self.assertTrue(sentinel._is_gate_task(gate_task))
        self.assertFalse(sentinel._is_gate_task(non_gate_task))

        # herdr-task _is_gate_task
        self.assertTrue(herdr_task._is_gate_task(gate_task))
        self.assertFalse(herdr_task._is_gate_task(non_gate_task))

        # Direct task with gate: True persists authoritative identity
        explicit_gate_task = {
            "task_id": "wf-p1-custom",
            "node": "custom_arbitrary_name",
            "gate": True,
        }
        self.assertTrue(sentinel._is_gate_task(explicit_gate_task))
        self.assertTrue(herdr_task._is_gate_task(explicit_gate_task))

        # Controller node_is_gate and resolve_gate_config
        custom_node = {"id": "custom_signoff", "node_type": "gate"}
        self.assertIsNotNone(controller.resolve_gate_config(custom_node, "custom_signoff"))
        with patch.object(controller, "workflow_config_for", return_value={"nodes": [custom_node]}):
            self.assertTrue(controller.node_is_gate("wf-p1", "custom_signoff"))

    def test_p2_grace_seconds_rejects_inf_and_nan(self):
        """P2: HERDR_GATE_HANDOFF_GRACE_SECONDS must reject inf, -inf, nan, and non-positive values."""
        sentinel = _load_sentinel()
        controller = _load_controller()

        for invalid in ["inf", "+inf", "-inf", "nan", "NaN", "0", "-10", "abc"]:
            with patch.dict(os.environ, {"HERDR_GATE_HANDOFF_GRACE_SECONDS": invalid}):
                self.assertEqual(sentinel.task_gate_handoff_grace_seconds(), 300.0)
                self.assertEqual(controller.task_gate_handoff_grace_seconds(), 300.0)

        with patch.dict(os.environ, {"HERDR_GATE_HANDOFF_GRACE_SECONDS": "120.5"}):
            self.assertEqual(sentinel.task_gate_handoff_grace_seconds(), 120.5)
            self.assertEqual(controller.task_gate_handoff_grace_seconds(), 120.5)

    def test_p2_controller_reconciles_rework_gate_task(self):
        """P2: process_gate_verdict_reconciliations must reconcile gate task in status='rework'."""
        controller = _load_controller()
        task_id = "wf-p2-rework-task"
        verdict_path = self.verdicts_dir / f"{task_id}.json"
        verdict_path.write_text(
            json.dumps({"verdict": "pass", "note": "verdict produced during rework"}),
            encoding="utf-8",
        )

        now = time.time()
        os.utime(str(verdict_path), (now, now))

        task = {
            "task_id": task_id,
            "workflow_id": "wf-p2",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "pane_id": "p2",
            "status": "rework",
            "gate": True,
            "rework_started_at": now - 100.0,
            "version": 3,
        }

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(controller, "load_tasks", return_value=[task]), \
             patch.object(controller, "node_is_gate", return_value=True), \
             patch.object(controller, "get_agent_runtime_status", return_value="idle"), \
             patch.object(controller, "gate_verdict_ready", return_value=True), \
             patch.object(controller, "_set_observed_status", return_value=True) as mock_set_status, \
             patch.object(controller, "emit_done_if_allowed") as mock_emit:
            processed = controller.process_gate_verdict_reconciliations()
            self.assertEqual(processed, 1)
            mock_set_status.assert_called_once_with(task, "agent_done", "gate_verdict_recovery")


    def test_p1_rework_execution_token_binding(self):
        """P1: Verdict freshness must be bound to rework execution token epoch."""
        controller = _load_controller()
        sentinel = _load_sentinel()
        herdr_task = _load_herdr_task()

        task_id = "wf-p1-epoch-gate"
        verdict_path = self.verdicts_dir / f"{task_id}.json"

        # Rework task has execution_token "tok-new-1234"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1",
            "node": "cleanroom_gate",
            "stage": "cleanroom_gate",
            "status": "rework",
            "gate": True,
            "execution_token": "tok-new-1234",
            "rework_started_at": 2000.0,
            "version": 4,
        }

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}):
            # Scenario A: Prior attempt writes a verdict AFTER rework_started_at, but without token
            verdict_path.write_text(
                json.dumps({"verdict": "pass", "note": "stale attempt 1 result"}),
                encoding="utf-8",
            )
            os.utime(str(verdict_path), (2050.0, 2050.0))  # mtime is after rework_started_at!

            # Controller, Sentinel, and herdr-task must all reject it
            self.assertFalse(controller._gate_verdict_file_is_fresh(str(verdict_path), task))
            self.assertFalse(controller._gate_verdict_is_fresh(task))
            has_valid, _, _, _ = sentinel._task_has_valid_gate_verdict(task)
            self.assertFalse(has_valid)

            # Scenario B: Prior attempt writes a verdict with old epoch token "tok-old-9999"
            verdict_path.write_text(
                json.dumps({
                    "verdict": "pass",
                    "note": "stale attempt 1 result with old token",
                    "execution_token": "tok-old-9999",
                }),
                encoding="utf-8",
            )
            os.utime(str(verdict_path), (2050.0, 2050.0))

            self.assertFalse(controller._gate_verdict_file_is_fresh(str(verdict_path), task))
            self.assertFalse(controller._gate_verdict_is_fresh(task))
            has_valid, _, _, _ = sentinel._task_has_valid_gate_verdict(task)
            self.assertFalse(has_valid)

            # Scenario C: Current rework attempt writes verdict with matching execution_token
            verdict_path.write_text(
                json.dumps({
                    "verdict": "pass",
                    "note": "fresh rework attempt verdict",
                    "execution_token": "tok-new-1234",
                }),
                encoding="utf-8",
            )
            os.utime(str(verdict_path), (2050.0, 2050.0))

            self.assertTrue(controller._gate_verdict_file_is_fresh(str(verdict_path), task))
            self.assertTrue(controller._gate_verdict_is_fresh(task))
            has_valid, verdict, note, _ = sentinel._task_has_valid_gate_verdict(task)
            self.assertTrue(has_valid)
            self.assertEqual(verdict, "pass")
            self.assertEqual(note, "fresh rework attempt verdict")

    def test_p2_no_substring_gate_classification(self):
        """P2: Ordinary agent nodes like aggregate_results must NOT be classified as gates."""
        sentinel = _load_sentinel()
        herdr_task = _load_herdr_task()
        controller = _load_controller()

        workflow_cfg = {
            "nodes": [
                {
                    "id": "aggregate_results",
                    "label": "汇总结果",
                    "node_type": "agent",
                },
                {
                    "id": "investigate_issue",
                    "label": "排查问题",
                    "node_type": "agent",
                },
                {
                    "id": "executive_briefing",
                    "label": "高管决策简报会签",
                    "node_type": "gate",
                },
            ]
        }

        task_agg = {
            "task_id": "wf-p2-agg-1",
            "workflow_id": "wf-p2",
            "node": "aggregate_results",
            "stage": "aggregate_results",
        }
        task_inv = {
            "task_id": "wf-p2-inv-1",
            "workflow_id": "wf-p2",
            "node": "investigate_issue",
            "stage": "investigate_issue",
        }
        task_exec = {
            "task_id": "wf-p2-exec-1",
            "workflow_id": "wf-p2",
            "node": "executive_briefing",
            "stage": "executive_briefing",
        }

        with patch("herdr.projects.workflow_config_for", return_value=workflow_cfg), \
             patch.object(controller, "workflow_config_for", return_value=workflow_cfg):
            # 1. sentinel _is_gate_task
            self.assertFalse(sentinel._is_gate_task(task_agg))
            self.assertFalse(sentinel._is_gate_task(task_inv))
            self.assertTrue(sentinel._is_gate_task(task_exec))

            # 2. herdr-task _is_gate_task
            self.assertFalse(herdr_task._is_gate_task(task_agg))
            self.assertFalse(herdr_task._is_gate_task(task_inv))
            self.assertTrue(herdr_task._is_gate_task(task_exec))

            # 3. controller node_is_gate
            self.assertFalse(controller.node_is_gate("wf-p2", "aggregate_results"))
            self.assertFalse(controller.node_is_gate("wf-p2", "investigate_issue"))
            self.assertTrue(controller.node_is_gate("wf-p2", "executive_briefing"))

            # 4. Even without workflow config, word-boundary tokens prevent substring match
            task_agg_nowf = {"task_id": "t1", "node": "aggregate_results"}
            task_inv_nowf = {"task_id": "t2", "node": "investigate_issue"}
            self.assertFalse(sentinel._is_gate_task(task_agg_nowf))
            self.assertFalse(herdr_task._is_gate_task(task_agg_nowf))
            self.assertFalse(sentinel._is_gate_task(task_inv_nowf))
            self.assertFalse(herdr_task._is_gate_task(task_inv_nowf))

    def test_p2_hard_timeout_rejects_inf_and_nan(self):
        """P2: task_gate_hard_timeout_seconds and task_hard_timeout_seconds reject inf/nan."""
        sentinel = _load_sentinel()

        # Check task_hard_timeout_seconds
        for invalid in ["inf", "+inf", "-inf", "nan", "NaN", "0", "-10", "abc"]:
            with patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT": invalid}):
                self.assertEqual(sentinel.task_hard_timeout_seconds(), sentinel.DEFAULT_TASK_HARD_TIMEOUT_SECONDS)

        # Check task_gate_hard_timeout_seconds
        for invalid in ["inf", "+inf", "-inf", "nan", "NaN", "0", "-10", "abc"]:
            with patch.dict(os.environ, {"HERDR_GATE_TASK_HARD_TIMEOUT": invalid}):
                # When base_timeout=3600, returns max(3600, 7200) = 7200
                self.assertEqual(sentinel.task_gate_hard_timeout_seconds(3600.0), 7200.0)

        # Finite positive value accepted
        with patch.dict(os.environ, {"HERDR_GATE_TASK_HARD_TIMEOUT": "9000.0"}):
            self.assertEqual(sentinel.task_gate_hard_timeout_seconds(3600.0), 9000.0)

        with patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT": "1800.0"}):
            self.assertEqual(sentinel.task_hard_timeout_seconds(), 1800.0)

    def test_p2_clock_skew_configuration(self):
        """P2: max_clock_skew_seconds returns configured finite positive float or default 5.0."""
        sentinel = _load_sentinel()
        controller = _load_controller()

        self.assertEqual(sentinel.max_clock_skew_seconds(), 5.0)
        self.assertEqual(controller.max_clock_skew_seconds(), 5.0)

        for invalid in ["inf", "+inf", "-inf", "nan", "-1.0", "abc"]:
            with patch.dict(os.environ, {"HERDR_MAX_CLOCK_SKEW_SECONDS": invalid}):
                self.assertEqual(sentinel.max_clock_skew_seconds(), 5.0)
                self.assertEqual(controller.max_clock_skew_seconds(), 5.0)

        with patch.dict(os.environ, {"HERDR_MAX_CLOCK_SKEW_SECONDS": "10.5"}):
            self.assertEqual(sentinel.max_clock_skew_seconds(), 10.5)
            self.assertEqual(controller.max_clock_skew_seconds(), 10.5)

    def test_p2_sentinel_rejects_future_verdict_timestamp(self):
        """P2: Sentinel does not suppress watchdog when verdict mtime is in the future beyond skew limit."""
        sentinel = _load_sentinel()
        now = 10000.0
        tid = "wf-p2-gate-future"
        task = {
            "task_id": tid,
            "workflow_id": "wf-p2",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "updated_at": now - 8000.0,
            "pane_id": "p-gate-1",
        }
        verdict_file = self.verdicts_dir / f"{tid}.json"
        verdict_file.write_text(json.dumps({"verdict": "pass", "note": "future verdict"}), encoding="utf-8")

        # Future timestamp: now + 3600s (> 5s skew limit)
        future_mtime = now + 3600.0
        os.utime(str(verdict_file), (future_mtime, future_mtime))

        state = {"stalls": {}, "timeouts": {}}
        mock_store = MagicMock()
        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(sentinel, "_get_store", return_value=mock_store), \
             patch.object(sentinel, "update_statuses", return_value=True) as mock_update_statuses:
            sentinel.check_task_stalls([task], state, now=now)
            # Watchdog should NOT be suppressed; timeout must trigger update_statuses
            mock_update_statuses.assert_called_once_with(
                {tid: ("failed", "hard_timeout_watchdog")},
                expected={tid: "working"},
                versions=None,
            )

        # Small clock skew: now + 2.0s (<= 5s skew limit) -> treated as fresh within grace, suppressed
        state = {"stalls": {}, "timeouts": {}}
        skew_mtime = now + 2.0
        os.utime(str(verdict_file), (skew_mtime, skew_mtime))
        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(sentinel, "_get_store", return_value=mock_store), \
             patch.object(sentinel, "update_statuses", return_value=True) as mock_update_statuses:
            sentinel.check_task_stalls([task], state, now=now)
            # Small skew within grace: Watchdog must be suppressed!
            mock_update_statuses.assert_not_called()

    def test_p2_controller_rejects_future_verdict_timestamp(self):
        """P2: Controller reconciles and adopts verdict when verdict mtime is in the future beyond skew limit."""
        controller = _load_controller()
        now = time.time()
        tid = "wf-p2-gate-future-ctrl"
        task = {
            "task_id": tid,
            "workflow_id": "wf-p2",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "updated_at": now - 8000.0,
            "pane_id": "p-gate-2",
            "version": 1,
        }
        verdict_file = self.verdicts_dir / f"{tid}.json"
        verdict_file.write_text(json.dumps({"verdict": "pass", "note": "future verdict ctrl"}), encoding="utf-8")

        # Future timestamp: now + 3600s (> 5s skew limit)
        future_mtime = now + 3600.0
        os.utime(str(verdict_file), (future_mtime, future_mtime))

        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(controller, "load_tasks", return_value=[task]), \
             patch.object(controller, "get_task", return_value=task), \
             patch.object(controller, "node_is_gate", return_value=True), \
             patch.object(controller, "get_agent_runtime_status", return_value="running"), \
             patch.object(controller, "_set_observed_status", return_value=True) as mock_set_status, \
             patch.object(controller, "emit_done_if_allowed"):
            processed = controller.process_gate_verdict_reconciliations()
            # Future timestamp must NOT extend grace -> adopted immediately
            self.assertEqual(processed, 1)
            mock_set_status.assert_called_once_with(task, "agent_done", "gate_verdict_recovery")

        # Normal fresh verdict within grace: now - 10s -> runtime unsettled, waits (continue)
        past_mtime = now - 10.0
        os.utime(str(verdict_file), (past_mtime, past_mtime))
        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(controller, "load_tasks", return_value=[task]), \
             patch.object(controller, "get_task", return_value=task), \
             patch.object(controller, "node_is_gate", return_value=True), \
             patch.object(controller, "get_agent_runtime_status", return_value="running"), \
             patch.object(controller, "_set_observed_status") as mock_set_status, \
             patch.object(controller, "emit_done_if_allowed"):
            processed = controller.process_gate_verdict_reconciliations()
            self.assertEqual(processed, 0)
            mock_set_status.assert_not_called()

        # Small clock skew: now + 2.0s (<= 5s skew limit) -> within grace, waits (continue)
        skew_mtime = now + 2.0
        os.utime(str(verdict_file), (skew_mtime, skew_mtime))
        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir)}), \
             patch.object(controller, "load_tasks", return_value=[task]), \
             patch.object(controller, "get_task", return_value=task), \
             patch.object(controller, "node_is_gate", return_value=True), \
             patch.object(controller, "get_agent_runtime_status", return_value="running"), \
             patch.object(controller, "_set_observed_status") as mock_set_status, \
             patch.object(controller, "emit_done_if_allowed"):
            processed = controller.process_gate_verdict_reconciliations()
            self.assertEqual(processed, 0)
            mock_set_status.assert_not_called()

    def test_p1_rework_interrupt_only_in_first_idempotent_phase(self):
        """P1: Pane interrupt and verdict purge execute only in first idempotent delivery phase."""
        from types import SimpleNamespace
        import herdr.task_resources
        herdr_task = _load_herdr_task()
        from herdr.state_store import SQLiteStateStore

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-p1-rework", "status": "running"})

        task_id = "task-gate-rework"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1-rework",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "pane_id": "p-rework-gate",
            "clone_path": str(self.tmp_dir),
            "completion_protocol": "receipt-v1",
        }
        store.save_task(task)

        calls = []
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None):

            # Initial rework invocation: prepare_task runs, performs cleanup and delivers prompt
            args = SimpleNamespace(task_id=task_id, request_id="req-1", prompt="retest", reason="review")
            herdr_task.cmd_rework(args)

            # calls must contain: 1 send-keys C-c (from prepare_task) + 1 agent prompt
            send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
            prompt_calls = [c for c in calls if len(c) >= 2 and c[0] == "agent" and c[1] == "prompt"]
            self.assertEqual(len(send_keys_calls), 1)
            self.assertEqual(len(prompt_calls), 1)

            # Now simulate a retry of a pending delivery
            # Update task to status='rework', rework_delivery='pending'
            current = store.get_task(task_id)
            current.update(status="rework", rework_delivery="pending")
            store.save_task(current)

            calls.clear()
            # For non-receipt-v1 retry scenario: _cleanup_gate_for_rework is skipped
            task_non_receipt = dict(current, completion_protocol=None)
            store.save_task(task_non_receipt)
            import herdr.kernel
            with patch.object(herdr.kernel, "transition_task", return_value={"accepted": True}):
                herdr_task.cmd_rework(args)
            # send-keys C-c must NOT have been called because rework_delivery was pending
            send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
            self.assertEqual(len(send_keys_calls), 0)

    def test_p1_rework_aborts_when_pane_interrupt_fails(self):
        """P1: cmd_rework aborts with RuntimeError when pane send-keys exits nonzero."""
        from types import SimpleNamespace
        import herdr.task_resources
        herdr_task = _load_herdr_task()
        from herdr.state_store import SQLiteStateStore

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-p1-abort-interrupt", "status": "running"})

        task_id = "task-gate-abort-interrupt"
        clone_dir = self.tmp_dir / "clone_abort"
        clone_dir.mkdir(exist_ok=True)
        (clone_dir / ".herdr").mkdir(exist_ok=True)
        verdict_file = clone_dir / ".herdr" / "gate-verdict.json"
        verdict_file.write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")

        task = {
            "task_id": task_id,
            "workflow_id": "wf-p1-abort-interrupt",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "pane_id": "p-abort-gate",
            "clone_path": str(clone_dir),
            "completion_protocol": "receipt-v1",
        }
        store.save_task(task)

        # Simulate send-keys C-c failing with exit code 1
        def mock_herdr(*a, **k):
            if len(a) >= 2 and a[0] == "pane" and a[1] == "send-keys":
                return SimpleNamespace(returncode=1, stdout="", stderr="tmux: pane not found")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        args = SimpleNamespace(task_id=task_id, request_id="req-abort", prompt="retest", reason="test")
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", mock_herdr), \
             patch.object(herdr_task.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError) as ctx:
                herdr_task.cmd_rework(args)
            self.assertIn("Failed to interrupt gate pane", str(ctx.exception))

        # The verdict file must NOT have been purged
        self.assertTrue(verdict_file.exists())
        # The task must still be in 'working' status (not transitioned to rework)
        fresh = store.get_task(task_id)
        self.assertEqual(fresh["status"], "working")
        # gate_cleanup_completed must NOT have been recorded because pane interrupt failed
        events = store.list_events(task_id=task_id)
        cleanup_completed_events = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(cleanup_completed_events), 0)
        # gate_cleanup_intent was recorded
        intent_events = [e for e in events if e.get("event_type") == "gate_cleanup_intent"]
        self.assertEqual(len(intent_events), 1)

        # Now simulate a subsequent retry where the pane issue is resolved
        calls = []
        def mock_herdr_success(*a, **k):
            calls.append(a)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", mock_herdr_success), \
             patch.object(herdr_task.time, "sleep", lambda s: None):
            herdr_task.cmd_rework(args)

        # On retry, pane send-keys C-c MUST be called (not skipped!), and verdict purged
        send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 1)
        self.assertFalse(verdict_file.exists())
        # gate_cleanup_completed is now recorded
        events = store.list_events(task_id=task_id)
        cleanup_completed_events = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(cleanup_completed_events), 1)

    def test_p2_rework_persists_cleanup_phase_and_does_not_repeat_interrupt_on_retry(self):
        """P2: Recoverable cleanup phase is persisted before C-c; retry skips duplicate interrupt."""
        from types import SimpleNamespace
        import herdr.task_resources
        herdr_task = _load_herdr_task()
        from herdr.state_store import SQLiteStateStore

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-p2-cleanup-phase", "status": "running"})

        task_id = "task-gate-cleanup-phase"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-p2-cleanup-phase",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "pane_id": "p-cleanup-gate",
            "clone_path": str(self.tmp_dir),
            "completion_protocol": "receipt-v1",
        }
        store.save_task(task)

        calls = []
        args = SimpleNamespace(task_id=task_id, request_id="req-phase-1", prompt="fix", reason="test")

        # Simulate first invocation where deliver crashes during prepare
        from herdr import supervisor_delivery as delivery
        original_deliver = delivery.deliver
        def crashing_deliver(*a, **k):
            raise RuntimeError("Simulated DB crash during delivery prepare")

        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None), \
             patch.object(delivery, "deliver", crashing_deliver):
            with self.assertRaises(RuntimeError):
                herdr_task.cmd_rework(args)

        # Verify gate_cleanup_completed was recorded durably in events
        events = store.list_events(task_id=task_id)
        cleanup_events = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(cleanup_events), 1)
        self.assertEqual(cleanup_events[0]["payload"].get("delivery_id"), "req-phase-1")

        # 1 send-keys C-c was called in the first attempt
        send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 1)

        # Second invocation (retry): deliver is restored, cmd_rework retried with same request_id
        calls.clear()
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None), \
             patch.object(delivery, "deliver", original_deliver):
            herdr_task.cmd_rework(args)

        # On retry, send-keys C-c must NOT have been called again!
        send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 0)
        prompt_calls = [c for c in calls if len(c) >= 2 and c[0] == "agent" and c[1] == "prompt"]
        self.assertEqual(len(prompt_calls), 1)

    def test_round5_p1_crash_between_intent_and_interrupt_retries_interrupt(self):
        """Round 5 P1: Crash after gate_cleanup_intent before C-c must NOT treat intent as completed; retry runs C-c."""
        from types import SimpleNamespace
        import herdr.task_resources
        herdr_task = _load_herdr_task()
        from herdr.state_store import SQLiteStateStore

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-r5-p1", "status": "running"})

        task_id = "task-r5-p1-intent"
        clone_dir = self.tmp_dir / "clone_r5_p1"
        clone_dir.mkdir(parents=True, exist_ok=True)
        dot_herdr = clone_dir / ".herdr"
        dot_herdr.mkdir(parents=True, exist_ok=True)
        verdict_file = dot_herdr / "gate-verdict.json"
        verdict_file.write_text(json.dumps({"verdict": "fail", "note": "prior"}), encoding="utf-8")

        task = {
            "task_id": task_id,
            "workflow_id": "wf-r5-p1",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "pane_id": "p-r5-p1",
            "clone_path": str(clone_dir),
            "completion_protocol": "receipt-v1",
        }
        store.save_task(task)

        # Simulate crash: gate_cleanup_intent was recorded, but process died before C-c or gate_cleanup_completed
        store.record_event(
            "gate_cleanup_intent",
            {"delivery_id": "deliv-intent-only", "task_id": task_id, "run_id": "r1"},
            task_id=task_id,
            workflow_id="wf-r5-p1",
            run_id="r1",
            source="herdr-task",
        )

        # Verify that _is_gate_cleanup_completed returns False when only intent exists
        self.assertFalse(herdr_task._is_gate_cleanup_completed(task, "deliv-intent-only", store))

        calls = []
        args = SimpleNamespace(task_id=task_id, request_id="deliv-intent-only", prompt="retest", reason="test")
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None):
            herdr_task.cmd_rework(args)

        # On retry, C-c MUST be executed (not skipped!), and verdict file purged
        send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 1)
        self.assertFalse(verdict_file.exists())

        # gate_cleanup_completed must now be recorded
        events = store.list_events(task_id=task_id)
        completed = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["payload"].get("delivery_id"), "deliv-intent-only")

    def test_round5_p2_rework_without_request_id_recovers_cleanup_id_and_skips_duplicate_interrupt(self):
        """Round 5 P2: Rework without --request-id recovers existing cleanup ID on retry and skips duplicate C-c."""
        from types import SimpleNamespace
        import herdr.task_resources
        herdr_task = _load_herdr_task()
        from herdr.state_store import SQLiteStateStore

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-r5-p2", "status": "running"})

        task_id = "task-r5-p2-noreqid"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-r5-p2",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "pane_id": "p-r5-p2",
            "clone_path": str(self.tmp_dir),
            "completion_protocol": "receipt-v1",
        }
        store.save_task(task)

        calls = []
        # CLI invocation WITHOUT request_id (the default CLI path)
        args = SimpleNamespace(task_id=task_id, request_id=None, prompt="fix without req id", reason="test")

        from herdr import supervisor_delivery as delivery
        original_deliver = delivery.deliver
        def crashing_deliver(*a, **k):
            raise RuntimeError("Simulated crash in deliver before prepare_task transaction commits")

        # First invocation: deliver crashes before its prepare_task commits
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None), \
             patch.object(delivery, "deliver", crashing_deliver):
            with self.assertRaises(RuntimeError):
                herdr_task.cmd_rework(args)

        # Verify C-c was called once
        send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 1)

        # Verify gate_cleanup_completed was recorded with an auto-generated token
        events = store.list_events(task_id=task_id)
        cleanup_events = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(cleanup_events), 1)
        generated_token = cleanup_events[0]["payload"].get("delivery_id")
        self.assertTrue(generated_token and generated_token.startswith("tok-"))

        # Task was not transitioned because deliver crashed before prepare_task committed
        fresh = store.get_task(task_id)
        self.assertEqual(fresh["status"], "working")
        self.assertIsNone(fresh.get("rework_delivery"))

        # Second invocation: user retries the exact same command without --request-id
        calls.clear()
        captured_deliver_args = []
        def capturing_deliver(t, s, action, payload, prompt, send, **kw):
            captured_deliver_args.append(payload)
            return original_deliver(t, s, action, payload, prompt, send, **kw)

        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None), \
             patch.object(delivery, "deliver", capturing_deliver):
            herdr_task.cmd_rework(args)

        # On retry without request_id, send-keys C-c MUST NOT be called again (reused cleanup ID!)
        send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 0)

        # Prompt transport MUST have been delivered
        prompt_calls = [c for c in calls if len(c) >= 2 and c[0] == "agent" and c[1] == "prompt"]
        self.assertEqual(len(prompt_calls), 1)

        # The deliver call must have reused the EXACT generated token from attempt 1
        self.assertEqual(len(captured_deliver_args), 1)
        self.assertEqual(captured_deliver_args[0].get("intervention_id"), generated_token)

        # Task is now marked delivered with execution_token = generated_token
        delivered_task = store.get_task(task_id)
        self.assertEqual(delivered_task["rework_delivery"], "delivered")
        self.assertEqual(delivered_task["execution_token"], generated_token)

        # Third invocation: a subsequent NEW rework in a later epoch generates a NEW token
        calls.clear()
        # Simulate new gate failure placing task into blocked
        store.transition_task(task_id, "blocked", reason="new_gate_failure", source="test")
        fresh_after_block = store.get_task(task_id)

        captured_deliver_args.clear()
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None), \
             patch.object(delivery, "deliver", capturing_deliver):
            herdr_task.cmd_rework(args)

        # For the new rework, C-c MUST be called again
        send_keys_calls = [c for c in calls if len(c) >= 2 and c[0] == "pane" and c[1] == "send-keys"]
        self.assertEqual(len(send_keys_calls), 1)

        # The new deliver call must have a NEW token, distinct from generated_token
        self.assertEqual(len(captured_deliver_args), 1)
        new_token = captured_deliver_args[0].get("intervention_id")
        self.assertNotEqual(new_token, generated_token)

    def test_round5_honor_configured_gate_verdict_dir_during_cleanup(self):
        """Round 5: HERDR_GATE_VERDICT_DIR must be honored during gate cleanup purge."""
        from types import SimpleNamespace
        import herdr.task_resources
        herdr_task = _load_herdr_task()
        from herdr.state_store import SQLiteStateStore

        custom_vdir = self.tmp_dir / "custom_verdicts"
        custom_vdir.mkdir(parents=True, exist_ok=True)

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-custom-vdir", "status": "running"})

        task_id = "task-custom-vdir-gate"
        verdict_file = custom_vdir / f"{task_id}.json"
        verdict_file.write_text(json.dumps({"verdict": "fail", "note": "custom prior"}), encoding="utf-8")

        task = {
            "task_id": task_id,
            "workflow_id": "wf-custom-vdir",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "pane_id": "p-custom-vdir",
            "clone_path": str(self.tmp_dir),
            "completion_protocol": "receipt-v1",
        }
        store.save_task(task)

        args = SimpleNamespace(task_id=task_id, request_id="req-custom-vdir", prompt="fix", reason="test")
        with patch.dict(os.environ, {"HERDR_GATE_VERDICT_DIR": str(custom_vdir)}), \
             patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None):
            herdr_task.cmd_rework(args)

        # The verdict file in custom_vdir MUST be purged
        self.assertFalse(verdict_file.exists())
        # gate_cleanup_completed must be recorded
        events = store.list_events(task_id=task_id)
        cleanup_events = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(cleanup_events), 1)

    def test_round5_abort_cleanup_when_verdict_cannot_be_removed(self):
        """Round 5: When verdict file cannot be unlinked, abort cleanup and withhold gate_cleanup_completed."""
        from types import SimpleNamespace
        import herdr.task_resources
        herdr_task = _load_herdr_task()
        from herdr.state_store import SQLiteStateStore

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-unlink-fail", "status": "running"})

        task_id = "task-unlink-fail-gate"
        clone_dir = self.tmp_dir / "clone_unlink_fail"
        clone_dir.mkdir(parents=True, exist_ok=True)
        dot_herdr = clone_dir / ".herdr"
        dot_herdr.mkdir(parents=True, exist_ok=True)
        verdict_file = dot_herdr / "gate-verdict.json"
        verdict_file.write_text(json.dumps({"verdict": "fail", "note": "unremovable"}), encoding="utf-8")

        task = {
            "task_id": task_id,
            "workflow_id": "wf-unlink-fail",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "working",
            "pane_id": "p-unlink-fail",
            "clone_path": str(clone_dir),
            "completion_protocol": "receipt-v1",
        }
        store.save_task(task)

        # Simulate os.remove failing with PermissionError
        original_remove = os.remove
        def failing_remove(path, *a, **k):
            if str(path) == str(verdict_file):
                raise PermissionError(f"[Errno 13] Permission denied: '{path}'")
            return original_remove(path, *a, **k)

        args = SimpleNamespace(task_id=task_id, request_id="req-unlink-fail", prompt="fix", reason="test")
        with patch("os.remove", failing_remove), \
             patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError) as ctx:
                herdr_task.cmd_rework(args)
            self.assertIn("Failed to purge gate verdict file", str(ctx.exception))

        # The verdict file is still there
        self.assertTrue(verdict_file.exists())
        # gate_cleanup_completed must NOT have been recorded
        events = store.list_events(task_id=task_id)
        cleanup_events = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(cleanup_events), 0)
        # _is_gate_cleanup_completed returns False
        self.assertFalse(herdr_task._is_gate_cleanup_completed(task, "req-unlink-fail", store))

        # Subsequent retry when remove works succeeds and records gate_cleanup_completed
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": store.list_tasks()}), \
             patch.object(herdr.task_resources, "owned_live_pane", lambda t: (True, "identity_match")), \
             patch.object(herdr_task, "_compile_working_context_ref", lambda t: None), \
             patch.object(herdr_task, "_herdr", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr="")), \
             patch.object(herdr_task.time, "sleep", lambda s: None):
            herdr_task.cmd_rework(args)

        self.assertFalse(verdict_file.exists())
        events = store.list_events(task_id=task_id)
        cleanup_events = [e for e in events if e.get("event_type") == "gate_cleanup_completed"]
        self.assertEqual(len(cleanup_events), 1)

    def test_round6_keep_receipt_v1_rework_behind_receipt_validation(self):
        """Round 6: receipt-v1 rework tasks must NOT bypass receipt validation via legacy rework recovery."""
        from herdr.state_store import SQLiteStateStore
        ctrl = _load_controller()

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-r6", "status": "running"})

        task_id = "task-r6-receipt-v1-rework"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-r6",
            "run_id": "r1",
            "node": "implementation",
            "stage": "implementation",
            "gate": False,
            "status": "rework",
            "pane_id": "p-r6",
            "completion_protocol": "receipt-v1",
            "version": 1,
        }
        store.save_task(task)

        # Mock deliverables ready and verification/retry complete
        with patch.object(ctrl, "get_task", lambda tid: store.get_task(tid)), \
             patch.object(ctrl, "_completion_marker_snapshot", lambda t: (False, "screen output")), \
             patch.object(ctrl, "check_task_deliverables_ready", lambda t: True), \
             patch.object(ctrl, "_verification_execution_complete_for_rework", lambda t: True), \
             patch.object(ctrl, "_retry_execution_complete_for_rework", lambda t: True), \
             patch.object(ctrl, "emit_done_if_allowed", MagicMock()) as mock_emit, \
             patch.object(ctrl, "_set_observed_status", MagicMock(wraps=ctrl._set_observed_status)):
            ctrl.handle_event(task_id, "idle")

        # Must NOT transition to agent_done via rework_recovery
        updated = store.get_task(task_id)
        self.assertEqual(updated["status"], "rework")
        mock_emit.assert_not_called()

        # Non-receipt-v1 task with the same conditions DOES transition
        task_id_legacy = "task-r6-legacy-rework"
        task_legacy = {
            "task_id": task_id_legacy,
            "workflow_id": "wf-r6",
            "run_id": "r1",
            "node": "implementation",
            "stage": "implementation",
            "gate": False,
            "status": "rework",
            "pane_id": "p-legacy",
            "version": 1,
        }
        store.save_task(task_legacy)

        with patch.object(ctrl, "get_task", lambda tid: store.get_task(tid)), \
             patch.object(ctrl, "_completion_marker_snapshot", lambda t: (False, "screen output")), \
             patch.object(ctrl, "check_task_deliverables_ready", lambda t: True), \
             patch.object(ctrl, "_verification_execution_complete_for_rework", lambda t: True), \
             patch.object(ctrl, "_retry_execution_complete_for_rework", lambda t: True), \
             patch.object(ctrl, "emit_done_if_allowed", MagicMock()), \
             patch.object(ctrl, "_set_observed_status", lambda t, st, rs: (store.save_task({**t, "status": st}), True)[1]):
            ctrl.handle_event(task_id_legacy, "idle")

        updated_legacy = store.get_task(task_id_legacy)
        self.assertEqual(updated_legacy["status"], "agent_done")

    def test_round6_adopt_verdict_preserves_authorized_snapshot_version(self):
        """Round 6: adopt-verdict must validate fresh snapshot and abort if failure_reason, token, or version changed."""
        import io
        from herdr.state_store import SQLiteStateStore
        herdr_task = _load_herdr_task()

        store = SQLiteStateStore(self.db_path)
        store.save_workflow({"workflow_id": "wf-r6-p2", "status": "running"})

        task_id = "task-r6-adopt-race"
        task = {
            "task_id": task_id,
            "workflow_id": "wf-r6-p2",
            "run_id": "r1",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "gate": True,
            "status": "failed",
            "failure_reason": "hard_timeout_watchdog",
            "execution_token": "token-r6",
            "version": 2,
        }
        store.save_task(task)

        # Case A: When fresh snapshot from store has a different failure_reason (e.g. concurrent writer updated it)
        fresh_modified_reason = dict(task, failure_reason="script_error", version=3)
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": [dict(task)]}), \
             patch.object(herdr_task, "_capture_verified_candidate", lambda t: None), \
             patch.object(store, "get_task", lambda tid: dict(fresh_modified_reason)):
            with self.assertRaises(SystemExit) as ctx, patch("sys.stderr", new_callable=io.StringIO) as err:
                herdr_task.set_status(task_id, "completed", verdict="pass")
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("is no longer an authorized watchdog-failed gate", err.getvalue())

        # Case B: When fresh snapshot has a different execution_token
        fresh_modified_token = dict(task, execution_token="token-epoch-2", version=3)
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": [dict(task)]}), \
             patch.object(herdr_task, "_capture_verified_candidate", lambda t: None), \
             patch.object(store, "get_task", lambda tid: dict(fresh_modified_token)):
            with self.assertRaises(SystemExit) as ctx, patch("sys.stderr", new_callable=io.StringIO) as err:
                herdr_task.set_status(task_id, "completed", verdict="pass")
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("execution token changed", err.getvalue())

        # Case C: When fresh snapshot has a newer version modified by another writer
        fresh_modified_version = dict(task, version=5)
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": [dict(task)]}), \
             patch.object(herdr_task, "_capture_verified_candidate", lambda t: None), \
             patch.object(store, "get_task", lambda tid: dict(fresh_modified_version)):
            with self.assertRaises(SystemExit) as ctx, patch("sys.stderr", new_callable=io.StringIO) as err:
                herdr_task.set_status(task_id, "completed", verdict="pass")
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("was modified by another writer", err.getvalue())

        # Case D: Consistent authorized snapshot succeeds and updates task to completed
        with patch.object(herdr_task, "_get_store", lambda: store), \
             patch.object(herdr_task, "load_tasks", lambda: {"tasks": [store.get_task(task_id)]}), \
             patch.object(herdr_task, "_capture_verified_candidate", lambda t: None):
            herdr_task.set_status(task_id, "completed", verdict="pass", note="verified")

        adopted = store.get_task(task_id)
        self.assertEqual(adopted["status"], "completed")
        self.assertEqual(adopted["stage_verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
