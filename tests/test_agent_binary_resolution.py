"""Regression tests for agent CLI binary resolution outside LaunchAgent PATH."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import agent_binary
from herdr import preflight as herdr_preflight


def make_exec(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    mode = path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    path.chmod(mode)
    return path


class ResolveBinaryTests(unittest.TestCase):
    def test_path_hit_wins(self):
        with patch.object(agent_binary.shutil, "which", return_value="/from/path/codex") as w:
            self.assertEqual(agent_binary.resolve_binary("codex"), "/from/path/codex")
        w.assert_called_once_with("codex")

    def test_falls_back_to_extra_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            binary = make_exec(Path(td) / "codex")
            with patch.object(agent_binary.shutil, "which", return_value=None), \
                 patch.object(agent_binary, "EXTRA_BIN_DIRS", [Path(td)]), \
                 patch.object(agent_binary, "_find_via_login_shell", return_value=None):
                self.assertEqual(agent_binary.resolve_binary("codex"), str(binary))

    def test_extra_dirs_require_exec_bit(self):
        with tempfile.TemporaryDirectory() as td:
            binary = Path(td) / "codex"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.object(agent_binary.shutil, "which", return_value=None), \
                 patch.object(agent_binary, "EXTRA_BIN_DIRS", [Path(td)]), \
                 patch.object(agent_binary, "_find_via_login_shell", return_value=None):
                self.assertIsNone(agent_binary.resolve_binary("codex"))

    def test_login_shell_is_last_resort(self):
        with patch.object(agent_binary.shutil, "which", return_value=None), \
             patch.object(agent_binary, "EXTRA_BIN_DIRS", []), \
             patch.object(agent_binary, "_find_via_login_shell", return_value="/volta/bin/codex") as z:
            self.assertEqual(agent_binary.resolve_binary("codex"), "/volta/bin/codex")
        z.assert_called_once_with("codex")

    def test_missing_binary_returns_none(self):
        with patch.object(agent_binary.shutil, "which", return_value=None), \
             patch.object(agent_binary, "EXTRA_BIN_DIRS", []), \
             patch.object(agent_binary, "_find_via_login_shell", return_value=None):
            self.assertIsNone(agent_binary.resolve_binary("no-such-bin-xyz"))

    def test_empty_name_returns_none(self):
        self.assertIsNone(agent_binary.resolve_binary(""))

    def test_agent_id_mapping_is_applied(self):
        with patch.object(agent_binary, "resolve_binary", return_value="/x/qodercn") as rb:
            self.assertEqual(agent_binary.resolve_agent_binary("qodercli"), "/x/qodercn")
        rb.assert_called_once_with("qodercn")

    def test_unknown_agent_uses_same_name(self):
        with patch.object(agent_binary, "resolve_binary", return_value="/x/foo") as rb:
            self.assertEqual(agent_binary.resolve_agent_binary("foo"), "/x/foo")
        rb.assert_called_once_with("foo")

    def test_user_bin_dirs_registered(self):
        opencode_bin = agent_binary.HOME / ".opencode" / "bin"
        self.assertIn(opencode_bin, agent_binary.USER_BIN_DIRS)
        self.assertIn(opencode_bin, agent_binary.EXTRA_BIN_DIRS)
        # 用户主目录应优先排在系统目录前面
        opencode_idx = agent_binary.EXTRA_BIN_DIRS.index(opencode_bin)
        homebrew_idx = agent_binary.EXTRA_BIN_DIRS.index(Path("/opt/homebrew/bin"))
        self.assertLess(opencode_idx, homebrew_idx)

    def test_ensure_user_bin_dirs_prepends_path(self):
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            fake_user_dir = Path(td1)
            fake_sys_dir = Path(td2)
            orig_env = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = str(fake_sys_dir)
                with patch.object(agent_binary, "USER_BIN_DIRS", [fake_user_dir]):
                    agent_binary.ensure_user_bin_dirs()
                    parts = os.environ["PATH"].split(os.pathsep)
                    self.assertEqual(parts[0], str(fake_user_dir))
                    self.assertIn(str(fake_sys_dir), parts)
            finally:
                os.environ["PATH"] = orig_env


class PreflightInspectTests(unittest.TestCase):
    def test_inspect_uses_shared_resolver(self):
        with patch.object(herdr_preflight, "project_pool", return_value={}), \
             patch.object(herdr_preflight, "auth_hint", return_value=("present", [])), \
             patch.object(herdr_preflight, "probe_version", return_value=(True, "1.0")), \
             patch.object(herdr_preflight, "resolve_agent_binary",
                          side_effect=lambda a: "/bin/fake-" + a if a == "codex" else None):
            rows = {r["agent"]: r for r in herdr_preflight.inspect(None)}
        self.assertEqual(rows["codex"]["status"], "READY")
        self.assertEqual(rows["codex"]["binary"], "/bin/fake-codex")
        self.assertEqual(rows["agy"]["status"], "MISSING")
        self.assertIsNone(rows["agy"]["binary"])


if __name__ == "__main__":
    unittest.main()
