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


def _agy_add_entry_worker(home, path, barrier, result_q):
    """Child-process body: real cross-process flock contention, released together
    by the barrier so both enter the read-modify-write window simultaneously."""
    try:
        os.environ["HOME"] = home
        barrier.wait(timeout=10)
        ok = workspace_trust.ensure_agy_workspace_trust(path)
        result_q.put(("ok" if ok else "fail", str(path)))
    except BaseException as exc:  # noqa: BLE001
        result_q.put(("error", repr(exc)))


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

    # --- Codex review on PR #259: fail-closed preservation + concurrent launches ---

    def _agy_conf_path(self):
        conf = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        conf.parent.mkdir(parents=True, exist_ok=True)
        return conf

    def test_agy_malformed_settings_is_preserved_and_fails_closed(self):
        """A corrupted settings.json must never be silently replaced with an
        empty object: that destroys unrelated user config (model, permissions).
        The handler must leave the original bytes intact and report failure."""
        conf = self._agy_conf_path()
        original = "{ not valid json !!"
        conf.write_text(original, encoding="utf-8")
        os.chmod(conf, 0o600)
        target_path = self.home / "agy_corrupt_project"
        target_path.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertFalse(workspace_trust.ensure_agy_workspace_trust(target_path))

        self.assertEqual(conf.read_text(encoding="utf-8"), original)
        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)

    def test_agy_non_object_settings_is_preserved(self):
        """JSON that parses but is not an object (list/scalar) must be
        preserved, not treated as an empty config."""
        conf = self._agy_conf_path()
        conf.write_text('["legacy-array"]', encoding="utf-8")
        os.chmod(conf, 0o600)

        with patch("pathlib.Path.home", return_value=self.home):
            self.assertFalse(workspace_trust.ensure_agy_workspace_trust(self.home / "p"))

        self.assertEqual(conf.read_text(encoding="utf-8"), '["legacy-array"]')

    def test_agy_unreadable_settings_fails_closed(self):
        """A temporarily unreadable existing file (EACCES) must fail closed
        with the original file untouched, instead of being overwritten."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("chmod 000 does not restrict root")
        conf = self._agy_conf_path()
        original = json.dumps({"model": "keep-me", "trustedWorkspaces": []})
        conf.write_text(original, encoding="utf-8")
        os.chmod(conf, 0o000)
        try:
            with patch("pathlib.Path.home", return_value=self.home):
                self.assertFalse(workspace_trust.ensure_agy_workspace_trust(self.home / "p"))
        finally:
            os.chmod(conf, 0o600)
        self.assertEqual(conf.read_text(encoding="utf-8"), original)

    def test_agy_concurrent_launches_keep_both_entries(self):
        """Two launches racing in one interpreter must not drop entries: the
        read-modify-write is serialized and tmp files are per-write unique.
        Cross-process serialization is covered separately by
        test_agy_concurrent_processes_keep_both_entries."""
        import threading

        conf = self._agy_conf_path()
        conf.write_text(json.dumps({"model": "m", "trustedWorkspaces": []}), encoding="utf-8")
        os.chmod(conf, 0o600)
        proj_a = self.home / "proj_a"
        proj_b = self.home / "proj_b"
        proj_a.mkdir()
        proj_b.mkdir()
        errors = []

        def add(path):
            try:
                if not workspace_trust.ensure_agy_workspace_trust(path):
                    errors.append(path)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        old_mask = os.umask(0o022)
        try:
            # Single patch for the whole test: per-thread patch contexts race,
            # one thread's __exit__ would restore the real HOME mid-run.
            with patch("pathlib.Path.home", return_value=self.home):
                threads = [threading.Thread(target=add, args=(p,)) for p in (proj_a, proj_b)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        finally:
            os.umask(old_mask)

        self.assertEqual(errors, [])
        data = json.loads(conf.read_text(encoding="utf-8"))
        trusted = data["trustedWorkspaces"]
        canon_a = workspace_trust._exact_case_path(os.path.realpath(str(proj_a)))
        canon_b = workspace_trust._exact_case_path(os.path.realpath(str(proj_b)))
        self.assertIn(canon_a, trusted)
        self.assertIn(canon_b, trusted)
        self.assertEqual(data.get("model"), "m")
        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)
        leftovers = [p.name for p in conf.parent.iterdir()
                     if p.name != "settings.json" and not p.name.endswith(".lock")]
        self.assertEqual(leftovers, [])

    def test_agy_concurrent_processes_keep_both_entries(self):
        """Two separate OS processes contending on the settings lock must both
        keep their entries: threads in one interpreter cannot prove the
        cross-process flock boundary that previously lost trust entries."""
        import multiprocessing as mp

        if "fork" not in mp.get_all_start_methods():
            self.skipTest("fork start method unavailable")

        conf = self._agy_conf_path()
        conf.write_text(json.dumps({"model": "m", "trustedWorkspaces": []}), encoding="utf-8")
        os.chmod(conf, 0o600)
        proj_a = self.home / "proj_a"
        proj_b = self.home / "proj_b"
        proj_a.mkdir()
        proj_b.mkdir()

        ctx = mp.get_context("fork")
        barrier = ctx.Barrier(2, timeout=15)
        queue = ctx.Queue()
        processes = [
            ctx.Process(target=_agy_add_entry_worker,
                        args=(str(self.home), str(p), barrier, queue))
            for p in (proj_a, proj_b)
        ]
        old_mask = os.umask(0o022)
        try:
            for proc in processes:
                proc.start()
            for proc in processes:
                proc.join(30)
        finally:
            os.umask(old_mask)

        self.assertFalse([p.exitcode for p in processes if p.exitcode != 0])
        results = [queue.get(timeout=5) for _ in processes]
        self.assertEqual([status for status, _ in results], ["ok", "ok"])
        data = json.loads(conf.read_text(encoding="utf-8"))
        trusted = data["trustedWorkspaces"]
        self.assertIn(workspace_trust._exact_case_path(os.path.realpath(str(proj_a))), trusted)
        self.assertIn(workspace_trust._exact_case_path(os.path.realpath(str(proj_b))), trusted)
        self.assertEqual(data.get("model"), "m")
        self.assertEqual(conf.stat().st_mode & 0o777, 0o600)

    def test_agy_short_write_is_completed_before_replace(self):
        """os.write may accept fewer bytes than requested (e.g. ENOSPC after a
        partial accept); the write must loop until every byte lands, otherwise
        replace() would publish truncated JSON over the user's settings."""
        conf = self._agy_conf_path()
        conf.write_text(json.dumps({"model": "m", "trustedWorkspaces": []}), encoding="utf-8")
        os.chmod(conf, 0o600)
        target_path = self.home / "proj_sw"
        target_path.mkdir()

        real_write = os.write
        chunked = {"calls": 0}

        def halving_write(fd, data):
            chunked["calls"] += 1
            n = max(1, len(data) // 2)
            return real_write(fd, data[:n])

        with patch("pathlib.Path.home", return_value=self.home), \
             patch("os.write", side_effect=halving_write):
            self.assertTrue(workspace_trust.ensure_agy_workspace_trust(target_path))

        self.assertGreater(chunked["calls"], 1)
        data = json.loads(conf.read_text(encoding="utf-8"))
        self.assertEqual(data.get("model"), "m")
        self.assertIn(workspace_trust._exact_case_path(os.path.realpath(str(target_path))),
                      data["trustedWorkspaces"])


if __name__ == "__main__":
    unittest.main()
