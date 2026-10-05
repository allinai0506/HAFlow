"""Tests for ops-center split-view redesign, progress bars, anomaly actions, and auto-refresh."""

import importlib.machinery
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


def _load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_ops_test",
        importlib.machinery.SourceFileLoader("herdr_console_ops_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConsoleOpsCenterRedesign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = _load_console()
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")

    def test_ops_center_has_split_grid_and_panels(self):
        """Verify ops center defines split-view grid layout and column containers."""
        self.assertIn(".ops-dashboard-grid", self.html)
        self.assertIn("ops-col-left", self.html)
        self.assertIn("ops-col-right", self.html)
        self.assertIn("ops-panel-anomaly", self.html)
        self.assertIn("ops-panel-fleet", self.html)

    def test_ops_center_card_progress_and_expand_nodes(self):
        """Verify workflow cards include progress bar, callout, and node toggle."""
        self.assertIn("ops-progress-bar", self.html)
        self.assertIn("ops-progress-fill", self.html)
        self.assertIn("toggleOpsCardNodes", self.html)
        self.assertIn("filterOpsWorkflows", self.html)

    def test_ops_center_anomaly_repair_actions(self):
        """Verify anomaly action execution helper and backend repair handler exist."""
        self.assertIn("runOpsAnomalyAction", self.html)
        self.assertIn("ops_repair", self.html)

    def test_ops_center_timer_and_refresh_button(self):
        """Verify 10s auto-refresh timer, badge, and manual refresh button exist, and 10min text removed."""
        self.assertNotIn("每 10 分钟自动刷新", self.html)
        self.assertIn("startOpsTimer", self.html)
        self.assertIn("triggerOpsRefresh", self.html)
        self.assertIn("opsTimerBadge", self.html)
    def test_ops_center_adheres_to_4px_spacing_grid(self):
        """Verify ops center layout and styles adhere to Linear 4px spacing grid with 0 forbidden naked values."""
        import re
        bad_spacing_regex = re.compile(r'(padding|margin|gap)[^:;}\'\"]*:[^;}\'\"]*\b(5|6|7|9|10|11|13|14)px\b')
        matches = []
        for line in self.html.splitlines():
            if 'ops-' in line or 'showOpsAnomaly' in line or 'opsTimer' in line:
                m = bad_spacing_regex.search(line)
                if m:
                    matches.append((line.strip(), m.group(0)))
        self.assertEqual(matches, [], f"Found forbidden spacing values in ops center: {matches}")


if __name__ == "__main__":
    unittest.main()
