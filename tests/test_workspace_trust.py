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

    # --- issue #255: agy (Antigravity CLI) trust pre-seeding ---

    def test_agy_has_trust_handler_and_preseed_target(self):
        """agy must be covered by launch pre-seed; otherwise every fresh clone
        blocks on the interactive trust dialog (issue #255)."""
        self.assertIn("agy", workspace_trust._TRUST_HANDLERS)
        self.assertEqual(
            workspace_trust.preseed_trust_target("/tmp/clones/t1", "agy"),
            "/tmp/clones/t1",
        )

    def test_ensure_agy_workspace_trust_creates_and_updates(self):
        """agy handler must add the canonical path to trustedWorkspaces and be idempotent."""
        target_path = self.home / "agy_project"
        target_path.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertTrue(workspace_trust.ensure_agy_workspace_trust(target_path))
            conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
            self.assertTrue(conf.exists())
            data = json.loads(conf.read_text(encoding="utf-8"))
            resolved = workspace_trust._exact_case_path(os.path.realpath(str(target_path)))
            self.assertIn(resolved, data["trustedWorkspaces"])

            # Second call must be idempotent (no duplicate entries).
            self.assertTrue(workspace_trust.ensure_agy_workspace_trust(target_path))
            data2 = json.loads(conf.read_text(encoding="utf-8"))
            self.assertEqual(data2["trustedWorkspaces"].count(resolved), 1)

    def test_ensure_agy_workspace_trust_preserves_other_config_and_entries(self):
        """Write must merge: keep existing trustedWorkspaces and all other top-level keys."""
        target_path = self.home / "agy_project2"
        target_path.mkdir(parents=True)
        conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        conf.parent.mkdir(parents=True)
        existing = {
            "allowNonWorkspaceAccess": True,
            "model": "some-model",
            "permissions": {"allow": ["foo"]},
            "trustedWorkspaces": ["/Users/user/existing-project"],
        }
        conf.write_text(json.dumps(existing), encoding="utf-8")

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertTrue(workspace_trust.ensure_agy_workspace_trust(target_path))
            data = json.loads(conf.read_text(encoding="utf-8"))

        resolved = workspace_trust._exact_case_path(os.path.realpath(str(target_path)))
        self.assertEqual(data["allowNonWorkspaceAccess"], True)
        self.assertEqual(data["model"], "some-model")
        self.assertEqual(data["permissions"], {"allow": ["foo"]})
        self.assertIn("/Users/user/existing-project", data["trustedWorkspaces"])
        self.assertIn(resolved, data["trustedWorkspaces"])

    def test_ensure_agy_workspace_trust_uses_real_disk_casing(self):
        """agy matches trusted paths exactly, case-sensitive; wrong-cased input
        must be canonicalized to the real directory-entry casing."""
        real_dir = self.home / "RealRepo"
        real_dir.mkdir()
        wrong_cased = real_dir.parent / (real_dir.name.lower())
        self.assertNotEqual(str(real_dir), str(wrong_cased))
        # Only meaningful on case-insensitive filesystems; otherwise skip.
        if not wrong_cased.exists():
            self.skipTest("filesystem is case-sensitive")

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertTrue(workspace_trust.ensure_agy_workspace_trust(wrong_cased))
            conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
            data = json.loads(conf.read_text(encoding="utf-8"))

        self.assertEqual(len(data["trustedWorkspaces"]), 1)
        self.assertEqual(
            data["trustedWorkspaces"][0],
            os.path.realpath(str(real_dir)),
        )

    def test_agy_trust_canonicalizes_existing_prefix_for_not_yet_created_clone(self):
        """Launch preseeds planned clone paths that do not exist yet: existing
        prefix segments must be canonicalized, missing leaf segments kept."""
        parent = self.home / "MyClones"
        parent.mkdir()
        planned = parent / "lowercase-clone"

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertTrue(workspace_trust.ensure_agy_workspace_trust(planned))
            conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
            data = json.loads(conf.read_text(encoding="utf-8"))

        expected = os.path.join(
            workspace_trust._exact_case_path(os.path.realpath(str(parent))),
            "lowercase-clone",
        )
        self.assertEqual(data["trustedWorkspaces"], [expected])

    def test_exact_case_path_preserves_unknown_leaf_case(self):
        """Pure helper: wrong-cased existing segments are fixed from disk
        entries; a missing leaf is kept verbatim."""
        real_dir = self.home / "TrueName"
        real_dir.mkdir()
        wrong = real_dir.parent / "truename"
        if not wrong.exists():
            self.skipTest("filesystem is case-sensitive")

        canon = workspace_trust._exact_case_path(os.path.realpath(str(wrong)))
        self.assertEqual(canon, os.path.realpath(str(real_dir)))

        missing_leaf = wrong / "deeper-LeAF"
        canon2 = workspace_trust._exact_case_path(os.path.realpath(str(missing_leaf)))
        self.assertEqual(canon2, os.path.realpath(str(real_dir)) + os.sep + "deeper-LeAF")

    # --- B-1 (cleanroom_review on 52f0369): replace() must not widen file mode ---

    def test_ensure_agy_workspace_trust_preserves_existing_0600_under_umask_022(self):
        """A real user settings.json is 0600; tmp+replace carries the tmp's
        umask-derived 0644, so the first agy dispatch would silently widen it.
        The write must keep the original mode."""
        target_path = self.home / "agy_perm_project"
        target_path.mkdir(parents=True)
        conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        conf.parent.mkdir(parents=True)
        conf.write_text(json.dumps({"model": "keep-me"}), encoding="utf-8")
        os.chmod(conf, 0o600)
        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)

        old_mask = os.umask(0o022)
        try:
            with patch("pathlib.Path.home", return_value=self.home):
                self.assertTrue(workspace_trust.ensure_agy_workspace_trust(target_path))
        finally:
            os.umask(old_mask)

        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)
        data = json.loads(conf.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "keep-me")
        resolved = workspace_trust._exact_case_path(os.path.realpath(str(target_path)))
        self.assertIn(resolved, data["trustedWorkspaces"])

    def test_ensure_agy_workspace_trust_preserves_other_existing_mode(self):
        """Mode preservation is the captured file mode, not a hardcoded 0600."""
        target_path = self.home / "agy_perm_project640"
        target_path.mkdir(parents=True)
        conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        conf.parent.mkdir(parents=True)
        conf.write_text(json.dumps({}), encoding="utf-8")
        os.chmod(conf, 0o640)

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertTrue(workspace_trust.ensure_agy_workspace_trust(target_path))

        self.assertEqual(conf.stat().st_mode & 0o777, 0o640)

    def test_ensure_agy_workspace_trust_first_create_is_owner_only(self):
        """First creation must not inherit the loose umask default (0644):
        settings.json may contain user config, so it is created 0600."""
        target_path = self.home / "agy_first_create"
        target_path.mkdir(parents=True)
        conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertFalse(conf.exists())

        old_mask = os.umask(0o022)
        try:
            with patch("pathlib.Path.home", return_value=self.home):
                self.assertTrue(workspace_trust.ensure_agy_workspace_trust(target_path))
        finally:
            os.umask(old_mask)

        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
