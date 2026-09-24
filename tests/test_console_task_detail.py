"""Task Detail V1 drawer contracts: drawer DOM, tabs, row wiring, escape, empty states."""

import importlib.machinery
import importlib.util
import re
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


if __name__ == "__main__":
    unittest.main()
