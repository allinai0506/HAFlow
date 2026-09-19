"""Regression tests for executor self-check accuracy (deep preflight).

Covers the 2026-09-15 report: "opencode ERROR code=1 / claude TIMEOUT 35s".
- claude cold start measured 36.9s success (occasionally >60s flake), so a
  flat 35s timeout deterministically misreports healthy-but-slow as TIMEOUT.
- Fast startup failures (e.g. 401/402/billing/overloaded phrasing) previously
  fell through to generic ERROR with no actionable classification.
"""

import importlib.util
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def load_deep_preflight():
    spec = importlib.util.spec_from_file_location(
        "herdr_deep_preflight_accuracy",
        str(ROOT / "herdr" / "deep_preflight.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_console_html():
    return (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")


class TestClassifyAccuracy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_deep_preflight()

    def test_billing_and_quota_variants_map_to_token_exhausted(self):
        for text in [
            "Premium request limit reached",
            "402 Payment Required",
            "Out of credits for this billing period",
            "credit balance depleted",
            "Free tier limit exhausted",
        ]:
            self.assertEqual(
                self.m.classify_text(text), "TOKEN_EXHAUSTED", msg=text
            )

    def test_auth_code_variants_map_to_auth_required(self):
        for text in [
            "401 Unauthorized",
            "403 Forbidden",
            "API key expired, please rotate",
            "access denied for this key",
            "Unauthenticated request",
        ]:
            self.assertEqual(
                self.m.classify_text(text), "AUTH_REQUIRED", msg=text
            )

    def test_transient_provider_issues_map_to_provider_error(self):
        for text in [
            "The model is overloaded, try again later",
            "Internal Server Error",
            "503 Service Unavailable",
            "model not found: muse-spark-xyz",
            "connection refused by provider",
        ]:
            self.assertEqual(
                self.m.classify_text(text), "PROVIDER_ERROR", msg=text
            )

    def test_benign_output_stays_unclassified(self):
        self.assertIsNone(self.m.classify_text("HERDR_PREFLIGHT_OK"))
        self.assertIsNone(self.m.classify_text(""))
        # Skill-conflict warnings also appear in SUCCESSFUL qoder runs.
        self.assertIsNone(self.m.classify_text(
            'Skill conflict: "six-step-finish" from user is overriding the same skill from project.'
        ))

    def test_local_infra_failures_map_to_local_error(self):
        for text in [
            "An unexpected critical error occurred:Error: Watcher did not become ready within 5000ms: /Users/user/.qoder-cn/skills",
            "Error: ENOENT: no such file or directory, uv_cwd",
            "EACCES: permission denied, open '/tmp/x.log'",
        ]:
            self.assertEqual(
                self.m.classify_text(text), "LOCAL_ERROR", msg=text
            )

    def test_pi_invalid_key_maps_to_auth_required(self):
        self.assertEqual(
            self.m.classify_text(
                '401: {"message":"Authentication Fails, Your api key: ****4a3d is invalid",'
                '"type":"authentication_error"}'
            ),
            "AUTH_REQUIRED",
        )


class TestSmokeTimeoutAndRetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_deep_preflight()

    def test_claude_gets_longer_timeout_than_default(self):
        self.assertGreater(
            self.m.SMOKE_TIMEOUTS.get("claude", 0),
            self.m.DEFAULT_SMOKE_TIMEOUT,
        )
        # 35s flat timeout misfired on a measured 36.9s healthy run.
        self.assertGreaterEqual(self.m.SMOKE_TIMEOUTS["claude"], 60)

    def test_claude_timeout_is_retried_once_then_reported(self):
        m = self.m

        def fake_run(cmd, timeout=12, cwd=None, stdin=None):
            class R:
                returncode = None
                stdout = ""
                stderr = ""

            raise __import__("subprocess").TimeoutExpired(cmd, timeout)

        with patch.object(m, "choose_smoke_command",
                          return_value=(["claude", "--print", "x"], "claude --print")), \
             patch.object(m, "run", side_effect=fake_run), \
             patch.object(m.time, "sleep", return_value=None):
            # normalize_result receives TimeoutExpired-raised dict path via run();
            # emulate run() returning timeout dict instead for determinism.
            def timeout_run(cmd, timeout=12, cwd=None, stdin=None):
                return {"timeout": True, "stdout": "", "stderr": "",
                        "returncode": None}

            with patch.object(m, "run", side_effect=timeout_run) as run_mock:
                res = m.smoke_probe("claude", "/usr/bin/claude", "/tmp")
        self.assertEqual(res["status"], "TIMEOUT")
        self.assertEqual(run_mock.call_count, 2)
        self.assertIn("重试", res["note"])

    def test_claude_retry_success_marks_ready(self):
        m = self.m
        calls = {"n": 0}

        def flaky_run(cmd, timeout=12, cwd=None, stdin=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"timeout": True, "stdout": "", "stderr": "",
                        "returncode": None}

            class R:
                returncode = 0
                stdout = "HERDR_PREFLIGHT_OK\n"
                stderr = ""

            return R()

        with patch.object(m, "choose_smoke_command",
                          return_value=(["claude", "--print", "x"], "claude --print")), \
             patch.object(m, "run", side_effect=flaky_run), \
             patch.object(m.time, "sleep", return_value=None):
            res = m.smoke_probe("claude", "/usr/bin/claude", "/tmp")
        self.assertEqual(res["status"], "READY")
        self.assertEqual(calls["n"], 2)

    def test_fast_startup_failure_keeps_evidence_output(self):
        m = self.m

        class R:
            returncode = 1
            stdout = ""
            stderr = "402 Payment Required: billing limit reached"

        with patch.object(m, "choose_smoke_command",
                          return_value=(["opencode", "run", "x"], "opencode run")), \
             patch.object(m, "run", return_value=R()):
            res = m.smoke_probe("opencode", "/usr/bin/opencode", "/tmp")
        # Must classify, not generic ERROR, and must keep evidence.
        self.assertEqual(res["status"], "TOKEN_EXHAUSTED")
        self.assertIn("402", res["output"])

    def test_qoder_watcher_failure_maps_to_local_error(self):
        m = self.m

        class R:
            returncode = 1
            stdout = 'Skill conflict: "six-step-finish" overriding.\n'
            stderr = ("An unexpected critical error occurred:"
                      "Error: Watcher did not become ready within 5000ms")

        with patch.object(m, "choose_smoke_command",
                          return_value=(["qodercn", "--print", "x"], "qodercn --print")), \
             patch.object(m, "run", return_value=R()) as run_mock:
            res = m.smoke_probe("qodercli", "/usr/bin/qodercn", "/tmp")
        self.assertEqual(res["status"], "LOCAL_ERROR")
        # Slow local failures stay single-sample (no blind retry).
        self.assertEqual(run_mock.call_count, 1)

    def test_fast_provider_error_is_retried_once(self):
        m = self.m
        calls = {"n": 0}

        class Fail:
            returncode = 1
            stdout = ""
            stderr = "503 Service Unavailable"

        class Ok:
            returncode = 0
            stdout = "HERDR_PREFLIGHT_OK\n"
            stderr = ""

        def flaky(cmd, timeout=12, cwd=None, stdin=None):
            calls["n"] += 1
            return Fail() if calls["n"] == 1 else Ok()

        with patch.object(m, "choose_smoke_command",
                          return_value=(["opencode", "run", "x"], "opencode run")), \
             patch.object(m, "run", side_effect=flaky), \
             patch.object(m.time, "sleep", return_value=None):
            # Fast failures report small elapsed; force via time mock.
            with patch.object(m.time, "time", side_effect=[0.0, 1.2, 0.0, 8.0]):
                res = m.smoke_probe("opencode", "/usr/bin/opencode", "/tmp")
        self.assertEqual(res["status"], "READY")
        self.assertEqual(calls["n"], 2)
        self.assertIn("重试成功", res["note"])

    def test_slow_provider_error_stays_single_sample(self):
        m = self.m

        class Fail:
            returncode = 1
            stdout = ""
            stderr = "overloaded"

        with patch.object(m, "choose_smoke_command",
                          return_value=(["codex", "exec", "x"], "codex exec")), \
             patch.object(m, "run", return_value=Fail()) as run_mock, \
             patch.object(m.time, "sleep", return_value=None):
            with patch.object(m.time, "time", side_effect=[0.0, 30.0]):
                res = m.smoke_probe("codex", "/usr/bin/codex", "/tmp")
        self.assertEqual(res["status"], "PROVIDER_ERROR")
        self.assertEqual(run_mock.call_count, 1)

    def test_pi_adapter_uses_print_no_session(self):
        m = self.m
        help_text = "  --print, -p  Non-interactive mode: process prompt and exit\n"
        with patch.object(m, "help_probe", return_value=help_text):
            cmd, adapter = m.choose_smoke_command("pi", "/opt/homebrew/bin/pi", "/tmp")
        self.assertEqual(
            cmd,
            ["/opt/homebrew/bin/pi", "--print", "--no-session",
             "Reply with exactly HERDR_PREFLIGHT_OK and nothing else."],
        )
        self.assertEqual(adapter, "pi --print")

    def test_grok_adapter_uses_p(self):
        m = self.m
        help_text = "  -p, --single <PROMPT>  Single-turn prompt. Prints the response to stdout and exits\n"
        with patch.object(m, "help_probe", return_value=help_text):
            cmd, adapter = m.choose_smoke_command("grok", "/Users/user/.local/bin/grok", "/tmp")
        self.assertEqual(
            cmd,
            ["/Users/user/.local/bin/grok", "-p",
             "Reply with exactly HERDR_PREFLIGHT_OK and nothing else."],
        )
        self.assertEqual(adapter, "grok -p")

    def test_kimi_adapter_uses_p(self):
        m = self.m
        help_text = "  -p, --prompt <prompt>  Run one prompt non-interactively and print the response.\n"
        with patch.object(m, "help_probe", return_value=help_text):
            cmd, adapter = m.choose_smoke_command("kimi", "/Users/user/.kimi-code/bin/kimi", "/tmp")
        self.assertEqual(
            cmd,
            ["/Users/user/.kimi-code/bin/kimi", "-p",
             "Reply with exactly HERDR_PREFLIGHT_OK and nothing else."],
        )
        self.assertEqual(adapter, "kimi -p")


class TestTargetAgentsFiltering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_deep_preflight()

    def test_inspect_filters_to_target_agent(self):
        project = {"project_id": "p1", "project_root": "/tmp"}
        with patch.object(self.m, "project_pool", return_value={"allowed_agents": ["opencode", "codex", "claude"]}), \
             patch.object(self.m, "resolve_binary", return_value="/bin/true"), \
             patch.object(self.m, "version_probe", return_value=(True, "1.0")):
            rows = self.m.inspect(project, deep=False, target_agents=["opencode"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent"], "opencode")

    def test_inspect_filters_multiple_target_agents(self):
        project = {"project_id": "p1", "project_root": "/tmp"}
        with patch.object(self.m, "project_pool", return_value={"allowed_agents": ["opencode", "codex", "claude"]}), \
             patch.object(self.m, "resolve_binary", return_value="/bin/true"), \
             patch.object(self.m, "version_probe", return_value=(True, "1.0")):
            rows = self.m.inspect(project, deep=False, target_agents=["opencode", "claude"])
        self.assertEqual([r["agent"] for r in rows], ["opencode", "claude"])

    def test_deep_inspect_runs_agent_probes_concurrently(self):
        project = {"project_id": "p1", "project_root": "/tmp"}
        started = threading.Barrier(3)

        def concurrent_probe(agent, binary, cwd):
            started.wait(timeout=1)
            return {"attempted": True, "status": "READY", "adapter": agent}

        with patch.object(self.m, "project_pool", return_value={
            "allowed_agents": ["opencode", "codex", "claude"]
        }), patch.object(self.m, "resolve_binary", return_value="/bin/true"), \
             patch.object(self.m, "version_probe", return_value=(True, "1.0")), \
             patch.object(self.m, "smoke_probe", side_effect=concurrent_probe):
            rows = self.m.inspect(project, deep=True)

        self.assertEqual([row["final_status"] for row in rows], ["READY"] * 3)


class TestConsoleSelfCheckEvidence(unittest.TestCase):
    def test_modal_renders_probe_output_evidence(self):
        html = load_console_html()
        self.assertIn("deep.output", html)
        self.assertIn("PROVIDER_ERROR", html)
        self.assertIn("LOCAL_ERROR", html)
        self.assertIn("重试", html)

    def test_modal_supports_dynamic_single_agent_streaming(self):
        html = load_console_html()
        self.assertIn("executeSinglePreflight", html)
        self.assertIn("updateAgentPreflightResult", html)
        self.assertIn("retrySinglePreflight", html)
        self.assertIn("preflightProgressBar", html)


if __name__ == "__main__":
    unittest.main()
