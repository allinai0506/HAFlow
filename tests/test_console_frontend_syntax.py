"""Automated tests guarding console HTML/JS template syntax and UI contracts.

Prevents regressions where unescaped Python multi-line string interpolation
or invalid JS syntax crashes the frontend on page load.
"""

import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_frontend_test",
        importlib.machinery.SourceFileLoader("herdr_console_frontend_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConsoleFrontendSyntaxAndContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console()
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")

    def test_html_template_is_raw_string_in_source(self):
        """Guard against Python escape issues: HTML_TEMPLATE must be declared as raw string r''' or r\"\"\"."""
        source = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
        match = re.search(r"HTML_TEMPLATE\s*=\s*(r['\"]{3})", source)
        self.assertIsNotNone(
            match,
            "HTML_TEMPLATE must be declared with a raw string prefix r''' or r\"\"\" "
            "to prevent Python from mutating \\n in JS regexes and split strings.",
        )

    def test_javascript_syntax_clean_in_template(self):
        """Extract inline <script> block and validate with node -c to catch JS syntax crashes."""
        script_match = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL)
        self.assertIsNotNone(script_match, "<script> block not found in HTML_TEMPLATE")
        js_code = script_match.group(1)

        # Locate node executable
        node_bin = shutil.which("node") or shutil.which("node", path="/Users/user/.volta/bin:/usr/local/bin:/opt/homebrew/bin")
        if not node_bin:
            self.skipTest("node executable not found in PATH; skipping JS syntax compilation test")

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js_code)
            temp_path = f.name

        try:
            res = subprocess.run([node_bin, "-c", temp_path], capture_output=True, text=True)
            self.assertEqual(
                res.returncode,
                0,
                f"JavaScript syntax error in HTML_TEMPLATE:\n{res.stderr}",
            )
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_new_workflow_modal_has_task_title_in_correct_order(self):
        """Verify modal form fields contract: 项目 -> 本次任务名称 -> 工作流模板 -> 执行者策略 -> 自然语言需求."""
        self.assertIn('id="newTitle"', self.html)
        self.assertIn("本次任务名称", self.html)
        self.assertIn("autoFillWorkflowTitle()", self.html)

        # Verify ordering of label texts in modal definition
        match_proj = re.search(r"<label[^>]*>项目</label>", self.html)
        match_title = re.search(r"<label[^>]*>本次任务名称</label>", self.html)
        match_tpl = re.search(r"<label[^>]*>工作流模板</label>", self.html)
        match_agent = re.search(r"<label[^>]*>执行者策略</label>", self.html)
        match_req = re.search(r"<label[^>]*>自然语言需求</label>", self.html)
        self.assertIsNotNone(match_proj, "missing <label>项目</label>")
        self.assertIsNotNone(match_title, "missing <label>本次任务名称</label>")
        self.assertIsNotNone(match_tpl, "missing <label>工作流模板</label>")
        self.assertIsNotNone(match_agent, "missing <label>执行者策略</label>")
        self.assertIsNotNone(match_req, "missing <label>自然语言需求</label>")

        p_proj = match_proj.start()
        p_title = match_title.start()
        p_tpl = match_tpl.start()
        p_agent = match_agent.start()
        p_req = match_req.start()

        self.assertTrue(
            -1 < p_proj < p_title < p_tpl < p_agent < p_req,
            f"Modal field order violated: proj={p_proj}, title={p_title}, tpl={p_tpl}, agent={p_agent}, req={p_req}",
        )

    def test_auto_fill_workflow_title_logic_present(self):
        """Ensure autoFillWorkflowTitle function is present and avoids generic headers."""
        self.assertIn("function autoFillWorkflowTitle()", self.html)
        self.assertIn("## 需求", self.html)
        self.assertIn("onblur=\"autoFillWorkflowTitle()\"", self.html)

    def test_modal_and_toast_accessibility_attributes(self):
        """Ensure modal dialog and toast have proper WCAG ARIA attributes."""
        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="true"', self.html)
        self.assertIn('aria-labelledby="modalTitle"', self.html)
        self.assertIn('role="alert"', self.html)

    def test_keyboard_escape_closes_modal(self):
        """Verify Escape key listener is registered to close modal and dropdowns."""
        self.assertIn("Escape", self.html)
        self.assertIn("closeModal()", self.html)

    def test_native_blocking_dialogs_eliminated(self):
        """Ensure blocking native confirm() and prompt() calls are replaced by styled modals."""
        # Find script block
        script_match = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL)
        self.assertIsNotNone(script_match)
        js = script_match.group(1)
        # Should not have naked confirm( or prompt( calls in JS
        self.assertNotIn("confirm(", js)
        self.assertNotIn("prompt(", js)
        self.assertIn("showConfirmModal", js)
        self.assertIn("showPromptModal", js)

    def test_primary_button_styling_and_no_duplicate_plus(self):
        """Ensure primary button uses white text on royal blue and avoids double plus icons."""
        # Check no duplicate plus in buttons
        self.assertNotIn("＋ 新需求", self.html)
        self.assertNotIn("＋ 新建模板", self.html)
        self.assertIn("<span>新需求</span>", self.html)
        # Ensure primary button has white text and not muddy black text
        self.assertNotIn(".btn.primary{background:var(--accent);color:#06111f;", self.html)
        self.assertIn(".btn.primary{background:#2563eb;color:#ffffff;", self.html)

    def test_universal_studio_ui_elements(self):
        """Verify Universal Studio Phase 5 contracts: Attention Hub, Filter tabs, Deep Drawer, and Signoff Chamber."""
        # Attention Hub
        self.assertIn('id="attentionBanner"', self.html)
        self.assertIn('class="attention-banner"', self.html)
        self.assertIn("人机协同态势", self.html)
        self.assertIn("updateAttentionHub()", self.html)

        # Attention Filters
        self.assertIn('id="fDecision"', self.html)
        self.assertIn('id="fAttention"', self.html)
        self.assertIn('id="fActive"', self.html)
        self.assertIn("setTaskFilter", self.html)

        # Deep Physical Drawer
        self.assertIn('id="deepDrawer"', self.html)
        self.assertIn('class="deep-drawer collapsed"', self.html)
        self.assertIn("底层物理现场", self.html)
        self.assertIn("toggleDeepDrawer()", self.html)
        self.assertIn("refreshDeepDrawer()", self.html)

        # Artifact Signoff Chamber
        self.assertIn("成果交付会签室", self.html)
        self.assertIn("openSignoffChamber", self.html)
        self.assertIn("submitSignoffDecision", self.html)
        self.assertIn("成果会签", self.html)

    def test_attention_hub_explains_each_pending_decision(self):
        """The banner must identify real pending gates and explain their decision basis."""
        self.assertIn("function isDecisionTask(t)", self.html)
        self.assertIn("t.stage_verdict==='blocked'||t.status==='blocked'||(t.node_type==='gate'", self.html)
        self.assertIn("t.stage_verdict!=='pass'", self.html)
        self.assertIn("function decisionSummary(t)", self.html)
        self.assertIn("stage_verdict_note", self.html)
        self.assertIn("blocker", self.html)
        self.assertIn("goal", self.html)
        self.assertIn("待决策事项", self.html)
        self.assertIn("查看决策项", self.html)

    def test_workflow_detail_enriches_tasks_with_node_type(self):
        """Decision detection must receive gate metadata absent from raw task records."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"nodes": [{"id": "review", "node_type": "gate"}]}, f)
            workflow_path = f.name
        try:
            with patch.object(self.console, "workflows", return_value={
                "wf-1": {"workflow_id": "wf-1", "project_id": "p-1"},
            }), patch.object(self.console, "project_for_workflow", return_value={
                "workflow_file": workflow_path,
            }), patch.object(self.console, "tasks_for_workflow", return_value=[{
                "task_id": "t-1", "workflow_id": "wf-1", "node": "review",
                "status": "completed",
            }]), patch.object(self.console, "agent_runtime", return_value=None), patch.object(
                self.console.herdr_projection, "detect_workflow_stalls", return_value=None,
            ):
                detail = self.console.workflow_detail("wf-1")
        finally:
            Path(workflow_path).unlink(missing_ok=True)
        self.assertEqual(detail["tasks"][0]["node_type"], "gate")

    def test_task_row_overflow_menu_and_stepper_progress(self):
        """Contract: task list uses compact rows with row-click details, overflow menu, and full width."""
        # Task row click & overflow menu
        self.assertIn("onTaskRowClick", self.html)
        self.assertIn("toggleTaskMenu", self.html)
        self.assertIn("closeAllTaskMenus", self.html)
        self.assertIn("task-dropdown-menu", self.html)
        self.assertIn("task-menu-btn", self.html)
        self.assertIn("···", self.html)
        # Stepper active / next badges
        self.assertIn("stage-next", self.html)
        self.assertIn("stage-badge next", self.html)
        self.assertIn("下一阶段", self.html)
        # Full width layout (no max-width: 1400px)
        self.assertNotIn("max-width: 1400px", self.html)
        self.assertIn("width: 100%", self.html)

    def test_decision_task_excludes_superseded_and_renders_actions(self):
        """Ensure isDecisionTask filters out superseded tasks and CLI command preview functions exist."""
        script_match = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL)
        self.assertIsNotNone(script_match)
        js = script_match.group(1)
        self.assertIn("function isDecisionTask", js)
        self.assertIn("status==='superseded'", js)
        self.assertIn("copyCliCommand", js)
        self.assertIn("executeControllerAction", js)


if __name__ == "__main__":
    unittest.main()
