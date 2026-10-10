"""Standard Workspace Layout & Horizontal Workflow Tabs tests.

Validates the native Light Theme incremental iteration contracts:
1. Coaxial 44px Horizontal Baseline across Sidebar (.brand-row 44px) and Main (.top 44px).
2. Middle Horizontal Workflow Tabs (Option A) with dynamic lifecycle (open/close/switch, status dots).
3. 100% Pure Light Theme without dark/black backgrounds.
4. Floating Canvas Toolbar preventing horizontal stair-stepping.
5. Snapping Grid & Spacing Whitelist (0, 4, 8, 12, 16, 20, 24, 32px) compliance.
6. Prototype Snapping Grid principles (P0 Right Axis, P1 Left Axis, P1 Numeric Axis).
7. Proper MIME type for static HTML serving.
"""

import importlib.machinery
import importlib.util
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_standard_layout_test",
        importlib.machinery.SourceFileLoader("herdr_console_standard_layout_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConsoleStandardLayoutAndTabs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console()
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")
        script_match = re.search(r"<script>(.*?)</script>", cls.html, re.DOTALL)
        cls.js = script_match.group(1) if script_match else ""

    def test_coaxial_44px_baseline(self):
        """1. Coaxial Y=44px horizontal baseline across sidebar and main workspace."""
        # Top bar height and border
        self.assertIn("height: 44px;", self.html)
        self.assertIn("border-bottom: 1px solid #e6e8ee;", self.html)

        # Brand row height in sidebar matches top bar exactly
        self.assertIn(".brand-row {\n  height: 44px;", self.html)

    def test_workflow_tabs_bar_markup_and_css(self):
        """2. Workflow Tabs Bar container, scroll area, new-tab button, and tab styles."""
        # Markup tokens
        for token in (
            'id="workflowTabsBar"',
            'class="workflow-tabs-bar"',
            'id="workflowTabsList"',
            'class="tabs-scroll"',
        ):
            self.assertIn(token, self.html, f"Missing Workflow Tabs markup token: {token}")

        # CSS styling
        self.assertIn(".workflow-tabs-bar {", self.html)
        self.assertIn(".tabs-scroll {", self.html)
        self.assertIn(".wf-tab {", self.html)
        self.assertIn(".wf-tab.active {", self.html)
        self.assertIn(".tab-dot {", self.html)
        self.assertIn(".tab-close {", self.html)

    def test_workflow_tabs_dynamic_lifecycle_logic(self):
        """3. Option A: Dynamic tabs lifecycle functions (open, close, render, auto-switch)."""
        self.assertIn("openWorkflowTabIds", self.js)
        self.assertIn("function openWorkflowTab(id)", self.js)
        self.assertIn("function closeWorkflowTab(id,e)", self.js)
        self.assertIn("function renderWorkflowTabs()", self.js)

        # Validate tab logic via node execution
        node_script = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {textContent: '', innerHTML: '', hidden: false, style: {}, classList: {toggle(){}}});
    return elements.get(id);
  },
  querySelector() { return {classList: {toggle(){}}}; },
  querySelectorAll() { return []; },
  body: {classList: {toggle(){}}}
};
function esc(x) { return String(x); }
function workflowSubject(w) { return (w && (w.title || w.requirement_subject)) || ''; }
function saveViewState() {}

""" + f"""
let state = {{
  projectId: 'test-p',
  workflowId: 'wf-1',
  openWorkflowTabIds: [],
  project: {{
    workflows: [
      {{workflow_id: 'wf-1', title: 'Work 1', status: 'working'}},
      {{workflow_id: 'wf-2', title: 'Work 2', status: 'blocked'}},
      {{workflow_id: 'wf-3', title: 'Work 3', status: 'completed'}}
    ]
  }}
}};
let loadedWfId = null;
function loadWorkflow(id) {{
  state.workflowId = id;
  loadedWfId = id;
  if (!state.openWorkflowTabIds.includes(id)) state.openWorkflowTabIds.push(id);
  renderWorkflowTabs();
}}
function clearWorkflow() {{
  state.workflowId = null;
  renderWorkflowTabs();
}}
{re.search(r'function openWorkflowTab\(id\)\{.*?\n\}', self.js, re.DOTALL).group(0)}
{re.search(r'function closeWorkflowTab\(id,e\)\{.*?\n\}', self.js, re.DOTALL).group(0)}
{re.search(r'function renderWorkflowTabs\(\)\{.*?\n\}', self.js, re.DOTALL).group(0)}

