"""Acceptance tests for the console workflow template library and page-based authoring."""

import importlib.machinery
import importlib.util
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent

VALID_YAML = """name: demo-flow
label: 演示流程
version: "1.0"
description: 验收测试用
nodes:
  - id: gather
    label: 采集
    node_type: agent
    purpose: 收集输入
  - id: report
    label: 汇总
    node_type: agent
    depends_on: [gather]
    purpose: 汇总产出
"""


def load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_templates_test",
        importlib.machinery.SourceFileLoader("herdr_console_templates_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTemplateLibraryBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_console()

    def test_templates_summary_lists_bundled_templates(self):
        data = self.module.templates_summary()
        ids = {t["id"] for t in data["templates"]}
        self.assertIn("software-development-v1", ids)
        self.assertIn("bidding", ids)
        builtin = next(t for t in data["templates"] if t["id"] == "software-development-v1")
        self.assertTrue(builtin["is_builtin"])
        self.assertGreaterEqual(builtin["node_count"], 6)

    def test_template_detail_returns_nodes_dependencies_and_yaml(self):
        detail = self.module.template_detail("bidding")
        self.assertTrue(detail["is_builtin"])
        self.assertIn("name: bidding", detail["yaml"])
        ids = [n["id"] for n in detail["nodes"]]
        self.assertEqual(len(ids), 7)
        strategy = next(n for n in detail["nodes"] if n["id"] == "strategy")
        self.assertEqual(sorted(strategy["depends_on"]), ["history_search", "scoring_extract"])

    def test_template_detail_unknown_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.module.template_detail("no-such-template")
        self.assertIn("模板不存在", str(ctx.exception))


class TestTemplateSave(unittest.TestCase):
    def setUp(self):
        self.module = load_console()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.user_dir = Path(tmp.name)
        patcher = patch.object(self.module.herdr_workflow, "USER_TEMPLATES_DIR", self.user_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_template_writes_valid_yaml_to_user_dir(self):
        result = self.module.save_template("demo-flow", VALID_YAML)
        self.assertTrue((self.user_dir / "demo-flow.yaml").exists())
        self.assertEqual(result["node_count"], 2)
        ids = {t["id"] for t in self.module.templates_summary()["templates"]}
        self.assertIn("demo-flow", ids)
        saved = next(t for t in self.module.templates_summary()["templates"] if t["id"] == "demo-flow")
        self.assertFalse(saved["is_builtin"])

    def test_save_template_rejects_circular_dependency(self):
        cyclic = VALID_YAML.replace("depends_on: [gather]", "depends_on: [gather, report]")
        with self.assertRaises(RuntimeError) as ctx:
            self.module.save_template("demo-flow", cyclic)
        self.assertIn("cycle", str(ctx.exception))
        self.assertFalse((self.user_dir / "demo-flow.yaml").exists())

    def test_save_template_rejects_unknown_dependency(self):
        broken = VALID_YAML.replace("depends_on: [gather]", "depends_on: [ghost]")
        with self.assertRaises(RuntimeError):
            self.module.save_template("demo-flow", broken)
        self.assertFalse((self.user_dir / "demo-flow.yaml").exists())

    def test_save_template_rejects_path_traversal_name(self):
        with self.assertRaises(RuntimeError):
            self.module.save_template("../evil", VALID_YAML)
        self.assertFalse((self.user_dir.parent / "evil.yaml").exists())

    def test_save_template_rejects_builtin_override(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.module.save_template("bidding", VALID_YAML)
        self.assertIn("内置模板只读", str(ctx.exception))

    def test_save_template_rejects_missing_nodes(self):
        with self.assertRaises(RuntimeError):
            self.module.save_template("no-nodes", "name: no-nodes\nlabel: 空\n")

    def test_save_template_rejects_mismatched_name_field(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.module.save_template("demo-flow", VALID_YAML.replace("name: demo-flow", "name: other"))
        self.assertIn("不一致", str(ctx.exception))


class TestRunWorkflowTemplate(unittest.TestCase):
    def setUp(self):
        self.module = load_console()

    @staticmethod
    def completed():
        return type("Completed", (), {"returncode": 0, "stdout": "WORKFLOW_ID\n", "stderr": ""})()

    def test_run_workflow_forwards_template_flag(self):
        with patch.object(self.module, "run", return_value=self.completed()) as run_command:
            self.module.run_workflow("/tmp/project", "需求", "auto", "bidding")
        cmd = run_command.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--template") + 1], "bidding")

    def test_run_workflow_defaults_to_software_development(self):
        with patch.object(self.module, "run", return_value=self.completed()) as run_command:
            self.module.run_workflow("/tmp/project", "需求")
        cmd = run_command.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--template") + 1], "software-development-v1")

    def test_start_workflow_job_carries_template_to_run_workflow(self):
        with patch.object(self.module, "run_workflow", return_value="WORKFLOW_ID") as run_workflow:
            job = self.module.start_workflow_job("/tmp/project", "需求", "auto", "bidding")
        for _ in range(50):
            if self.module.workflow_job_status(job["job_id"])["status"] != "running":
                break
            time.sleep(0.01)
        run_workflow.assert_called_once_with("/tmp/project", "需求", "auto", "bidding")


class TestTemplateLibraryUI(unittest.TestCase):
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

    def test_template_library_button_exists_in_factory_actions(self):
        self.assertIn("showTemplateLibrary()", self.html)
        self.assertIn(">模板库</button>", self.html.replace("\n", ""))

    def test_template_library_fetches_and_renders_templates(self):
        body = self.function_body("showTemplateLibrary")
        self.assertIn("fetchTemplates()", body)
        self.assertIn("is_builtin", body)
        self.assertIn("/api/templates", self.function_body("fetchTemplates"))

    def test_template_dag_preview_renders_dependencies(self):
        body = self.function_body("showTemplateDAG")
        self.assertIn("/api/template?id=", body)
        self.assertIn("depends_on", body)

    def test_template_editor_saves_via_post(self):
        body = self.function_body("saveTemplate")
        self.assertIn("'/api/template'", body)
        self.assertIn("name", body)
        self.assertIn("yaml", body)

    def test_new_workflow_modal_has_template_selector(self):
        body = self.function_body("showNewWorkflow")
        self.assertIn("newTemplate", body)

    def test_template_select_defaults_to_software_development(self):
        body = self.function_body("populateTemplateSelect")
        self.assertIn("software-development-v1", body)
        self.assertIn("selected", body)

    def test_submit_new_workflow_sends_template(self):
        body = self.function_body("submitNewWorkflowAsync")
        self.assertIn("newTemplate", body)
        self.assertIn("template:", body)


class TestTermTranslation(unittest.TestCase):
    """用户确认的术语映射：Tab=工作流节点、Pane=智能体工位、Agent=执行者、Task=任务、Workspace=项目空间、Workflow=工作流。"""

    @classmethod
    def setUpClass(cls):
        cls.html = load_console().HTML

    def test_confirmed_terms_are_translated(self):
        for snippet in (
            'data-nav="workbench"',
            ">执行者阵容<",
            ">常驻智能体工位<",
            "执行者舰队",
            ">执行者自检<",
            ">指定执行者<",
            "个工作流节点 · ",
            "个智能体工位",
            "工作流 / 阶段节点<",
            "<span>项目空间</span>",
            "<span>活跃工作流</span>",
            ">启动工作流<",
            "<label>工作流</label>",
        ):
            self.assertIn(snippet, self.html, f"missing translation: {snippet}")

    def test_english_terms_are_gone_from_visible_copy(self):
        content = self.html.replace("让人和多个 AI Agent 一起把事情做完", "")
        for term in ("Agent ", "Task ", "Pane ", "Tab ", "Herdr 空间", "活跃 Workflow"):
            self.assertNotIn(term, content, f"untranslated term remains: {term!r}")


if __name__ == "__main__":
    unittest.main()
