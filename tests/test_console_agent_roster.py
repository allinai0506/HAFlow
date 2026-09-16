"""Regression tests for Factory Console agent roster binary detection."""

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_agent_roster_test",
        importlib.machinery.SourceFileLoader("herdr_console_agent_roster_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConsoleAgentRoster(unittest.TestCase):
    def test_preflight_uses_shared_resolver(self):
        module = load_console()
        with patch.object(module, "pool", return_value={}), \
             patch.object(module, "agent_loads", return_value={}), \
             patch.object(module, "resolve_agent_binary",
                          side_effect=lambda a: "/bin/fake-codex" if a == "codex" else None):
            rows = module.preflight({"project_id": "p1"})
        by_agent = {r["agent"]: r for r in rows}
        self.assertEqual(by_agent["codex"]["status"], "ready")
        self.assertTrue(by_agent["codex"]["installed"])
        self.assertEqual(by_agent["codex"]["binary"], "/bin/fake-codex")
        self.assertEqual(by_agent["agy"]["status"], "missing")
        self.assertFalse(by_agent["agy"]["installed"])

    def test_agent_binaries_mapping_is_shared_not_local(self):
        module = load_console()
        from herdr import agent_binary
        self.assertNotIn("AGENT_BINARIES", vars(module))
        # qodercli -> qodercn 映射必须来自 herdr.agent_binary 单一事实来源
        self.assertEqual(agent_binary.AGENT_BINARIES["qodercli"], "qodercn")
        self.assertEqual(agent_binary.AGENT_BINARIES["grok"], "grok")


if __name__ == "__main__":
    unittest.main()
