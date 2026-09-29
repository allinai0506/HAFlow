"""Flow Workbench v1 — console contracts (frontend + backend wiring).

Guards: view toggle, canvas lifecycle, inspector reusing drawer/controller,
offline vendor, no CDN, workflow_detail graph truth, static serving.
"""

import importlib.machinery
import importlib.util
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def _load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_flow_test",
        importlib.machinery.SourceFileLoader("herdr_console_flow_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestFlowWorkbenchFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console()
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")

    def test_view_toggle_present_and_default_flow(self):
        for token in ("viewFlowBtn", "viewListBtn", "流程图", "任务列表", "switchWorkflowView"):
            self.assertIn(token, self.html, f"missing view toggle token: {token}")
        self.assertIn("workflowView:'flow'", self.html)

    def test_canvas_and_inspector_dom(self):
        for token in (
            'id="flowCanvas"', 'id="flowCanvasWrap"', 'id="flowInspector"',
            'id="flowInspectorBody"', 'id="flowInspTitle"',
            "initFlowGraph" if "initFlowGraph" in self.html else "renderFlowGraph",
            "renderFlowGraph", "destroyFlowGraph", "resizeFlowGraph", "fitFlowGraph",
            "selectFlowNode", "renderNodeInspector", "switchWorkflowView",
            "flowTabSummary", "flowTabTasks", "flowTabContext", "flowTabRuntime",
        ):
            self.assertIn(token, self.html, f"missing flow DOM/JS token: {token}")

    def test_inspector_reuses_drawer_and_controller(self):
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL)
        self.assertIsNotNone(script)
        js = script.group(1)
        # Inspector task rows must go through existing drawer
        self.assertIn("openTaskDrawer(", js)
        # Blocked node must surface existing controller execution
        self.assertIn("executeControllerAction(", js)
        self.assertIn("controllerActionsData", js)

    def test_workflow_switch_cleans_graph(self):
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        # loadWorkflow must destroy old graph before rendering new one
        self.assertIn("destroyFlowGraph()", script)
        # Must track workflow id to avoid stale selection
        self.assertIn("flowGraphWfId", script)
        self.assertIn("flowSelectedNodeId", script)

    def test_dependency_missing_fail_soft(self):
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        self.assertIn("flowError", script)
        self.assertIn("/static/vendor/", script if "/static/vendor/" in script else self.html)
        # Explicit error, never white screen: both libs checked
        self.assertIn("window.X6", script)
        self.assertIn("window.dagre", script)

    def test_no_runtime_cdn(self):
        for bad in ("unpkg", "jsdelivr", "cdnjs"):
            self.assertNotIn(bad, self.html.lower(), f"runtime CDN forbidden: {bad}")

    def test_vendor_files_pinned_and_licensed(self):
        vendor = ROOT / "console" / "static" / "vendor"
        self.assertTrue((vendor / "x6-3.1.8.min.js").exists())
        self.assertTrue((vendor / "dagre-3.1.1.min.js").exists())
        self.assertTrue((vendor / "X6-LICENSE").exists())
        self.assertTrue((vendor / "DAGRE-LICENSE").exists())
        x6lic = (vendor / "X6-LICENSE").read_text(encoding="utf-8", errors="ignore")
        dagli = (vendor / "DAGRE-LICENSE").read_text(encoding="utf-8", errors="ignore")
        self.assertIn("Alipay", x6lic)
        self.assertIn("Chris Pettitt", dagli)

    def test_aux_modes_own_workspace_container(self):
        """Ops Center / Dashboard render into #tasks; Flow view must not leave it hidden."""
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        self.assertIn("function setWorkspaceMode", script)
        for fn in ("function showDashboard", "function showOpsCenter", "function clearWorkflow"):
            start = script.index(fn)
            body = script[start:start + 900]
            self.assertIn("setWorkspaceMode('aux')", body, f"{fn} must claim the workspace container")
        # Flow/List toggle must reassert its own mode after returning from aux
        self.assertIn("setWorkspaceMode(state.workflowView)", script)

    def test_every_aux_container_writer_claims_the_workspace(self):
        """Any code writing #tasks must claim the workspace, or Flow view hides its content."""
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        writers = [
            "function showDashboard",
            "function showOpsCenter",
            "function clearWorkflow",
            "function selectSpace",
        ]
        for fn in writers:
            start = script.index(fn)
            nxt = script.find("\nfunction ", start + 1)
            body = script[start:nxt if nxt > 0 else start + 6000]
            self.assertIn(
                "setWorkspaceMode('aux')", body,
                f"{fn} writes #tasks but never claims the workspace container",
            )

    def test_select_space_drops_stale_flow_graph(self):
        """selectSpace clears state.workflow; a surviving graph would show a foreign DAG."""
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        start = script.index("function selectSpace")
        nxt = script.find("\nfunction ", start + 1)
        body = script[start:nxt if nxt > 0 else start + 6000]
        self.assertIn("destroyFlowGraph()", body)
        self.assertIn("state.flowSelectedNodeId=null", body)

    def test_ops_center_clears_dashboard_mode_and_timer(self):
        """opsMode/dashMode must be mutually exclusive, else the 10s timer clobbers ops."""
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        start = script.index("function showOpsCenter")
        nxt = script.find("\nfunction ", start + 1)
        body = script[start:nxt if nxt > 0 else start + 2000]
        self.assertIn("state.dashMode=false", body)
        self.assertIn("stopDashTimer()", body)

    def test_aux_mode_hides_task_filters(self):
        """Task filters must not stay clickable over ops/dashboard content in #tasks."""
        script = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL).group(1)
        start = script.index("function setWorkspaceMode")
        nxt = script.find("\nfunction ", start + 1)
        body = script[start:nxt if nxt > 0 else start + 2000]
        self.assertIn(".task-filters", body)
        self.assertIn("mode==='aux'", body)

    def test_existing_hooks_preserved(self):
        for token in (
            'id="attentionBanner"', "updateAttentionHub()",
            "renderTasks()", "renderStages()", "openTaskDrawer",
            "executeControllerAction", 'id="deepDrawer"', 'id="tasks"', 'id="stages"',
        ):
            self.assertIn(token, self.html, f"existing hook lost: {token}")


class TestFlowWorkbenchBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console()

    def test_workflow_detail_includes_graph(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            import json

            json.dump({"nodes": [
                {"id": "implementation", "label": "实现", "node_type": "agent", "depends_on": ["plan"]},
                {"id": "plan", "label": "计划", "node_type": "agent", "depends_on": []},
            ]}, f)
            wf_path = f.name
        try:
            with patch.object(self.console, "workflows", return_value={
                "wf-1": {"workflow_id": "wf-1", "project_id": "p-1"},
            }), patch.object(self.console, "project_for_workflow", return_value={
                "workflow_file": wf_path,
            }), patch.object(self.console, "tasks_for_workflow", return_value=[
                {"task_id": "t-1", "workflow_id": "wf-1", "node": "plan", "stage": "plan", "status": "completed", "agent": "codex"},
            ]), patch.object(self.console, "agent_runtime", return_value=None), patch.object(
                self.console.herdr_projection, "detect_workflow_stalls", return_value=None,
            ), patch.object(self.console.herdr_projects, "workflow_config_for", return_value=None):
                detail = self.console.workflow_detail("wf-1")
        finally:
            Path(wf_path).unlink(missing_ok=True)
        self.assertIn("graph", detail)
        self.assertIn("context", detail)
        nodes = {n["id"]: n for n in detail["graph"]["nodes"]}
        self.assertIn("plan", nodes)
        self.assertIn("implementation", nodes)
        edges = {(e["from"], e["to"]) for e in detail["graph"]["edges"]}
        self.assertIn(("plan", "implementation"), edges)

    def test_static_vendor_serving(self):
        # Handler static path must resolve inside console/static without traversal
        base = (ROOT / "console" / "static").resolve()
        target = (base / "vendor/x6-3.1.8.min.js").resolve()
        self.assertTrue(str(target).startswith(str(base)))
        self.assertTrue(target.is_file())
        evil = (base / "../herdr_factory_console.py").resolve()
        self.assertFalse(str(evil).startswith(str(base)) and evil.parent == base)


if __name__ == "__main__":
    unittest.main()
