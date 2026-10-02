"""Console shell v1 — the approved workbench frame."""

import importlib.machinery
import importlib.util
import unittest
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_shell_test",
        importlib.machinery.SourceFileLoader("herdr_console_shell_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConsoleShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _load_console().HTML

    def test_sidebar_is_product_nav_with_space_switcher(self):
        for token in (
            'id="spaceSwitcher"',
            'id="spaceMenu"',
            'id="projects"',
            "新建工厂空间",
            "toggleSpaceMenu",
            ">空间工作流<",
            ">空间资源与工位<",
            ">项目治理与审计<",
            "toggleSidebar",
            'data-nav="workbench"',
            'data-nav="dashboard"',
            'data-nav="ops"',
            'data-nav="alerts"',
            'data-nav="workflows"',
            'data-nav="agents"',
            'data-nav="slots"',
            "showShellView",
            "openControllerCockpitModal()",
            ">模板库</button>",
            "showArchive()",
        ):
            self.assertIn(token, self.html, token)
        self.assertNotIn(">运转<", self.html)

    def test_topbar_is_breadcrumb_plus_workflow_actions(self):
        for token in (
            'id="crumbSection"',
            'id="workflowSubject"',
            'id="workflowSub"',
            'id="projectTitle"',
            "进入下一阶段",
            ">新需求<",
            'id="moreDropdown"',
        ):
            self.assertIn(token, self.html, token)
        self.assertNotIn(">执行者与任务实时看板<", self.html)

    def test_canvas_toolbar_and_html_nodes(self):
        for token in (
            'id="canvasToolbar"',
            'id="viewFlowBtn"',
            'id="viewListBtn"',
            'id="flowSummary"',
            "fitFlowGraph()",
            "flow-card",
            "Shape.HTML.register",
            "尚未开始",
            "读作",
            "本节点现状",
            "当前任务",
        ):
            self.assertIn(token, self.html, token)

    def test_workbench_restores_replaced_task_list(self):
        start = self.html.find("async function showShellView")
        end = self.html.find("\nfunction paintShellPage")
        body = self.html[start:end]
        self.assertIn("renderTasks()", body)
        self.assertIn("selectSpace(state.space.workspace_id,false)", body)
        self.assertIn("#flowCanvas .flow-node-card", self.html)
        self.assertIn("#flowInspector .flow-node-card", self.html)

    def test_existing_flow_hooks_remain(self):
        for token in (
            'id="flowCanvas"',
            'id="flowInspector"',
            "renderFlowGraph",
            "openTaskDrawer(",
            "executeControllerAction(",
            "dashButton",
        ):
            self.assertIn(token, self.html, token)

    def test_workflow_badge_matches_current_space_rows(self):
        functions = []
        for name in ("paintCrumb", "renderShellWorkflows"):
            match = re.search(
                rf"function {name}\([^)]*\)\{{.*?\n\}}",
                self.html,
                re.DOTALL,
            )
            self.assertIsNotNone(match, name)
            functions.append(match.group(0))
        script = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {textContent: '', innerHTML: '', hidden: false});
    return elements.get(id);
  },
  querySelectorAll() { return []; },
  querySelector() { return {dataset: {}}; }
};
const state = {overview: {active_workflows: 3}, shellView: 'workflows'};
function esc(value) { return String(value); }
function workflowDisplayName(workflow) { return workflow.title; }
""" + "\n".join(functions) + r"""
function verify(workflows, expected) {
  state.project = workflows === null ? null : {workflows};
  paintCrumb();
  renderShellWorkflows();
  const badge = document.getElementById('navWfCount');
  const rows = (document.getElementById('tasks').innerHTML.match(/class="shell-row"/g) || []).length;
  assert.equal(rows, expected);
  assert.equal(badge.textContent, String(rows));
  assert.equal(badge.hidden, expected === 0);
}
verify([{workflow_id: 'wf-current', title: 'Delivered', status: 'completed'}], 1);
verify([
  {workflow_id: 'wf-other-1', title: 'Other 1'},
  {workflow_id: 'wf-other-2', title: 'Other 2'}
], 2);
verify([], 0);
verify(null, 0);
verify([{workflow_id: 'wf-current', title: 'Delivered', status: 'completed'}], 1);
"""
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
