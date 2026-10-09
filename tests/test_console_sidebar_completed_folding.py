"""Unit and frontend behavior tests for sidebar completed workflow list folding."""

import importlib.machinery
import importlib.util
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_sidebar_folding_test",
        importlib.machinery.SourceFileLoader("herdr_console_sidebar_folding_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSidebarCompletedFolding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console()
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")

    def test_sidebar_completed_folding_script_logic(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")

        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        start = script.index("function renderSidebarWorkflows(){")
        end = script.index("function openWorkflowTab(", start)

        harness = r"""
const container = {dataset: {}, innerHTML: '', querySelectorAll: () => []};
const document = {
  getElementById: id => id === 'sidebarWorkflowGroups' ? container : {textContent: ''}
};
const workflowSubject = w => w.title || w.requirement_subject || w.workflow_id;
const esc = value => String(value);

// Create 12 completed workflows
const workflows = [];
for (let i = 1; i <= 12; i++) {
  workflows.push({
    workflow_id: 'wf-comp-' + i,
    title: '任务 ' + i,
    status: 'completed'
  });
}

let state = {
  workflowId: 'wf-comp-1',
  completedExpanded: false,
  completedVisibleLimit: 5,
  project: { workflows: workflows }
};
""" + script[start:end] + r"""

// 1. Initial render: collapsed by default, displays only 5 items plus toggle button
renderSidebarWorkflows();
if (!container.innerHTML.includes('展开更多 (剩余 7 项)')) {
  console.error("Expected toggle button with remaining count, got:\n" + container.innerHTML);
  process.exit(1);
}
// Should only show task 1 to 5, not task 6
if (!container.innerHTML.includes('任务 1') || !container.innerHTML.includes('任务 5')) process.exit(2);
if (container.innerHTML.includes('任务 6')) process.exit(3);

// 2. Expand: displays all 12 items plus collapse button
toggleCompletedExpanded();
if (!state.completedExpanded) process.exit(4);
if (!container.innerHTML.includes('收起历史任务')) process.exit(5);
if (!container.innerHTML.includes('任务 6') || !container.innerHTML.includes('任务 12')) process.exit(6);

// 3. Fold back
toggleCompletedExpanded();
if (state.completedExpanded) process.exit(7);
if (!container.innerHTML.includes('展开更多')) process.exit(8);
if (container.innerHTML.includes('任务 6')) process.exit(9);

// 4. If active workflow is beyond visible limit, it must still be rendered so active tab is visible
state.workflowId = 'wf-comp-10';
container.dataset.sig = '';
renderSidebarWorkflows();
if (!container.innerHTML.includes('wf-comp-10')) {
  console.error("Active workflow outside limit should still be visible!");
  process.exit(10);
}
"""
        result = subprocess.run([node, "-e", harness], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or "sidebar folding test failed")

    def test_sidebar_completed_css_classes_present(self):
        self.assertIn(".sidebar-subgroup-title.collapsible", self.html)
        self.assertIn(".sidebar-completed-toggle", self.html)
        self.assertIn(".sidebar-subgroup-arrow", self.html)


if __name__ == "__main__":
    unittest.main()