// 1. Initial render with active workflow
state.openWorkflowTabIds = ['wf-1', 'wf-2'];
renderWorkflowTabs();
let html = document.getElementById('workflowTabsList').innerHTML;
assert.ok(html.includes('Work 1'));
assert.ok(html.includes('Work 2'));
assert.ok(html.includes('active'));

// 2. Open another workflow tab
openWorkflowTab('wf-3');
assert.equal(loadedWfId, 'wf-3');
assert.ok(state.openWorkflowTabIds.includes('wf-3'));
html = document.getElementById('workflowTabsList').innerHTML;
assert.ok(html.includes('Work 3'));

// 3. Close active tab (wf-3) -> should switch to previous (wf-2)
closeWorkflowTab('wf-3');
assert.equal(state.workflowId, 'wf-2');
assert.ok(!state.openWorkflowTabIds.includes('wf-3'));

// 4. Close all tabs -> clearWorkflow
closeWorkflowTab('wf-2');
closeWorkflowTab('wf-1');
assert.equal(state.workflowId, null);
assert.equal(state.openWorkflowTabIds.length, 0);

// 5. Open logs system tab
openWorkflowTab('__logs__');
assert.equal(state.workflowId, '__logs__');
assert.ok(state.openWorkflowTabIds.includes('__logs__'));
html = document.getElementById('workflowTabsList').innerHTML;
assert.ok(html.includes('调度审计日志'));
assert.ok(html.includes('__logs__'));

