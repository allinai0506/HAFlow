"""Acceptance and unit tests for project creation and workspace adoption in Factory Console."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from herdr import projects as herdr_projects


class TestProjectCreationLogic(unittest.TestCase):
    def test_create_project_rejects_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises((RuntimeError, ValueError)):
                herdr_projects.create_project(tmpdir)

    def test_create_project_invokes_provision_for_new_git_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            resolved_tmp = herdr_projects.canonical_root(tmpdir)
            # mock detect_git_root to return resolved_tmp
            with patch.object(herdr_projects, "detect_git_root", return_value=resolved_tmp), \
                 patch.object(herdr_projects, "project_by_root", return_value=None), \
                 patch.object(herdr_projects, "provision_project", return_value={"project_id": "test-p1", "workspace_id": "wX"}) as mock_provision:
                result = herdr_projects.create_project(tmpdir, project_name="my-proj", template_name="software-development-v1")
                self.assertEqual(result["project_id"], "test-p1")
                mock_provision.assert_called_once_with(
                    resolved_tmp,
                    template_name="software-development-v1",
                    project_name="my-proj",
                )

    def test_adopt_workspace_rejects_missing_workspace(self):
        with patch.object(herdr_projects, "_workspace_alive", return_value=False):
            with self.assertRaises(RuntimeError):
                herdr_projects.adopt_workspace_as_project("w99", "/tmp/fake")

    def test_adopt_workspace_assembles_tabs_and_registers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws_id = "wTest"
            fake_tabs = [
                {"tab_id": "wTest:t1", "label": "zsh", "workspace_id": ws_id},
            ]
            fake_panes = [
                {"pane_id": "wTest:p1", "tab_id": "wTest:t1", "label": "zsh", "cwd": tmpdir},
            ]
            template = {
                "name": "software-development-v1",
                "nodes": [
                    {"id": "requirements", "label": "2需求分析"},
                    {"id": "plan", "label": "3计划"},
                ],
            }

            def fake_run_json(cmd, *args, **kwargs):
                if cmd[1:3] == ["tab", "list"]:
                    return {"result": {"tabs": fake_tabs}}
                if cmd[1:3] == ["pane", "list"]:
                    return {"result": {"panes": fake_panes}}
                if cmd[1:3] == ["tab", "create"]:
                    # return a created tab
                    new_idx = len(fake_tabs) + 1
                    t_id = f"wTest:t{new_idx}"
                    p_id = f"wTest:p{new_idx}"
                    fake_tabs.append({"tab_id": t_id, "label": cmd[cmd.index("--label") + 1], "workspace_id": ws_id})
                    fake_panes.append({"pane_id": p_id, "tab_id": t_id, "label": "Anchor", "cwd": tmpdir})
                    return {"result": {"tab": {"tab_id": t_id}, "root_pane": {"pane_id": p_id}}}
                return {}

            with patch.object(herdr_projects, "_workspace_alive", return_value=True), \
                 patch.object(herdr_projects, "detect_git_root", return_value=tmpdir), \
                 patch.object(herdr_projects, "detect_base_branch", return_value="main"), \
                 patch.object(herdr_projects, "load_template", return_value=template), \
                 patch.object(herdr_projects, "_run_json", side_effect=fake_run_json), \
                 patch.object(herdr_projects, "_run") as mock_run, \
                 patch.object(herdr_projects, "_start_coordinator") as mock_coord, \
                 patch.object(herdr_projects, "load_projects", return_value={"projects": {}}), \
                 patch.object(herdr_projects, "save_projects") as mock_save:

                record = herdr_projects.adopt_workspace_as_project(ws_id, tmpdir, "software-development-v1")

                self.assertEqual(record["workspace_id"], ws_id)
                self.assertEqual(record["coordinator_pane_id"], "wTest:p1")
                mock_coord.assert_called_once()
                mock_save.assert_called_once()


class TemplateSwitchTest(unittest.TestCase):
    """回归:已注册项目在显式请求不同模板时必须重编节点拓扑。

    背景 wf-nexusarchive-0918-01:控制台选 general-task-v1,
    实际按 software-development-v1 运行(ensure_project 忽略入参模板)。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-template-switch-")
        self.addCleanup(self.tmp.cleanup)
        self.workflow_file = Path(self.tmp.name) / "workflow.json"
        self.workflow_file.write_text(json.dumps({
            "workflow_template": "software-development-v1",
            "coordinator": {"tab_id": "wT:t1", "pane_id": "wT:p1"},
            "nodes": [
                {"id": "requirements", "label": "2需求分析", "tab_id": "wT:t2"},
                {"id": "plan", "label": "3计划", "tab_id": "wT:t3"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        self.record = {
            "project_id": "p-1",
            "project_name": "proj",
            "project_root": self.tmp.name,
            "workspace_id": "wT",
            "coordinator_pane_id": "wT:p1",
            "workflow_file": str(self.workflow_file),
        }

    def _patchers(self, active=None):
        template = {
            "name": "general-task-v1",
            "nodes": [
                {"id": "intake_and_scoping", "label": "1任务理解与范围界定"},
                {"id": "deep_execution", "label": "2专项深度执行"},
                {"id": "review_and_delivery", "label": "3评审验收与交付归档"},
            ],
        }
        created = [
            {"result": {"tab": {"tab_id": "wT:t10"},
                        "root_pane": {"pane_id": "wT:p10"}}},
            {"result": {"tab": {"tab_id": "wT:t11"},
                        "root_pane": {"pane_id": "wT:p11"}}},
            {"result": {"tab": {"tab_id": "wT:t12"},
                        "root_pane": {"pane_id": "wT:p12"}}},
        ]
        run_mock = MagicMock()
        register_mock = MagicMock(return_value={"project_id": "p-1"})
        return [
            patch.object(herdr_projects, "canonical_root",
                         return_value=self.record["project_root"]),
            patch.object(herdr_projects, "project_by_root",
                         return_value=dict(self.record)),
            patch.object(herdr_projects, "_workspace_alive",
                         return_value=True),
            patch.object(herdr_projects, "_pane_alive", return_value=True),
            patch.object(herdr_projects, "_coordinator_alive",
                         return_value=True),
            patch.object(herdr_projects, "active_workflows_for_project",
                         return_value=active or []),
            patch.object(herdr_projects, "load_template",
                         return_value=template),
            patch.object(herdr_projects, "_run_json",
                         side_effect=created),
            patch.object(herdr_projects, "_run", run_mock),
            patch.object(herdr_projects, "_register_project_workflow",
                         register_mock),
        ], run_mock, register_mock

    def test_different_template_reprovisions_nodes_and_closes_old_tabs(self):
        from contextlib import ExitStack

        patchers, run_mock, register_mock = self._patchers()
        with ExitStack() as stack:
            for p in patchers:
                stack.enter_context(p)
            result = herdr_projects.ensure_project(
                self.record["project_root"], template_name="general-task-v1"
            )
        self.assertEqual(result["project_id"], "p-1")
        register_mock.assert_called_once()
        kwargs = register_mock.call_args.kwargs
        self.assertEqual(kwargs["workspace_id"], "wT")
        self.assertEqual(kwargs["coordinator_tab_id"], "wT:t1")
        self.assertEqual(kwargs["coordinator_pane_id"], "wT:p1")
        self.assertEqual(kwargs["template_name"], "general-task-v1")
        nodes = kwargs["runtime_nodes"]
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0]["tab_id"], "wT:t10")
        self.assertEqual(nodes[0]["anchor_pane_id"], "wT:p10")

        closed_tabs = [
            c.args[0][3] for c in run_mock.call_args_list
            if c.args and c.args[0][:3] == ["herdr", "tab", "close"]
        ]
        self.assertEqual(sorted(closed_tabs), ["wT:t2", "wT:t3"])
        self.assertNotIn("wT:t1", closed_tabs)

    def test_none_template_keeps_existing_topology(self):
        from contextlib import ExitStack

        patchers, _run_mock, register_mock = self._patchers()
        with ExitStack() as stack:
            for p in patchers:
                stack.enter_context(p)
            result = herdr_projects.ensure_project(
                self.record["project_root"], template_name=None
            )
        self.assertEqual(result["workflow_file"], str(self.workflow_file))
        register_mock.assert_not_called()

    def test_same_template_no_reprovision(self):
        from contextlib import ExitStack

        patchers, _run_mock, register_mock = self._patchers()
        with ExitStack() as stack:
            for p in patchers:
                stack.enter_context(p)
            herdr_projects.ensure_project(
                self.record["project_root"],
                template_name="software-development-v1",
            )
        register_mock.assert_not_called()

    def test_active_workflow_blocks_template_switch(self):
        from contextlib import ExitStack

        patchers, _run_mock, register_mock = self._patchers(
            active=[{"workflow_id": "wf-active-1", "status": "running"}]
        )
        with ExitStack() as stack:
            for p in patchers:
                stack.enter_context(p)
            with self.assertRaises(RuntimeError) as ctx:
                herdr_projects.ensure_project(
                    self.record["project_root"],
                    template_name="general-task-v1",
                )
        self.assertIn("wf-active-1", str(ctx.exception))
        register_mock.assert_not_called()


class TestConsoleProjectEndpointsAndUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "console" / "herdr_factory_console.py"
        spec = importlib.util.spec_from_file_location("console_mod", str(path))
        cls.console = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.console)

    def test_frontend_has_new_project_modal_and_button(self):
        html = getattr(self.console, "HTML_TEMPLATE", "")
        self.assertIn("showNewProjectModal()", html)
        self.assertIn("新建工厂空间", html)
        self.assertIn("submitNewProject()", html)

    def test_frontend_has_adopt_space_button_for_standalone_terminals(self):
        html = getattr(self.console, "HTML_TEMPLATE", "")
        self.assertIn("submitAdoptSpace", html)
        self.assertIn("按模板装配", html)

    def test_relation_text_for_unregistered_is_friendly(self):
        html = getattr(self.console, "HTML_TEMPLATE", "")
        self.assertIn("独立终端", html)


if __name__ == "__main__":
    unittest.main()
