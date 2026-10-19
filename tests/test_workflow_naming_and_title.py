import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Ensure herdr root is on sys.path
HERDR_ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(HERDR_ROOT))

from herdr import projects
from herdr.projects import (
    generate_workflow_id,
    register_workflow,
)


class WorkflowNamingAndTitleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workflows_file = Path(self.tmp.name) / "workflows.json"
        self.patcher = mock.patch.object(projects, "WORKFLOWS_FILE", self.workflows_file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_generate_workflow_id_first_seq(self):
        project = {
            "project_id": "nexusarchive-54433229",
            "project_name": "NexusArchive",
            "project_root": "/tmp/nexusarchive",
        }
        fixed_now = datetime.datetime(2026, 9, 13, 11, 10, 49)
        wid = generate_workflow_id(project, prefix="wf", now=fixed_now)
        self.assertEqual(wid, "wf-nexusarchive-0913-01")

    def test_generate_workflow_id_increments_sequence(self):
        project = {
            "project_id": "nexusarchive-54433229",
            "project_name": "NexusArchive",
            "project_root": "/tmp/nexusarchive",
        }
        fixed_now = datetime.datetime(2026, 9, 13, 11, 10, 49)

        # Existing workflow in registry
        existing = {
            "version": 1,
            "workflows": {
                "wf-nexusarchive-0913-01": {"project_id": "nexusarchive-54433229"},
            },
        }
        self.workflows_file.write_text(json.dumps(existing), encoding="utf-8")

        wid = generate_workflow_id(project, prefix="wf", now=fixed_now)
        self.assertEqual(wid, "wf-nexusarchive-0913-02")

    def test_generate_workflow_id_e2e_prefix(self):
        project = {
            "project_id": "demo-12345678",
            "project_name": "demo",
            "project_root": "/tmp/demo",
        }
        fixed_now = datetime.datetime(2026, 9, 13, 15, 0, 0)
        wid = generate_workflow_id(project, prefix="e2e", now=fixed_now)
        self.assertEqual(wid, "e2e-demo-0913-01")

    def test_register_workflow_with_explicit_title(self):
        project = {
            "project_id": "nexusarchive-54433229",
            "project_name": "NexusArchive",
            "project_root": "/tmp/nexusarchive",
            "workspace_id": "ws-1",
            "coordinator_pane_id": "pane-1",
            "workflow_file": "/tmp/nexusarchive-workflow.json",
        }
        register_workflow(
            "wf-nexusarchive-0913-01",
            project,
            requirement="## 需求\n第一步做登录\n第二步做登出",
            title="适配深色模式切换",
        )
        record = projects.load_workflows()["workflows"]["wf-nexusarchive-0913-01"]
        self.assertEqual(record["title"], "适配深色模式切换")
        self.assertEqual(record["requirement_subject"], "适配深色模式切换")

    def test_register_workflow_without_title_falls_back(self):
        project = {
            "project_id": "nexusarchive-54433229",
            "project_name": "NexusArchive",
            "project_root": "/tmp/nexusarchive",
            "workspace_id": "ws-1",
            "coordinator_pane_id": "pane-1",
            "workflow_file": "/tmp/nexusarchive-workflow.json",
        }
        register_workflow(
            "wf-nexusarchive-0913-01",
            project,
            requirement="## 需求\n第一步做登录\n第二步做登出",
        )
        record = projects.load_workflows()["workflows"]["wf-nexusarchive-0913-01"]
        self.assertEqual(record["title"], "")
        self.assertEqual(record["requirement_subject"], "第一步做登录")


import importlib.machinery
import importlib.util

def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_ht = _load_module("herdr_task_naming_test", HERDR_ROOT / "bin" / "herdr-task")


class ResolveWorkflowForCloseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workflows_file = Path(self.tmp.name) / "workflows.json"
        self.tasks_file = Path(self.tmp.name) / "tasks.json"
        self.tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")

        self.patchers = [
            mock.patch.object(projects, "WORKFLOWS_FILE", self.workflows_file),
            mock.patch.object(_ht, "TASKS_FILE", str(self.tasks_file)),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def test_exact_match(self):
        existing = {
            "version": 1,
            "workflows": {
                "wf-nexusarchive-0913-01": {"project_id": "p-1", "status": "working"},
            },
        }
        self.workflows_file.write_text(json.dumps(existing), encoding="utf-8")
        wid = _ht.resolve_workflow_id_for_close("wf-nexusarchive-0913-01")
        self.assertEqual(wid, "wf-nexusarchive-0913-01")

    def test_suffix_match(self):
        existing = {
            "version": 1,
            "workflows": {
                "wf-nexusarchive-0913-01": {"project_id": "p-1", "status": "working"},
            },
        }
        self.workflows_file.write_text(json.dumps(existing), encoding="utf-8")
        wid = _ht.resolve_workflow_id_for_close("0913-01")
        self.assertEqual(wid, "wf-nexusarchive-0913-01")

        wid2 = _ht.resolve_workflow_id_for_close("01")
        self.assertEqual(wid2, "wf-nexusarchive-0913-01")

    def test_implicit_detection_single_active(self):
        existing = {
            "version": 1,
            "workflows": {
                "wf-nexusarchive-0913-01": {"project_id": "p-1", "status": "working"},
            },
        }
        self.workflows_file.write_text(json.dumps(existing), encoding="utf-8")

        with mock.patch.object(_ht, "detect_git_root", return_value="/workspace/nexusarchive"):
            with mock.patch.object(
                _ht,
                "project_by_root",
                return_value={"project_id": "p-1", "project_name": "NexusArchive"},
            ):
                wid = _ht.resolve_workflow_id_for_close()
                self.assertEqual(wid, "wf-nexusarchive-0913-01")


_con = _load_module("herdr_console_naming_test", HERDR_ROOT / "console" / "herdr_factory_console.py")


class TestConsoleWorkflowTitleIntegration(unittest.TestCase):
    def test_run_workflow_includes_title_flag(self):
        with mock.patch.object(_con.common, "run") as mock_run:
            mock_run.return_value = type("Completed", (), {"returncode": 0, "stdout": "wf-1\n", "stderr": ""})()
            _con.run_workflow("/tmp/demo", "具体需求", agent="auto", template="t1", title="任务标题")
            cmd = mock_run.call_args[0][0]
            self.assertIn("--title", cmd)
            self.assertIn("任务标题", cmd)

    def test_with_subject_prefers_title(self):
        item = {
            "title": "显式任务标题",
            "requirement_subject": "正则提取的旧标题",
            "requirement": "## 需求\n正文",
        }
        res = _con._with_subject(item)
        self.assertEqual(res["requirement_subject"], "显式任务标题")


if __name__ == "__main__":
    unittest.main()

