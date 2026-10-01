"""Tests for Linear-style Standard Workflow Dropdown in HAFlow Console."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


class TestConsoleLinearDropdown(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "console" / "herdr_factory_console.py"
        cls.source = path.read_text(encoding="utf-8")

    def test_linear_dropdown_css_tokens(self):
        """Guard that Linear dropdown design system tokens and classes exist."""
        required_css_tokens = [
            ".linear-select",
            ".linear-trigger",
            ".linear-popover",
            ".popover-search-wrap",
            ".popover-search-input",
            ".popover-group",
            ".popover-group-header",
            ".popover-item",
            ".trigger-mono",
            ".trigger-pills",
        ]
        for token in required_css_tokens:
            self.assertIn(token, self.source, f"missing CSS token: {token}")

    def test_backward_compatibility_tokens(self):
        """Guard that legacy tokens required by existing automation and tests are preserved."""
        legacy_tokens = [
            "dashWfSel",
            "dashSelectWorkflow",
            "dashOpenWorkflow",
            "进入该工作流",
            "dashWorkflowId",
            "workflow_id=",
        ]
        for token in legacy_tokens:
            self.assertIn(token, self.source, f"missing legacy token: {token}")

    def test_collapsible_groups_support(self):
        """Guard that status groups support clickable collapse and expand."""
        group_tokens = [
            "togglePopoverGroup",
            "popover-group-header",
            "collapsed",
        ]
        for token in group_tokens:
            self.assertIn(token, self.source, f"missing collapsible group token: {token}")

    def test_dual_line_and_mono_id_rendering(self):
        """Guard that the dropdown explicitly presents workflow_id alongside title."""
        self.assertIn("trigger-mono", self.source)
        self.assertIn("item-id", self.source)

    def test_search_and_keyboard_navigation(self):
        """Guard that search filter and keyboard handling are implemented."""
        search_tokens = [
            "popover-search-input",
            "handleLinearSelectSearch",
            "handleLinearSelectKeydown",
        ]
        for token in search_tokens:
            self.assertIn(token, self.source, f"missing search/keyboard token: {token}")

    def test_outside_click_and_escape_dismissal(self):
        """Guard that clicking outside the combobox or pressing Escape closes the popover."""
        self.assertIn("!e.target.closest('.linear-select'))closeLinearSelectMenu()", self.source)
        self.assertIn("closeLinearSelectMenu();", self.source)

    def test_badge_pills_styling_and_labels(self):
        """Guard that badge pills avoid global .dot collision and use polished labels."""
        self.assertNotIn(".badge-pill.dot", self.source)
        self.assertIn("tabular-nums", self.source)
        self.assertIn("活跃</span>", self.source)
        self.assertIn("需决策</span>", self.source)
        self.assertIn("已完成</span>", self.source)


if __name__ == "__main__":
    unittest.main()
