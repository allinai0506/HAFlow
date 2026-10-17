import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


class TestWorkerRollbackCloneCleanup(unittest.TestCase):
    def test_worker_rolled_back_cleans_incomplete_clone(self):
        """When wait_startup_ready fails and disposition is rolled_back, incomplete clone must be purged."""
        import importlib.machinery
        import importlib.util
        import io
        worker_path = HERDR_ROOT / "services" / "herdr-worker.py"
        spec = importlib.util.spec_from_loader(
            "worker_mod_cleanup_test",
            importlib.machinery.SourceFileLoader("worker_mod_cleanup_test", str(worker_path)),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

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
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_clone_dir:
            tmp_clone = Path(tmp_clone_dir)
            with patch.object(sys, "argv", test_args), \
                 patch.object(mod, "create_clone", return_value=tmp_clone), \
                 patch.object(mod, "create_task_branch", return_value="agent/agy/test"), \
                 patch.object(mod, "capture_head_sha", return_value="sha123"), \
                 patch.object(mod, "_rev_parse_head", return_value="sha123"), \
                 patch.object(mod, "build_baseline_fingerprint", return_value={"tracked": {}, "untracked": {}}), \
                 patch.object(mod, "write_task_context", return_value=({}, {})), \
                 patch.object(mod, "verify_request_preflight", return_value={"preflight_identity": {}}), \
                 patch.object(mod, "create_pane", return_value="p-dynamic-99"), \
                 patch.object(mod, "start_agent", return_value={"name": "test-agent", "agent_session": "s1"}), \
                 patch.object(mod, "wait_startup_ready", return_value={"status": "TRUST_REQUIRED", "interactive_ready": False, "reason": "blocked"}), \
                 patch.object(mod, "rollback_unready_startup", return_value=True), \
                 patch.object(mod, "is_task_active_in_registry", return_value=False), \
                 patch("shutil.rmtree") as mock_rmtree, \
                 patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(RuntimeError):
                    mod.main()
                # Verify that clone cleanup was triggered
                mock_rmtree.assert_called_once_with(tmp_clone, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
