import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import task_resources


class TestWorkerStartupAtomicRollback(unittest.TestCase):
    def test_abort_launch_intent_marks_resources_absent(self):
        """abort_launch_intent must set phase to resources_absent."""
        mock_store = MagicMock()
        intent = {
            "key": "wf-1:test:worker::1",
            "intent_id": "intent-123",
            "phase": "allocating",
        }
        with patch.object(task_resources, "_latest_intent", return_value=intent), \
             patch.object(task_resources, "_launch_event") as mock_event:
            aborted = task_resources.abort_launch_intent(mock_store, intent, reason="startup_failed")
            self.assertIsNotNone(aborted)
            self.assertEqual(aborted["phase"], "resources_absent")
            self.assertEqual(aborted["abort_reason"], "startup_failed")
            mock_event.assert_called_once()

    def test_rollback_unready_startup_closes_dynamic_pane_and_preserves_clone(self):
        """rollback_unready_startup must close dynamic pane and preserve clone for postmortem."""
        import importlib.machinery
        import importlib.util
        worker_path = HERDR_ROOT / "services" / "herdr-worker.py"
        spec = importlib.util.spec_from_loader(
            "worker_mod_test",
            importlib.machinery.SourceFileLoader("worker_mod_test", str(worker_path)),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with patch.object(mod, "run_json") as mock_run_json, \
             patch("shutil.rmtree") as mock_rmtree, \
             patch("herdr.task_resources.workflow_launch_lock"):
            mock_run_json.return_value = {"status": "ok"}
            ok = mod.rollback_unready_startup(
                clone=Path("/tmp/fake-clone"),
                pane_id="p-123",
                pane_source="dynamic",
                identity={"intent_id": "i1"},
            )
            self.assertTrue(ok)
            mock_run_json.assert_called_with(["herdr", "pane", "close", "p-123"], timeout=3)
            mock_rmtree.assert_not_called()
    def test_worker_main_failure_before_interactive_ready_rolls_back(self):
        """When wait_startup_ready fails, worker must not set agent_started and must report disposition=rolled_back."""
        import importlib.machinery
        import importlib.util
        import io
        worker_path = HERDR_ROOT / "services" / "herdr-worker.py"
        spec = importlib.util.spec_from_loader(
            "worker_mod_test2",
            importlib.machinery.SourceFileLoader("worker_mod_test2", str(worker_path)),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Mock argv and external dependencies
        test_args = [
            "herdr-worker.py",
            "--task-id", "task-test-atomic",
            "--source", str(HERDR_ROOT),
            "--agent", "grok",
            "--base-branch", "main",
            "--parent-pane", "p0",
            "--launch-intent-id", "intent-atomic-1",
            "--run-id", "run-atomic-1",
        ]
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_clone_dir:
            tmp_clone = Path(tmp_clone_dir)
            with patch.object(sys, "argv", test_args), \
                 patch.object(mod, "create_clone", return_value=tmp_clone), \
                 patch.object(mod, "create_task_branch", return_value="agent/grok/test"), \
                 patch.object(mod, "capture_head_sha", return_value="sha123"), \
                 patch.object(mod, "_rev_parse_head", return_value="sha123"), \
                 patch.object(mod, "build_baseline_fingerprint", return_value={"tracked": {}, "untracked": {}}), \
                 patch.object(mod, "write_task_context", return_value=({}, {})), \
                 patch.object(mod, "verify_request_preflight", return_value={"preflight_identity": {}}), \
                 patch.object(mod, "create_pane", return_value="p-dynamic-99"), \
                 patch.object(mod, "start_agent", return_value={"name": "test-agent", "agent_session": "s1"}), \
                 patch.object(mod, "wait_startup_ready", return_value={"status": "TRUST_REQUIRED", "interactive_ready": False, "reason": "blocked"}), \
                 patch.object(mod, "rollback_unready_startup", return_value=True) as mock_rollback, \
                 patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                with self.assertRaises(RuntimeError) as ctx:
                    mod.main()
            self.assertIn("TRUST_REQUIRED", str(ctx.exception))
            mock_rollback.assert_called_once()
            stderr_output = mock_stderr.getvalue()
            self.assertIn("HERDR_WORKER_FAILURE=", stderr_output)
            for line in stderr_output.splitlines():
                if line.startswith("HERDR_WORKER_FAILURE="):
                    payload = json.loads(line.split("=", 1)[1])
                    self.assertFalse(payload["agent_started"])
                    self.assertFalse(payload["recovery_required"])
                    self.assertEqual(payload["disposition"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
