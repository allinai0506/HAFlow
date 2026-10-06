#!/usr/bin/env python3
"""已关闭工作流的 CLI 报错必须是干净的一行，而不是裸 ValueError traceback。

现场缺陷（tech-debt #5）：对 closed workflow 派单（implementation-05）时，
issue_completion_contract 抛 ValueError('Workflow is closed') 直接炸出调用栈。
修复后 CLI 顶层捕获类型化 WorkflowClosedError，给出 reopen 指引并 exit 2。
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from herdr.completion_receipt import WorkflowClosedError  # noqa: E402


class WorkflowClosedErrorContractTest(unittest.TestCase):
    def test_still_a_value_error_for_existing_catchers(self):
        self.assertTrue(issubclass(WorkflowClosedError, ValueError))


class ClosedWorkflowCliErrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-closed-wf-")
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            **__import__("os").environ,
            "HERDR_STATE_DB": str(Path(self.tmp.name) / "state.db"),
            "TASKS_FILE": str(Path(self.tmp.name) / "tasks.json"),
            "WORKFLOWS_FILE": str(Path(self.tmp.name) / "workflows.json"),
        }

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "herdr-task"), *args],
            text=True, capture_output=True, env=self.env, timeout=60,
        )

    def _close_workflow(self):
        conn = sqlite3.connect(self.env["HERDR_STATE_DB"])
        try:
            conn.execute(
                "UPDATE workflows SET status='completed' WHERE workflow_id='wf-closed'")
            conn.commit()
        finally:
            conn.close()

    def test_report_completion_on_closed_workflow_is_a_clean_error(self):
        added = self._cli("add", "--task-id", "t-closed",
                          "--workflow-id", "wf-closed", "--node", "implementation",
                          "--workspace", "/tmp", "--pane", "w1:p1", "--agent", "codex",
                          "--goal", "test")
        self.assertEqual(added.returncode, 0, added.stderr)
        self._close_workflow()

        identity = Path(self.tmp.name) / "ident.json"
        identity.write_text(json.dumps(
            {"task_id": "t-closed", "run_id": "r1", "epoch": "e1", "token": "tok"}))

        result = self._cli("report-completion", "t-closed",
                           "--identity-file", str(identity))

        self.assertEqual(result.returncode, 2)
        self.assertIn("工作流已关闭", result.stderr)
        self.assertIn("reopen-workflow", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Traceback", result.stdout)


if __name__ == "__main__":
    unittest.main()