// 6. Close logs system tab
closeWorkflowTab('__logs__');
assert.ok(!state.openWorkflowTabIds.includes('__logs__'));
"""
        res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Node tab lifecycle test failed: {res.stderr}")

    def test_pure_light_theme_palette(self):
        """4. 100% Light theme: No dark terminal backgrounds in sidebar, top bar, or main canvas."""
        source = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
        # Ensure dark backgrounds are not used as layout container backgrounds
        self.assertNotIn(".top {\n  background: #14161a", source)
        self.assertNotIn(".sidebar {\n  background: #14161a", source)
        self.assertNotIn(".top-bar {\n  background: #0f172a", source)

    def test_floating_canvas_toolbar_prevents_stair_stepping(self):
        """5. Floating pill toolbar floats over dot grid without introducing extra baseline rows."""
        self.assertIn('.shell[data-workspace="flow"] #canvasToolbar {', self.html)
        self.assertIn("position: absolute; top: 12px; left: 16px;", self.html)

    def test_static_html_mime_type(self):
        """6. Static file serving should correctly identify .html as text/html."""
        source = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
        self.assertIn("text/html; charset=utf-8' if suffix in ('.html', '.htm')", source)

    def test_no_spacing_grid_violations(self):
        """7. Verify strict adherence to 4/8/12/16px spacing rules in console."""
        bad_spacing_regex = re.compile(r'(padding|margin|gap)[^:;}]*:[^;}]*[^0-9.](5|6|7|9|10|11|13|14)px')
        source = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
        matches = []
        for line in source.splitlines():
            m = bad_spacing_regex.search(line)
            if m:
                matches.append(m.group(0))
        self.assertEqual(matches, [], f"Found forbidden spacing values: {matches}")

    def test_prototype_no_spacing_grid_violations(self):
        """8. Verify prototype_standard_layout_tabs.html has 0 spacing grid violations."""
        bad_spacing_regex = re.compile(r'(padding|margin|gap)[^:;}]*:[^;}]*[^0-9.](5|6|7|9|10|11|13|14)px')
        prototype_path = ROOT / "console" / "static" / "prototype_standard_layout_tabs.html"
        self.assertTrue(prototype_path.exists(), "prototype_standard_layout_tabs.html should exist")
        source = prototype_path.read_text(encoding="utf-8")
        matches = []
        for line in source.splitlines():
            m = bad_spacing_regex.search(line)
            if m:
                matches.append(line.strip())
        self.assertEqual(matches, [], f"Found forbidden spacing values in prototype: {matches}")

    def test_prototype_snapping_grid_principles(self):
        """9. Verify prototype satisfies Left Axis, Right Axis, and Numeric Axis."""
        prototype_path = ROOT / "console" / "static" / "prototype_standard_layout_tabs.html"
        source = prototype_path.read_text(encoding="utf-8")

        # 1. P0 Right Axis: decision-actions flex-end & [reject...pass]
        self.assertIn("justify-content: flex-end", source)
        reject_idx = source.find('class="btn-gate reject"')
        pass_idx = source.find('class="btn-gate pass"')
        self.assertNotEqual(reject_idx, -1)
        self.assertNotEqual(pass_idx, -1)
        self.assertTrue(reject_idx < pass_idx, "Secondary button must precede Primary CTA")

        # 2. P1 Left Axis: canonical 16px alignments
        self.assertIn("padding: 0 16px;", source)           # inspector-head-coaxial, light-bottom-bar
        self.assertIn("padding: 12px 16px;", source)        # flow-node-card
        self.assertIn("padding: 16px;", source)             # insp-body

        # 3. P1 Numeric Axis: tabular-nums enabled
        self.assertIn("font-variant-numeric: tabular-nums;", source)

    def test_prototype_coaxial_44px_baseline(self):
        """10. Verify prototype establishes Y=44px coaxial horizontal line across all 4 top zones."""
        prototype_path = ROOT / "console" / "static" / "prototype_standard_layout_tabs.html"
        source = prototype_path.read_text(encoding="utf-8")
        self.assertIn(".rail-head {\n      height: 44px;", source)
        self.assertIn(".sidebar-head {\n      height: 44px;", source)
        self.assertIn(".tabs-bar {\n      height: 44px;", source)
        self.assertIn(".inspector-head {\n      height: 44px;", source)

    def test_six_zone_architecture_in_console(self):
        """11. Verify console HTML and CSS implement the 6-zone architecture from the sketch."""
        # 1. Left Rail (48px, pure light, 32px icons)
        self.assertIn('<aside class="left-rail">', self.html)
        self.assertIn('.left-rail {\n  width: 48px;', self.html)
        self.assertIn('.rail-head {\n  height: 44px;\n  width: 48px;', self.html)
        self.assertIn('.rail-brand {\n  width: 32px;\n  height: 32px;', self.html)
        self.assertIn('.rail-item {\n  width: 32px;\n  height: 32px;', self.html)
        self.assertIn('data-rail-nav="workbench"', self.html)
        self.assertIn('data-rail-nav="dashboard"', self.html)

        # 2. Bottom Bar (28px, pure light, status & deep drawer hook)
        self.assertIn('<footer class="light-bottom-bar" id="consoleBottomBar">', self.html)
        self.assertIn('.light-bottom-bar {\n  height: 28px;', self.html)
        self.assertIn('toggleDeepDrawer()', self.html)
        self.assertIn('Controller 正常调度', self.html)

        # 3. Coaxial 44px Horizontal Baseline across all 4 top zones
        self.assertIn('.rail-head {\n  height: 44px;', self.html)
        self.assertIn('.brand-row {\n  height: 44px;', self.html)
        self.assertIn('.workflow-tabs-bar {\n  height: 44px;', self.html)
        self.assertIn('.flow-insp-head {\n  height: 44px;', self.html)

    def test_left_rail_and_sidecar_decoupled_without_duplication(self):
        """12. Verify Left Rail and Sidecar have decoupled responsibilities without duplication.

        - Left Rail: 48px global module switcher (Workbench, Dashboard, Ops, Alerts, Templates, Avatar).
        - Sidecar: Contextual factory space explorer with 4 distinct zones:
            Zone 1: Space Switcher Pill + Primary CTA (+ 发起新需求)
            Zone 2: Space Workflow Tree (categorized by live production state: 待拍板, 进行中, 历史完成)
            Zone 3: Space Resources & Fleet (执行者机队, 智能体工位)
            Zone 4: Project Governance & Audit (Controller, 模板规范库, 调度审计日志, 任务归档库, 注销项目)
        - Zero Duplication: No legacy duplicate '运转' group in the sidecar.
        - Collapsible: Coaxial 44px toggle buttons (btn-collapse-sidebar, btn-expand-sidebar) and ⌘B shortcut.
        """
        # 1. Left Rail global items
        self.assertIn('id="railNavWorkbench"', self.html)
        self.assertIn('id="railNavDashboard"', self.html)
        self.assertIn('id="railNavOps"', self.html)
        self.assertIn('id="railNavAlerts"', self.html)
        self.assertIn('id="railNavTemplates"', self.html)
        self.assertIn('id="railSpaceAvatar"', self.html)

        # 2. Sidecar 4 functional zones
        # Zone 1
        self.assertIn('class="space-pill"', self.html)
        self.assertIn('class="btn-primary-launch"', self.html)
        self.assertIn("发起新需求", self.html)
        # Zone 2
        self.assertIn('id="sidebarWorkflowTreeSection"', self.html)
        self.assertIn('id="sidebarWorkflowGroups"', self.html)
        self.assertIn("renderSidebarWorkflows", self.js)
        # Zone 3
        self.assertIn("空间资源与工位", self.html)
        self.assertIn('id="sidebarFleetItem"', self.html)
        self.assertIn('id="sidebarPanesItem"', self.html)
        # Zone 4
        self.assertIn("项目治理与审计", self.html)
        self.assertIn('id="navController"', self.html)
        self.assertIn('id="navTemplates"', self.html)
        self.assertIn("模板规范库", self.html)
        self.assertIn('id="sidebarLogsItem"', self.html)
        self.assertIn('id="navArchive"', self.html)

        # 3. Zero duplication assertion
        self.assertNotIn(">运转<", self.html)

        # 4. Collapsible sidebar support
        self.assertIn('id="btnCollapseSidebar"', self.html)
        self.assertIn('id="btnExpandSidebar"', self.html)
        self.assertIn("toggleSidebar", self.js)
        self.assertIn(".shell.sidebar-collapsed", self.html)
        self.assertIn(".sidebar.collapsed", self.html)

    def test_logs_system_tab_not_modal(self):
        """13. Verify 调度审计日志 is rendered as a standard workspace tab, not a popup modal."""
        # 1. logsTabView container exists with controller-panel class
        self.assertIn('id="logsTabView"', self.html)
        self.assertIn('class="controller-panel"', self.html)

        # 2. showLogs opens tab, not modal
        self.assertIn("function showLogs", self.js)
        show_logs_match = re.search(r"async function showLogs\(.*?\)\{.*?\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(show_logs_match)
        show_logs_body = show_logs_match.group(0)
        self.assertNotIn("openModal", show_logs_body)
        self.assertIn("setWorkspaceMode('logs')", show_logs_body)
        self.assertIn("__logs__", show_logs_body)

        # 3. Sidebar logs item invokes showLogs
        self.assertIn('id="sidebarLogsItem" onclick="showLogs()"', self.html)

    def test_sidecar_perf_and_soft_refresh(self):
        """14. Verify sidecar performance optimizations: Promise.all, DOM diffing, system tab protection, and fast service status."""
        # 1. Promise.all parallelization in loadWorkflow
        load_wf_match = re.search(r"async function loadWorkflow\(id\)\{.*?\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(load_wf_match)
        load_wf_body = load_wf_match.group(0)
        self.assertIn("Promise.all([", load_wf_body)
        # 2. Soft refresh: conditional destroyFlowGraph only when workflow changes
        self.assertIn("destroyFlowGraph()", load_wf_body)

        # 3. DOM diffing in renderSidebarWorkflows: data-wf-id and signature cache
        render_sb_match = re.search(r"function renderSidebarWorkflows\(\)\{.*?\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(render_sb_match)
        render_sb_body = render_sb_match.group(0)
        self.assertIn("data-wf-id", render_sb_body)
        self.assertIn("container.dataset.sig = listSig", render_sb_body)
        self.assertNotIn("state.workflowId + '#' + ws.map", render_sb_body)

        # 4. Optimistic UI update in openWorkflowTab
        open_wf_match = re.search(r"function openWorkflowTab\(id\)\{.*?\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(open_wf_match)
        open_wf_body = open_wf_match.group(0)
        self.assertIn("renderSidebarWorkflows()", open_wf_body)

        # 5. System tab protection in loadProject
        load_proj_match = re.search(r"async function loadProject\(id,rer=true\)\{.*?\n\}", self.js, re.DOTALL)
        self.assertIsNotNone(load_proj_match)
        load_proj_body = load_proj_match.group(0)
        self.assertIn("isSysTab", load_proj_body)

        # 6. Backend service_status fast launchctl list
        from console.herdr_factory_console import service_status
        status = service_status()
        self.assertIsInstance(status, dict)
        self.assertIn("com.user.herdr-controller", status)


if __name__ == "__main__":
    unittest.main()
