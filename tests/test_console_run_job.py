"""Regression tests for asynchronous Factory Console workflow starts."""

import importlib.machinery
import importlib.util
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_run_job_test",
        importlib.machinery.SourceFileLoader("herdr_console_run_job_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConsoleRunJob(unittest.TestCase):
    def wait_for_terminal(self, module, job_id):
        for _ in range(50):
            job = module.workflow_job_status(job_id)
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        self.fail("workflow job did not finish")

    def test_successful_start_is_reported(self):
        module = load_console()
        with patch.object(module, "run_workflow", return_value="WORKFLOW_ID"):
            job = module.start_workflow_job("/tmp/project", "需求")
            result = self.wait_for_terminal(module, job["job_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "WORKFLOW_ID")

    def test_failed_start_preserves_error(self):
        module = load_console()
        with patch.object(module, "run_workflow", side_effect=RuntimeError("preflight failed")):
            job = module.start_workflow_job("/tmp/project", "需求")
            result = self.wait_for_terminal(module, job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "preflight failed")

    def test_workflow_command_allows_preflight_and_dispatch_time(self):
        module = load_console()
        completed = type("Completed", (), {"returncode": 0, "stdout": "WORKFLOW_ID\n", "stderr": ""})()
        with patch.object(module, "run", return_value=completed) as run_command:
            self.assertEqual(module.run_workflow("/tmp/project", "需求"), "WORKFLOW_ID")
        self.assertEqual(run_command.call_args.args[1], 600)


class TestConsoleDeepPreflight(unittest.TestCase):
    @staticmethod
    def fake_completed():
        return type("Completed", (), {"returncode": 0, "stdout": '{"agents":[]}', "stderr": ""})()

    def test_uses_the_deep_preflight_cli_entrypoint(self):
        module = load_console()
        with patch.object(module, "run", return_value=self.fake_completed()) as run_command:
            result = module.deep_preflight({"project_id": "p1"})
        command = run_command.call_args.args[0]
        self.assertEqual(command[0], str(ROOT / "bin" / "herdr-deep-preflight"))
        self.assertEqual(command[1:], ["--project-id", "p1", "--deep", "--json"])
        self.assertEqual(run_command.call_args.args[1], 320)
        self.assertEqual(result, {"agents": []})

    def test_deep_preflight_supports_single_agent_flag(self):
        module = load_console()
        with patch.object(module, "run", return_value=self.fake_completed()) as run_command:
            result = module.deep_preflight({"project_id": "p1"}, agent="opencode")
        command = run_command.call_args.args[0]
        self.assertEqual(command[0], str(ROOT / "bin" / "herdr-deep-preflight"))
        self.assertEqual(command[1:], ["--project-id", "p1", "--deep", "--json", "--agent", "opencode"])
        self.assertEqual(result, {"agents": []})

    def test_falls_back_to_the_library_script_without_the_cli(self):
        module = load_console()
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "herdr" / "deep_preflight.py"
            lib.parent.mkdir()
            lib.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            module.HERDR_ROOT = Path(tmp)
            with patch.object(module, "run", return_value=self.fake_completed()) as run_command:
                module.deep_preflight({"project_id": "p1"})
            self.assertEqual(run_command.call_args.args[0][0], str(lib))

    def test_missing_install_reports_the_checked_path(self):
        module = load_console()
        with tempfile.TemporaryDirectory() as tmp:
            module.HERDR_ROOT = Path(tmp)
            with self.assertRaises(RuntimeError) as ctx:
                module.deep_preflight({"project_id": "p1"})
        self.assertIn("未安装", str(ctx.exception))


class TestConsoleOpsNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load_console().HTML

    def function_body(self, name):
        match = re.search(
            rf"(?:async )?function {name}\([^)]*\)\{{(.*?)\n\}}",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing JavaScript function: {name}")
        return match.group(1)

    def test_ops_button_is_an_explicit_two_way_toggle(self):
        body = self.function_body("syncOpsUi")
        self.assertIn("state.opsMode?'← 返回工厂':'进入运维驾驶舱'", body)
        self.assertIn("state.opsMode?exitOpsCenter:showOpsCenter", body)

    def test_return_restores_the_factory_view(self):
        body = self.function_body("exitOpsCenter")
        self.assertIn("state.opsMode=false", body)
        self.assertIn("syncOpsUi()", body)
        self.assertIn("await refreshAll()", body)

    def test_ops_mode_hides_factory_only_actions(self):
        body = self.function_body("syncOpsUi")
        self.assertIn("document.querySelectorAll('.factory-action')", body)
        self.assertIn("button.hidden=state.opsMode", body)

    def test_late_ops_response_cannot_overwrite_the_factory_view(self):
        body = self.function_body("loadOpsCenter")
        self.assertIn("if(!state.opsMode)return", body)

    def test_workflow_drilldown_restores_its_project_context(self):
        body = self.function_body("openWorkflowFromOps")
        self.assertIn("await loadWorkflow(id)", body)
        self.assertIn("state.projectId=project.project_id", body)
        self.assertIn("state.spaceId=project.workspace_id", body)
        self.assertIn("await refreshAll()", body)


class TestProductNaming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_console()
        cls.html = cls.module.HTML

    def test_product_name_is_a_first_class_constant(self):
        self.assertEqual(self.module.PRODUCT_NAME, "HAFlow")
        self.assertEqual(self.module.PRODUCT_TAGLINE, "让人和多个 AI Agent 一起把事情做完")

    def test_html_renders_product_name_from_the_constant(self):
        self.assertIn("<title>HAFlow</title>", self.html)
        self.assertIn('<div class="brand">HAFlow</div>', self.html)
        self.assertNotIn("__PRODUCT_NAME__", self.html)
        self.assertNotIn("__PRODUCT_TAGLINE__", self.html)

    def test_html_keeps_user_facing_copy_chinese(self):
        self.assertNotIn("Herdr Factory", self.html)
        self.assertNotIn("Needs Attention", self.html)
        self.assertNotIn("Agent Operations Center", self.html)
        self.assertNotIn("Agent Fleet", self.html)


if __name__ == "__main__":
    unittest.main()
