import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


class TestWorkerRollbackCloneCleanup(unittest.TestCase):
    def _load_worker_mod(self):
        import importlib.machinery
        import importlib.util
        worker_path = HERDR_ROOT / "services" / "herdr-worker.py"
        spec = importlib.util.spec_from_loader(
            "worker_mod_cleanup_test",
            importlib.machinery.SourceFileLoader("worker_mod_cleanup_test", str(worker_path)),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_worker_rolled_back_cleans_incomplete_clone_and_confirms_absent(self):
        """When wait_startup_ready fails and clone deletion succeeds, worker reports recovery_required=False and disposition=rolled_back."""
        import io
        import tempfile
        import shutil
        mod = self._load_worker_mod()

        test_args = [
            "herdr-worker.py",
            "--task-id", "task-test-cleanup",
            "--source", str(HERDR_ROOT),
            "--agent", "agy",
            "--base-branch", "main",
            "--parent-pane", "p0",
            "--launch-intent-id", "intent-cleanup-1",
            "--run-id", "run-cleanup-1",
        ]
        with tempfile.TemporaryDirectory() as tmp_parent:
            tmp_clone = Path(tmp_parent) / "clone_sub"
            tmp_clone.mkdir()
            mock_stderr = io.StringIO()
            with patch.object(sys, "argv", test_args), \
                 patch.object(mod, "create_clone", return_value=tmp_clone), \
                 patch.object(mod, "create_task_branch", return_value="agent/agy/test"), \
                 patch.object(mod, "capture_head_sha", return_value="sha123"), \
                 patch.object(mod, "_rev_parse_head", return_value="sha123"), \
                 patch.object(mod, "build_baseline_fingerprint", return_value={"tracked": {}, "untracked": {}}), \
                 patch.object(mod, "write_task_context", return_value=({}, {})), \
                 patch.object(mod, "verify_request_preflight", return_value={"preflight_identity": {}}), \
                 patch.object(mod, "allocate_task_pane", return_value=("p-dynamic-99", "dynamic")), \
                 patch.object(mod, "start_agent", return_value={"name": "test-agent", "agent_session": "s1"}), \
                 patch.object(mod, "wait_startup_ready", return_value={"status": "TRUST_REQUIRED", "interactive_ready": False, "reason": "blocked"}), \
                 patch.object(mod, "rollback_unready_startup", return_value=True), \
                 patch.object(mod, "is_task_active_in_registry", return_value=False), \
                 patch("sys.stderr", mock_stderr):
                with self.assertRaises(RuntimeError):
                    mod.main()
                # Verify that clone was deleted on disk
                self.assertFalse(tmp_clone.exists())
                # Verify failure payload confirmed rollback
                stderr_text = mock_stderr.getvalue()
                self.assertIn("HERDR_WORKER_FAILURE=", stderr_text)
                for line in stderr_text.splitlines():
                    if line.startswith("HERDR_WORKER_FAILURE="):
                        payload = json.loads(line.split("=", 1)[1])
                        self.assertFalse(payload["recovery_required"])
                        self.assertEqual(payload["disposition"], "rolled_back")

    def test_worker_rolled_back_surviving_clone_retains_recovery_required(self):
        """When clone cannot be deleted, worker retains recovery_required=True and does not emit rolled_back."""
        import io
        import tempfile
        mod = self._load_worker_mod()

        test_args = [
            "herdr-worker.py",
            "--task-id", "task-test-cleanup-fail",
            "--source", str(HERDR_ROOT),
            "--agent", "agy",
            "--base-branch", "main",
            "--parent-pane", "p0",
            "--launch-intent-id", "intent-cleanup-2",
            "--run-id", "run-cleanup-2",
        ]
        with tempfile.TemporaryDirectory() as tmp_parent:
            tmp_clone = Path(tmp_parent) / "clone_surviving"
            tmp_clone.mkdir()
            mock_stderr = io.StringIO()
            with patch.object(sys, "argv", test_args), \
                 patch.object(mod, "create_clone", return_value=tmp_clone), \
                 patch.object(mod, "create_task_branch", return_value="agent/agy/test"), \
                 patch.object(mod, "capture_head_sha", return_value="sha123"), \
                 patch.object(mod, "_rev_parse_head", return_value="sha123"), \
                 patch.object(mod, "build_baseline_fingerprint", return_value={"tracked": {}, "untracked": {}}), \
                 patch.object(mod, "write_task_context", return_value=({}, {})), \
                 patch.object(mod, "verify_request_preflight", return_value={"preflight_identity": {}}), \
                 patch.object(mod, "allocate_task_pane", return_value=("p-dynamic-99", "dynamic")), \
                 patch.object(mod, "start_agent", return_value={"name": "test-agent", "agent_session": "s1"}), \
                 patch.object(mod, "wait_startup_ready", return_value={"status": "TRUST_REQUIRED", "interactive_ready": False, "reason": "blocked"}), \
                 patch.object(mod, "rollback_unready_startup", return_value=True), \
                 patch.object(mod, "is_task_active_in_registry", return_value=False), \
                 patch("shutil.rmtree") as mock_rmtree, \
                 patch("sys.stderr", mock_stderr):
                # mock_rmtree does not delete the directory
                with self.assertRaises(RuntimeError):
                    mod.main()
                self.assertTrue(tmp_clone.exists())
                stderr_text = mock_stderr.getvalue()
                self.assertIn("HERDR_WORKER_FAILURE=", stderr_text)
                for line in stderr_text.splitlines():
                    if line.startswith("HERDR_WORKER_FAILURE="):
                        payload = json.loads(line.split("=", 1)[1])
                        self.assertTrue(payload["recovery_required"])
                        self.assertNotEqual(payload["disposition"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
