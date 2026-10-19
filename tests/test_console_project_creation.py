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


class ConsoleWorkflowStagesTest(unittest.TestCase):
    """前端阶段卡片必须跟随 workflow.json 的节点定义(模板切换可见)。

    回归背景 wf-nexusarchive-0918-02:切换 general-task-v1 后前端仍渲染
    software-development 的 6 阶段(workflow_detail 硬编码 STAGES)。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "console" / "herdr_factory_console.py"
        spec = importlib.util.spec_from_file_location("console_mod_stages", str(path))
        cls.console = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.console)

    def _workflow_file(self, nodes):
        tmp = tempfile.TemporaryDirectory(prefix="herdr-console-stages-")
        self.addCleanup(tmp.cleanup)
        wf_file = Path(tmp.name) / "workflow.json"
        wf_file.write_text(json.dumps({
            "workflow_template": "general-task-v1",
            "nodes": nodes,
        }, ensure_ascii=False), encoding="utf-8")
        return str(wf_file)

    def test_stages_follow_template_nodes(self):
        wf_file = self._workflow_file([
            {"id": "intake_and_scoping", "label": "1任务理解与范围界定"},
            {"id": "deep_execution", "label": "2专项深度执行"},
            {"id": "review_and_delivery", "label": "3评审验收与交付归档"},
        ])
        stages = self.console.workflow_stages("wf-1", {"workflow_file": wf_file})
        self.assertEqual(
            stages,
            [
                ("intake_and_scoping", "1任务理解与范围界定"),
                ("deep_execution", "2专项深度执行"),
                ("review_and_delivery", "3评审验收与交付归档"),
            ],
        )

    def test_fallback_to_builtin_stages_without_config(self):
        stages = self.console.workflow_stages("wf-1", {})
        self.assertEqual(stages, self.console.STAGES)
        stages = self.console.workflow_stages(
            "wf-1", {"workflow_file": "/nonexistent/workflow.json"}
        )
        self.assertEqual(stages, self.console.STAGES)

    def test_workflow_detail_uses_template_stages(self):
        wf_file = self._workflow_file([
            {"id": "intake_and_scoping", "label": "1任务理解与范围界定"},
            {"id": "deep_execution", "label": "2专项深度执行"},
            {"id": "review_and_delivery", "label": "3评审验收与交付归档"},
        ])
        with patch.object(self.console.inventory, "workflow_by_id", return_value={
            "workflow_id": "wf-1", "project_id": "p-1",
            "requirement": "REQ",
        }), patch.object(self.console.inventory, "project_for_workflow",
                         return_value={"workflow_file": wf_file}), \
             patch.object(self.console.inventory, "tasks_for_workflow", return_value=[]), \
             patch.object(self.console.runtime, "agent_runtime", return_value=None), \
             patch.object(
                 self.console.herdr_projection,
                 "detect_workflow_stalls", return_value=None,
             ):
            detail = self.console.workflow_detail("wf-1")
        self.assertEqual(
            [s["key"] for s in detail["stages"]],
            ["intake_and_scoping", "deep_execution", "review_and_delivery"],
        )
        self.assertEqual(detail["stages"][0]["label"], "1任务理解与范围界定")

    def test_stages_prefer_frozen_run_definition_over_project_template(self):
        """工作空间换模板后,历史运行的阶段卡片必须仍跟随该次运行的冻结定义。"""
        project_wf_file = self._workflow_file([
            {"id": "requirements", "label": "2需求分析"},
            {"id": "plan", "label": "3计划"},
        ])
        frozen = {"workflow_template": "code-craft-v1", "nodes": [
            {"id": "craft", "label": "1打磨"},
            {"id": "wrapup", "label": "收尾"},
        ]}
        stages = self.console.workflow_stages(
            "wf-1", {"workflow_file": project_wf_file}, w={"config": frozen})
        self.assertEqual(stages, [("craft", "1打磨"), ("wrapup", "收尾")])
        stages = self.console.workflow_stages(
            "wf-1", {"workflow_file": project_wf_file},
            definition={"nodes": [{"id": "craft", "label": "1打磨"}]})
        self.assertEqual(stages, [("craft", "1打磨")])

    def test_workflow_detail_stages_use_frozen_definition_when_project_template_changed(self):
        """项目 reprovision 到新模板后,老运行详情页的阶段必须仍按冻结定义渲染。"""
        project_wf_file = self._workflow_file([
            {"id": "requirements", "label": "2需求分析"},
            {"id": "plan", "label": "3计划"},
        ])
        frozen = {"workflow_template": "code-craft-v1", "nodes": [
            {"id": "craft", "label": "1打磨"},
            {"id": "cleanroom_test", "label": "2净室测试"},
            {"id": "wrapup", "label": "收尾"},
        ]}
        with patch.object(self.console.inventory, "workflow_by_id", return_value={
            "workflow_id": "wf-1", "project_id": "p-1",
            "requirement": "REQ", "config": frozen,
        }), patch.object(self.console.inventory, "project_for_workflow",
                         return_value={"workflow_file": project_wf_file}), \
             patch.object(self.console.inventory, "tasks_for_workflow", return_value=[]), \
             patch.object(self.console.runtime, "agent_runtime", return_value=None), \
             patch.object(
                 self.console.herdr_projection,
                 "detect_workflow_stalls", return_value=None,
             ):
            detail = self.console.workflow_detail("wf-1")
        self.assertEqual(
            [s["key"] for s in detail["stages"]],
            ["craft", "cleanroom_test", "wrapup"],
        )
        self.assertEqual(detail["stages"][0]["label"], "1打磨")

    def test_tasks_for_workflow_reads_state_store_not_json_projection(self):
        import os, tempfile
        from herdr import state_db
        tmp = tempfile.TemporaryDirectory(prefix="herdr-console-tasks-")
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "state.db"
        os.environ["HERDR_STATE_DB"] = str(db)
        self.addCleanup(lambda: os.environ.pop("HERDR_STATE_DB", None))
        tasks = [
            {"task_id": "task-current", "workflow_id": "wf-current", "project_id": "p-current", "agent": "opencode", "status": "blocked"},
            {"task_id": "task-other", "workflow_id": "wf-other", "project_id": "p-current", "agent": "codex", "status": "working"},
        ]
        for t in tasks:
            state_db.save_task(t, db_path=db)
        with patch.object(self.console.common, "load_json", return_value={"tasks": []}):
            actual = self.console.tasks_for_workflow("wf-current")
            loads = self.console.agent_loads("p-current")
        self.assertEqual(actual, [state_db.get_task("task-current", db_path=db)])
        self.assertEqual(loads["opencode"], 1)
        self.assertEqual(loads["codex"], 1)


class ConsoleProjectTemplateExposureTest(unittest.TestCase):
    """项目详情必须携带当前生效模板名,供前端按工作空间初始化模板库/弹窗。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = Path(__file__).resolve().parent.parent / "console" / "herdr_factory_console.py"
        spec = importlib.util.spec_from_file_location("console_mod_project_tpl", str(path))
        cls.console = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.console)

    def _detail(self, record):
        with patch.object(self.console.inventory, "project_by_id", return_value=record), \
             patch.object(self.console.inventory, "workflows_for_project", return_value=[]), \
             patch.object(self.console.runtime, "tabs", return_value=[]), \
             patch.object(self.console.runtime, "panes", return_value=[]), \
             patch.object(self.console.runtime, "slots", return_value=[]), \
             patch.object(self.console.runtime, "preflight", return_value=[]):
            return self.console.project_detail(record["project_id"])

    def test_project_detail_exposes_current_workflow_template(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wf_file = Path(tmpdir) / "workflow.json"
            wf_file.write_text(json.dumps({
                "workflow_template": "code-craft-v1",
                "nodes": [{"id": "craft", "label": "1打磨"}],
            }, ensure_ascii=False), encoding="utf-8")
            detail = self._detail({
                "project_id": "p-1", "workspace_id": "w1",
                "workflow_file": str(wf_file),
            })
        self.assertEqual(detail["project"]["workflow_template"], "code-craft-v1")

    def test_project_detail_without_workflow_file_yields_empty_template(self):
        detail = self._detail({"project_id": "p-1", "workspace_id": "w1"})
        self.assertEqual(detail["project"]["workflow_template"], "")


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


def test_r2_state_store_failure_prevents_project_registration(tmp_path, monkeypatch):
    """Failing SQLite initialization must leave the real registry untouched."""
    import contextlib
    import importlib.util
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from console import herdr_factory_console as console
    from herdr import state_store
    spec = importlib.util.spec_from_file_location('r2_sandbox', Path(__file__).parent / 'support/simple_workbench_sandbox.py')
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    stack = contextlib.ExitStack()
    def fail_store(*args):
        raise RuntimeError('SQLite unavailable')
    monkeypatch.setattr(state_store, 'get_state_store', fail_store)
    sandbox.configure(tmp_path, stack)
    registry = tmp_path / 'projects.json'
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{httpd.server_port}/api/project/create', data=json.dumps({'project_root':str(tmp_path),'template':'business-task-v1'}).encode(), headers={'Content-Type':'application/json','X-HAFlow-Console-Request':'1'})
        try:
            urllib.request.urlopen(request)
            assert False, 'must reject failed initialization'
        except urllib.error.HTTPError as error:
            assert 'SQLite unavailable' in error.read().decode()
        assert not registry.exists()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()
        stack.close()
