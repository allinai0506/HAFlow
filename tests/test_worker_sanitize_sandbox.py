"""Tests for sanitize_clone_sandbox preserving worker launch identity."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import importlib.machinery
import importlib.util

ROOT = Path(__file__).resolve().parent.parent


def load_worker():
    path = ROOT / "services" / "herdr-worker.py"
    spec = importlib.util.spec_from_loader(
        "herdr_worker_sanitize_test",
        importlib.machinery.SourceFileLoader("herdr_worker_sanitize_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSanitizeCloneSandbox(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo = Path(self.temp_dir) / "sandbox"
        self.repo.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True, capture_output=True)
        # CRITICAL: Isolate core.excludesFile so global ignore rules (e.g. ~/.config/git/ignore)
        # cannot mask or prevent git clean -fd from deleting untracked files
        subprocess.run(["git", "config", "core.excludesFile", "/dev/null"], cwd=self.repo, check=True, capture_output=True)

        # Create initial commit
        (self.repo / "tracked.txt").write_text("initial content\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sanitize_preserves_launch_identity_and_cleans_other_untracked(self):
        worker = load_worker()

        # Simulate write_worker_launch_identity(repo, identity, initial=True)
        from herdr.task_resources import write_worker_launch_identity
        identity = {
            "intent_id": "intent-123",
            "task_id": "task-abc",
            "run_id": "run-456",
            "phase": "workspace_created",
        }
        write_worker_launch_identity(self.repo, identity, initial=True)

        # Add dirty WIP to tracked file and create untracked junk file
        (self.repo / "tracked.txt").write_text("dirty uncommitted edit\n")
        (self.repo / "untracked_junk.txt").write_text("temporary junk\n")
        (self.repo / "junk_dir").mkdir()
        (self.repo / "junk_dir" / "nested.txt").write_text("nested junk\n")

        # Confirm before sanitize: identity exists, junk exists, tracked is dirty
        identity_path = self.repo / ".herdr-launch-identity.json"
        self.assertTrue(identity_path.exists())
        self.assertTrue((self.repo / "untracked_junk.txt").exists())
        self.assertTrue((self.repo / "junk_dir" / "nested.txt").exists())
        self.assertEqual((self.repo / "tracked.txt").read_text(), "dirty uncommitted edit\n")

        # The caller supplies this launch's ownership, as Worker.main does.
        worker.sanitize_clone_sandbox(self.repo, launch_identity=identity)

        # Assert:
        # 1. Tracked file is reset to clean HEAD state
        self.assertEqual((self.repo / "tracked.txt").read_text(), "initial content\n")

        # 2. Untracked junk files and directories are purged
        self.assertFalse((self.repo / "untracked_junk.txt").exists())
        self.assertFalse((self.repo / "junk_dir").exists())

        # 3. .herdr-launch-identity.json is preserved!
        self.assertTrue(identity_path.exists(), ".herdr-launch-identity.json must survive sanitize_clone_sandbox")
        preserved_data = json.loads(identity_path.read_text())
        self.assertEqual(preserved_data["intent_id"], "intent-123")

        # 4. Subsequent write_worker_launch_identity (initial=False) succeeds and verifies ownership
        updated_identity = dict(identity, phase="agent_start_requested", pane_id="w1:p1", pane_source="dynamic")
        write_worker_launch_identity(self.repo, updated_identity, initial=False)

        re_read = json.loads(identity_path.read_text())
        self.assertEqual(re_read["phase"], "agent_start_requested")
        self.assertEqual(re_read["pane_id"], "w1:p1")


if __name__ == "__main__":
    unittest.main()
