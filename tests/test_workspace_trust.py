import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import workspace_trust


class TestWorkspaceTrust(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_ensure_grok_workspace_trust_creates_and_updates(self):
        """ensure_grok_workspace_trust must write TOML entry idempotently."""
        target_path = self.home / "my_project"
        target_path.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=self.home):
            workspace_trust.ensure_grok_workspace_trust(target_path)
            grok_conf = self.home / ".grok" / "trusted_folders.toml"
            self.assertTrue(grok_conf.exists())
            content = grok_conf.read_text(encoding="utf-8")
            resolved = str(target_path.resolve())
            self.assertIn(f'[folders."{resolved}"]', content)
            self.assertIn("trusted = true", content)

            # Second call must be idempotent (no duplicate sections)
            workspace_trust.ensure_grok_workspace_trust(target_path)
            content2 = grok_conf.read_text(encoding="utf-8")
            self.assertEqual(content.count(f'[folders."{resolved}"]'), 1)

    def test_ensure_claude_workspace_trust_updates_json(self):
        """ensure_claude_workspace_trust must add directory to trustedDirectories."""
        target_path = self.home / "claude_project"
        target_path.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=self.home):
            workspace_trust.ensure_claude_workspace_trust(target_path)
            claude_conf = self.home / ".claude.json"
            self.assertTrue(claude_conf.exists())
            data = json.loads(claude_conf.read_text(encoding="utf-8"))
            resolved = str(target_path.resolve())
            self.assertIn(resolved, data.get("trustedDirectories", []))

            # Idempotent call
            workspace_trust.ensure_claude_workspace_trust(target_path)
            data2 = json.loads(claude_conf.read_text(encoding="utf-8"))
            self.assertEqual(data2.get("trustedDirectories", []).count(resolved), 1)

    def test_ensure_controller_env_trust_preseeds_roots(self):
        """ensure_controller_env_trust must preseed project root and clone root."""
        proj_root = self.home / "repo"
        proj_root.mkdir(parents=True)
        clone_root = self.home / ".herdr-controller" / "clones"
        clone_root.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=self.home):
            workspace_trust.ensure_controller_env_trust(project_root=proj_root, clone_root=clone_root)
            grok_conf = self.home / ".grok" / "trusted_folders.toml"
            self.assertTrue(grok_conf.exists())
            content = grok_conf.read_text(encoding="utf-8")
            self.assertIn(str(proj_root.resolve()), content)
            self.assertIn(str(clone_root.resolve()), content)

    def test_launch_preseed_trusts_planned_clone_for_grok(self):
        """Per-clone pre-seed must register the not-yet-created clone path."""
        clone = self.home / "clones" / "task-1"

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertTrue(workspace_trust.ensure_workspace_trust(str(clone), ["grok"]))
            grok_conf = self.home / ".grok" / "trusted_folders.toml"
            self.assertTrue(grok_conf.exists())
            self.assertIn(str(clone.resolve()), grok_conf.read_text(encoding="utf-8"))

    def test_trust_required_failure_detected(self):
        self.assertTrue(workspace_trust.is_trust_required_failure(
            "Worker startup TRUST_REQUIRED in clone"))
        self.assertFalse(workspace_trust.is_trust_required_failure("Worker failed: timeout"))
        self.assertFalse(workspace_trust.is_trust_required_failure(""))

    def test_merge_trust_failure_unhealthy(self):
        record = {"healthy_agents": ["grok", "codex"], "unhealthy_agents": {"kimi": "UNKNOWN"}}
        unhealthy, healthy = workspace_trust.merge_trust_failure_unhealthy(record, "grok")
        self.assertEqual(unhealthy["grok"], "TRUST_REQUIRED")
        self.assertEqual(unhealthy["kimi"], "UNKNOWN")
        self.assertNotIn("grok", healthy)
        self.assertIn("codex", healthy)

    def test_preseed_target_is_mode_independent(self):
        """Trust is needed whenever an agent starts in a fresh clone,
        regardless of integration mode; agents without a trust gate skip."""
        self.assertEqual(
            workspace_trust.preseed_trust_target("/tmp/clones/t1", "grok"),
            "/tmp/clones/t1",
        )
        self.assertEqual(
            workspace_trust.preseed_trust_target("/tmp/clones/t1", "claude"),
            "/tmp/clones/t1",
        )
        self.assertIsNone(workspace_trust.preseed_trust_target("/tmp/clones/t1", "codex"))
        self.assertIsNone(workspace_trust.preseed_trust_target("", "grok"))
        self.assertIsNone(workspace_trust.preseed_trust_target(None, "grok"))


if __name__ == "__main__":
    unittest.main()
