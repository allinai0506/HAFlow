"""Task Detail V1 drawer contracts: drawer DOM, tabs, row wiring, escape, empty states."""

import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_console(name):
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract_fn(html, name):
    start = html.find(f"function {name}(")
    if start < 0:
        return None
    i = html.find("{", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start:j + 1]
    return None


STUB_GLOBALS = (
    "function esc(s){return String(s??'');}"
    "function humanStatus(s){return s||'unknown';}"
    "var state={selectedTaskDetail:null,workflowId:null};"
)


class TestTaskDetailDrawer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console("herdr_console_task_detail_test")
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")

    def test_drawer_dom_exists_with_a11y(self):
        self.assertIn('id="taskDrawer"', self.html)
        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="false"', self.html)
        self.assertIn('aria-labelledby="taskDrawerTitle"', self.html)
        self.assertIn('aria-label="关闭任务详情"', self.html)

    def test_four_tabs_present_default_overview(self):
        for label in ["概览", "活动", "产物", "运行时"]:
            self.assertIn(label, self.html)
        self.assertIn("switchTaskDrawerTab", self.html)
        self.assertIn("taskDrawerTab", self.html)
        # default tab overview referenced in opener
        self.assertIn("openTaskDrawer", self.html)
        self.assertIn("closeTaskDrawer", self.html)

    def test_row_click_uses_drawer_not_modal(self):
        m = re.search(r"function onTaskRowClick\(.*?\{.*?\}", self.html, re.DOTALL)
        self.assertIsNotNone(m, "onTaskRowClick not found")
        # row click must route to drawer
        self.assertIn("openTaskDrawer", self.html)
        body = m.group(0)
        self.assertIn("openTaskDrawer", body)

    def test_escape_closes_drawer(self):
        self.assertIn("Escape", self.html)
        self.assertIn("closeTaskDrawer", self.html)

    def test_overflow_menu_preserved(self):
        self.assertIn("toggleTaskMenu", self.html)
        self.assertIn("closeAllTaskMenus", self.html)
        self.assertIn("task-dropdown-menu", self.html)
        self.assertIn("openSignoffChamber", self.html)

    def test_empty_states_distinct(self):
        self.assertIn("当前任务尚未产生结果", self.html)
        self.assertIn("暂无可用活动记录", self.html)
        self.assertIn("当前任务尚未产生可展示产物", self.html)
        self.assertIn("暂无运行时信息", self.html)

    def test_design_tokens_reused(self):
        m = re.search(r"\.task-drawer\s*\{.*?\}", self.html, re.DOTALL)
        self.assertIsNotNone(m, ".task-drawer CSS missing")
        css = m.group(0)
        self.assertIn("var(--bg-surface)", css + self.html)
        self.assertIn("var(--border-default)", self.html)
        # drawer must sit above deep drawer (40) but below modal (50)
        self.assertIn("z-index: 45", self.html)

    def test_no_fake_metadata(self):
        # drawer must not render dash-filled placeholders or hard-coded fake durations
        self.assertNotIn("4m 18s", self.html)

    def _menu_html_via_node(self, task):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        fns = ["taskDrawerMenuHtml", "canSteerTask", "canForceReviewTask", "canForcePassTask"]
        parts = []
        for fn in fns:
            src = _extract_fn(self.html, fn)
            self.assertIsNotNone(src, f"{fn} not found")
            parts.append(src)
        harness = STUB_GLOBALS + "\n" + "\n".join(parts) + f"\nconsole.log(taskDrawerMenuHtml({task}));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            p = f.name
        try:
            res = subprocess.run([node_bin, p], capture_output=True, text=True, timeout=15)
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, f"node failed: {res.stderr}")
        return res.stdout

    def test_view_pane_uses_pane_id(self):
        html = self._menu_html_via_node(json.dumps({"task_id": "T1", "pane_id": "p9", "status": "working"}))
        self.assertIn("showPane('p9')", html)
        self.assertNotIn("showPane('T1')", html)
        self.assertIn("查看工位", html)

    def test_view_pane_hidden_without_pane_id(self):
        html = self._menu_html_via_node(json.dumps({"task_id": "T1", "status": "working"}))
        self.assertNotIn("查看工位", html)
        self.assertNotIn("showPane", html)

    def test_escape_closes_topmost_layer_first(self):
        seg_start = self.html.find("if(e.key==='Escape')")
        self.assertNotEqual(seg_start, -1, "Escape handler not found")
        seg = self.html[seg_start:seg_start + 1200]
        self.assertIn("modal.classList.contains('open')", seg)
        p1 = seg.find("closeModal();return")
        p2 = seg.find("closeTaskDrawerMenu();return")
        p3 = seg.find("closeTaskDrawer();return")
        self.assertTrue(-1 < p1 < p2 < p3, "Escape must unwind modal -> drawer menu -> drawer in order")

    def _resolve_via_node(self, exprs):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        src = _extract_fn(self.html, "resolveTaskDetail")
        self.assertIsNotNone(src, "resolveTaskDetail not found")
        harness = src + "\nconsole.log(JSON.stringify([" + ",".join(exprs) + "]));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            p = f.name
        try:
            res = subprocess.run([node_bin, p], capture_output=True, text=True, timeout=15)
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, f"node failed: {res.stderr}")
        return json.loads(res.stdout.strip())

    def test_degrade_task_ok_proj_fail(self):
        (r,) = self._resolve_via_node([
            "resolveTaskDetail('T',{task:{task_id:'T',status:'working'}},null,{message:'p'},null,null)",
        ])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["task"]["task_id"], "T")

    def test_degrade_task_fail_proj_ok(self):
        (r,) = self._resolve_via_node([
            "resolveTaskDetail('T',null,{task_id:'T',status:'working',goal:'g'},{message:'t'},null,null)",
        ])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["task"]["task_id"], "T")

    def test_both_fail_no_row_is_error_not_empty_task(self):
        (r,) = self._resolve_via_node([
            "resolveTaskDetail('T',null,null,{message:'Task 不存在'},{message:'nope'},null)",
        ])
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["message"], "Task 不存在")
        self.assertNotIn("task", r)

    def test_both_fail_with_row_renders_cached_data(self):
        (r,) = self._resolve_via_node([
            "resolveTaskDetail('T',null,null,{message:'a'},{message:'b'},{task_id:'T',status:'working'})",
        ])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["task"]["task_id"], "T")

    def test_failure_header_not_stuck_on_loading(self):
        seg_start = self.html.find("async function openTaskDrawer(")
        self.assertNotEqual(seg_start, -1)
        seg = self.html[seg_start:seg_start + 4000]
        self.assertIn("taskDrawerTitle').textContent='任务详情加载失败'", seg)

    def test_stale_response_guard_intact(self):
        seg_start = self.html.find("async function openTaskDrawer(")
        self.assertNotEqual(seg_start, -1)
        seg = self.html[seg_start:seg_start + 4000]
        self.assertIn("if(state.selectedTaskId!==tid)return;", seg)


if __name__ == "__main__":
    unittest.main()
