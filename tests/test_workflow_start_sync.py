"""Regression tests for Workflow startup request/state synchronization."""

import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_projects():
    path = ROOT / "herdr" / "projects.py"
    spec = importlib.util.spec_from_loader(
        "herdr_projects_start_sync_test",
        importlib.machinery.SourceFileLoader("herdr_projects_start_sync_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestWorkflowStartSync(unittest.TestCase):
    def test_requirement_is_persisted_until_preflight_opens_gate(self):
        module = load_projects()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_workflows_file = module.WORKFLOWS_FILE
            self.addCleanup(setattr, module, "WORKFLOWS_FILE", original_workflows_file)
            module.WORKFLOWS_FILE = root / "workflows.json"
            project = {
                "project_id": "project-1",
                "project_name": "demo",
                "project_root": str(root),
                "base_branch": "main",
                "workspace_id": "w1",
                "coordinator_pane_id": "w1:p1",
                "workflow_file": str(root / "workflow.json"),
            }
            module.register_workflow("wf-1", project, requirement="具体需求")
            record = module.project_for_workflow("wf-1")
            self.assertEqual(record["requirement"], "具体需求")
            self.assertFalse(record["startup_ready"])

            module.mark_workflow_startup_ready("wf-1", ["opencode"], {})
            record = module.project_for_workflow("wf-1")
            self.assertTrue(record["startup_ready"])
            self.assertEqual(record["healthy_agents"], ["opencode"])

    def test_factory_no_longer_directly_prompts_coordinator(self):
        source = (ROOT / "bin" / "herdr-factory").read_text(encoding="utf-8")
        start = source.index("def start_workflow(")
        end = source.index("\ndef status(", start)
        body = source[start:end]
        self.assertNotIn('"agent",\n            "prompt"', body)
        self.assertIn("mark_workflow_startup_ready", body)

    def test_controller_checks_startup_gate_in_queue_consumer(self):
        source = (ROOT / "services" / "controller" / "stage_delivery.py").read_text(encoding="utf-8")
        self.assertIn('startup_record.get("startup_ready") is False', source)
        self.assertIn('queued event held until request is ready', source)


if __name__ == "__main__":
    unittest.main()
