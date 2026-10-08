#!/usr/bin/env python3
"""tests/test_os_sandbox_boundary.py

Empirical OS-level sandbox boundary verification test suite.
Validates that AI reviewer subprocesses run under strictly enforced OS-level
Seatbelt/container boundaries rather than declarative environment variables.
"""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from herdr.review_benchmark import (
    STRICT_ENV_WHITELIST_KEYS,
    generate_macos_seatbelt_profile,
    probe_macos_seatbelt,
    review_diff,
    run_agent_review,
    verify_os_security_isolation,
)


class TestOSSandboxBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_dir = Path(__file__).resolve().parent.parent
        cls.canary_path = Path.home() / ".haflow_protected_test_credential.txt"
        cls.canary_path.write_text("TEST_CANARY_PROTECTED_SECRET_DO_NOT_EXPOSE", encoding="utf-8")
        os.chmod(cls.canary_path, 0o600)

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.canary_path.exists():
                cls.canary_path.unlink()
        except Exception:
            pass

    def test_01_verify_os_isolation_detects_real_kernel_seatbelt(self):
        """Active probe must verify macOS Seatbelt kernel enforcement or container."""
        if sys.platform == "darwin":
            sandbox_exec = shutil.which("sandbox-exec")
            if sandbox_exec:
                is_isolated, reason, meta = verify_os_security_isolation(self.repo_dir)
                self.assertTrue(is_isolated)
                self.assertEqual(meta.get("isolation_type"), "macos-seatbelt")
                self.assertIn("sandbox_exec", meta)
                self.assertIn("profile", meta)

    def test_02_strict_environment_whitelist(self):
        """Child AI subprocesses must receive ONLY whitelisted environment variables."""
        test_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "USER": "testuser",
            "GITHUB_TOKEN": "secret_gh_write_token",
            "GH_TOKEN": "secret_gh_token",
            "GITHUB_PAT": "secret_gh_pat",
            "SSH_AUTH_SOCK": "/tmp/ssh.sock",
            "AWS_SECRET_ACCESS_KEY": "aws_secret",
            "CUSTOM_SECRET": "top_secret",
        }
        child_env = {k: test_env[k] for k in STRICT_ENV_WHITELIST_KEYS if k in test_env}
        self.assertNotIn("GITHUB_TOKEN", child_env)
        self.assertNotIn("GH_TOKEN", child_env)
        self.assertNotIn("GITHUB_PAT", child_env)
        self.assertNotIn("SSH_AUTH_SOCK", child_env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", child_env)
        self.assertNotIn("CUSTOM_SECRET", child_env)
        self.assertIn("PATH", child_env)
        self.assertIn("HOME", child_env)
        self.assertIn("USER", child_env)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "Requires macOS sandbox-exec")
    def test_03_os_kernel_denies_reading_developer_home_canary(self):
        """macOS Seatbelt must return PermissionError / Operation not permitted for developer home canary."""
        profile = generate_macos_seatbelt_profile(self.repo_dir)
        sandbox_bin = shutil.which("sandbox-exec")
        cmd = [
            sandbox_bin,
            "-p",
            profile,
            sys.executable,
            "-c",
            f"open('{self.canary_path}', 'r').read()",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(
            "PermissionError" in proc.stderr or "Operation not permitted" in proc.stderr,
            f"Expected permission error, got: {proc.stderr}",
        )

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "Requires macOS sandbox-exec")
    def test_04_os_kernel_denies_reading_runner_credentials(self):
        """macOS Seatbelt must return PermissionError for runner credentials file if present."""
        runner_cred = Path.home() / "actions-runner-haflow" / ".credentials"
        if not runner_cred.exists():
            runner_cred = Path("/Users/user/actions-runner-haflow/.credentials")
        if runner_cred.exists():
            profile = generate_macos_seatbelt_profile(self.repo_dir)
            sandbox_bin = shutil.which("sandbox-exec")
            cmd = [
                sandbox_bin,
                "-p",
                profile,
                sys.executable,
                "-c",
                f"open('{runner_cred}', 'r').read()",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(
                "PermissionError" in proc.stderr or "Operation not permitted" in proc.stderr,
                f"Expected permission error, got: {proc.stderr}",
            )

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "Requires macOS sandbox-exec")
    def test_05_os_kernel_denies_writing_to_repository_and_home(self):
        """macOS Seatbelt must deny file write operations inside the repository and developer home."""
        profile = generate_macos_seatbelt_profile(self.repo_dir)
        sandbox_bin = shutil.which("sandbox-exec")
        exploit_file = self.repo_dir / "test_sandbox_exploit_marker.txt"
        cmd = [
            sandbox_bin,
            "-p",
            profile,
            sys.executable,
            "-c",
            f"open('{exploit_file}', 'w').write('pwned')",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(exploit_file.exists())

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sandbox-exec"), "Requires macOS sandbox-exec")
    def test_06_os_kernel_allows_reading_allowed_repo_snapshot(self):
        """macOS Seatbelt must allow reading files in the repository under review."""
        profile = generate_macos_seatbelt_profile(self.repo_dir)
        sandbox_bin = shutil.which("sandbox-exec")
        readme = self.repo_dir / "README.md"
        cmd = [
            sandbox_bin,
            "-p",
            profile,
            sys.executable,
            "-c",
            f"print(open('{readme}', 'r').readline())",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("HAFlow", proc.stdout)

    def test_07_env_vars_cannot_bypass_unisolated_safety_skip(self):
        """Environment variables cannot forge isolation; review_diff skips when OS isolation fails."""
        from unittest.mock import patch

        with patch("herdr.review_benchmark.verify_os_security_isolation", return_value=(False, "No OS sandbox", {})), patch.dict(
            os.environ,
            {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HERDR_SECURE_LLM_RUNNER": "1",
                "HERDR_ALLOW_AGY_SHADOW": "1",
                "HERDR_ALLOW_UNISOLATED_RUNNER": "1",
            },
            clear=True,
        ):
            res = review_diff(self.repo_dir, "diff", reviewer_agent="rule", shadow_mode=True, shadow_agent="agy")
            self.assertEqual(res["shadow_status"], "shadow_skipped")
            self.assertIn("No OS sandbox", res["shadow"]["reason"])


if __name__ == "__main__":
    unittest.main()
