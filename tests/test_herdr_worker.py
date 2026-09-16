"""Regression tests for Herdr Worker agent startup commands."""

import importlib.machinery
import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_worker():
    path = ROOT / "services" / "herdr-worker.py"
    spec = importlib.util.spec_from_loader(
        "herdr_worker_test",
        importlib.machinery.SourceFileLoader("herdr_worker_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestStartAgent(unittest.TestCase):
    def test_agy_start_skips_permission_prompts(self):
        worker = load_worker()
        response = {"result": {"agent": {"agent": "agy"}}}

        with patch.object(worker, "run_json", return_value=response) as run_json:
            agent = worker.start_agent("urgent-fix", "agy", "w1:p2", retries=1)

        self.assertEqual(agent, response["result"]["agent"])
        self.assertEqual(
            run_json.call_args.args[0],
            [
                "herdr",
                "agent",
                "start",
                worker.unique_agent_name("urgent-fix", "w1:p2"),
                "--kind",
                "agy",
                "--pane",
                "w1:p2",
                "--timeout",
                "120000",
                "--",
                "--dangerously-skip-permissions",
            ],
        )

    def test_grok_start_uses_always_approve(self):
        worker = load_worker()
        response = {"result": {"agent": {"agent": "grok"}}}

        with patch.object(worker, "run_json", return_value=response) as run_json:
            agent = worker.start_agent("urgent-fix", "grok", "w1:p2", retries=1)

        self.assertEqual(agent, response["result"]["agent"])
        self.assertEqual(
            run_json.call_args.args[0],
            [
                "herdr",
                "agent",
                "start",
                worker.unique_agent_name("urgent-fix", "w1:p2"),
                "--kind",
                "grok",
                "--pane",
                "w1:p2",
                "--timeout",
                "120000",
                "--",
                "--always-approve",
            ],
        )

    def test_ensure_grok_workspace_trust(self):
        worker = load_worker()
        with tempfile.TemporaryDirectory() as td:
            with patch.object(worker.Path, "home", return_value=Path(td)):
                worker.ensure_grok_workspace_trust("/path/to/myrepo")
                config = Path(td) / ".grok" / "trusted_folders.toml"
                self.assertTrue(config.exists())
                content = config.read_text(encoding="utf-8")
                self.assertIn('[folders."/path/to/myrepo"]', content)
                self.assertIn("trusted = true", content)



class TestCleanSandbox(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source_repo = Path(self.temp_dir) / "source"
        self.source_repo.mkdir()
        # Initialize dummy git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=self.source_repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.source_repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.source_repo, check=True, capture_output=True)
        
        # Initial commit on main
        (self.source_repo / "foo.txt").write_text("main version\n")
        subprocess.run(["git", "add", "foo.txt"], cwd=self.source_repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.source_repo, check=True, capture_output=True)

        # Switch to feature branch and create committed diff
        subprocess.run(["git", "checkout", "-b", "feat/wip"], cwd=self.source_repo, check=True, capture_output=True)
        (self.source_repo / "foo.txt").write_text("feature committed version\n")
        subprocess.run(["git", "commit", "-am", "feature commit"], cwd=self.source_repo, check=True, capture_output=True)

        # Now create an uncommitted dirty modification in working tree
        (self.source_repo / "foo.txt").write_text("uncommitted dirty WIP\n")
        (self.source_repo / "untracked.txt").write_text("untracked dirty file\n")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_task_branch_with_dirty_source(self):
        worker = load_worker()
        clone_root = Path(self.temp_dir) / "clones"
        clone_root.mkdir()
        with patch.object(worker, "CLONE_ROOT", clone_root):
            clone = worker.create_clone(str(self.source_repo), "test-task-1")
            self.assertTrue(clone.exists())
            # Branch switch to base_branch='main' should succeed despite source dirty WIP
            branch = worker.create_task_branch(clone, "test-task-1", "codex", "feat", "main")
            self.assertEqual(branch, "agent/codex/feat-test-task-1")
            
            # Verify source repo is untouched and still dirty
            status = subprocess.run(["git", "status", "--porcelain"], cwd=self.source_repo, capture_output=True, text=True).stdout
            self.assertIn("foo.txt", status)
            self.assertIn("untracked.txt", status)
    def test_checkout_onto_branch_with_dirty_source(self):
        worker = load_worker()
        clone_root = Path(self.temp_dir) / "clones"
        clone_root.mkdir()
        # Set up a remote origin and push feat/wip so onto branch check passes
        origin_repo = Path(self.temp_dir) / "origin"
        subprocess.run(["git", "clone", "--bare", str(self.source_repo), str(origin_repo)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(origin_repo)], cwd=self.source_repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "feat/wip"], cwd=self.source_repo, check=True, capture_output=True)

        with patch.object(worker, "CLONE_ROOT", clone_root):
            clone = worker.create_clone(str(self.source_repo), "test-task-onto")
            branch = worker.checkout_onto_branch(clone, "feat/wip")
            self.assertEqual(branch, "feat/wip")

    def test_stale_unmanaged_clone_healed(self):
        worker = load_worker()
        clone_root = Path(self.temp_dir) / "clones"
        clone_root.mkdir()
        stale_dir = clone_root / "stale-task"
        stale_dir.mkdir()
        (stale_dir / "leftover.txt").write_text("broken")

        with patch.object(worker, "CLONE_ROOT", clone_root):
            # Task 'stale-task' is not in registry, so create_clone should auto-remove and recreate
            with patch.object(worker, "is_task_active_in_registry", return_value=False):
                clone = worker.create_clone(str(self.source_repo), "stale-task")
                self.assertTrue(clone.exists())
                self.assertFalse((clone / "leftover.txt").exists())
                self.assertTrue((clone / ".git").exists())


if __name__ == "__main__":
    unittest.main()


