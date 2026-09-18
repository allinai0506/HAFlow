"""herdr-task note-add/note-list CLI + gate-evidence hook tests.

Covers the shell half of the workflow shared document area:
  - note-add/note-list roundtrip through the real CLI entry point;
  - validation failures surface as non-zero exits;
  - provenance (node/agent) is derived from the source task record;
  - set_status' gate-evidence hook writes controller-authored gate notes.
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import workflow_docs as wd


def _load_task_cli(name="herdr_task_note_cli_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "bin" / "herdr-task")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NoteCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-wfdocs-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.docs_root = Path(self.tmp.name) / "docs"
        self.tasks_file = Path(self.tmp.name) / "tasks.json"
        self.tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        self.env = patch.dict(
            os.environ,
            {
                wd.DOCS_DIR_ENV: str(self.docs_root),
                "TASKS_FILE": str(self.tasks_file),
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(HERDR_ROOT / "bin" / "herdr-task"), *args],
            text=True,
            capture_output=True,
        )

    def test_note_add_and_list_roundtrip(self):
        added = self._run(
            "note-add",
            "--workflow-id", "wf-cli",
            "--kind", "spec",
            "--title", "规格标题",
            "--text", "正文内容",
            "--node", "requirements",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("[NOTE_ADDED]", added.stdout)

        listed = self._run("note-list", "wf-cli", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        data = json.loads(listed.stdout)
        self.assertEqual(len(data["notes"]), 1)
        self.assertEqual(data["notes"][0]["title"], "规格标题")
        self.assertEqual(data["notes"][0]["node"], "requirements")

    def test_note_add_from_file(self):
        body_file = Path(self.tmp.name) / "body.md"
        body_file.write_text("# 计划\n- 步骤一", encoding="utf-8")
        added = self._run(
            "note-add",
            "--workflow-id", "wf-cli",
            "--kind", "plan",
            "--title", "计划文件",
            "--file", str(body_file),
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        notes = wd.load_notes("wf-cli")
        self.assertIn("步骤一", notes[0]["body"])

    def test_note_add_rejects_bad_workflow_id(self):
        added = self._run(
            "note-add",
            "--workflow-id", "../evil",
            "--kind", "note",
            "--title", "x",
        )
        self.assertEqual(added.returncode, 2)
        self.assertIn("invalid workflow id", added.stdout)

    def test_note_add_rejects_unknown_kind(self):
        added = self._run(
            "note-add",
            "--workflow-id", "wf-cli",
            "--kind", "bogus",
            "--title", "x",
        )
        self.assertEqual(added.returncode, 2)

    def test_note_add_rejects_text_and_file_together(self):
        body_file = Path(self.tmp.name) / "body.md"
        body_file.write_text("x", encoding="utf-8")
        added = self._run(
            "note-add",
            "--workflow-id", "wf-cli",
            "--kind", "note",
            "--title", "x",
            "--text", "y",
            "--file", str(body_file),
        )
        self.assertEqual(added.returncode, 2)

    def test_note_add_rejects_controller_source(self):
        added = self._run(
            "note-add",
            "--workflow-id", "wf-cli",
            "--kind", "gate",
            "--title", "x",
            "--source", "controller",
        )
        self.assertEqual(added.returncode, 2)

    def test_note_list_rejects_bad_workflow_id(self):
        listed = self._run("note-list", "../evil")
        self.assertEqual(listed.returncode, 2)
        self.assertIn("invalid workflow id", listed.stdout)

    def test_note_add_derives_provenance_from_task(self):
        mod = _load_task_cli()
        task = {
            "task_id": "wf-cli-req-executor",
            "workflow_id": "wf-cli",
            "node": "requirements",
            "stage": "requirements",
            "agent": "claude",
            "status": "working",
        }
        args = Namespace(
            workflow_id="wf-cli",
            kind="evidence",
            title="需求证据",
            text="done",
            file=None,
            node=None,
            task="wf-cli-req-executor",
            agent=None,
            source="agent",
            base_sha=None,
            round=1,
            invalidates=None,
        )
        with patch.object(mod, "load_tasks", return_value={"tasks": [task]}):
            mod.note_add(args)
        notes = wd.load_notes("wf-cli")
        self.assertEqual(notes[0]["node"], "requirements")
        self.assertEqual(notes[0]["agent"], "claude")
        self.assertEqual(notes[0]["task_id"], "wf-cli-req-executor")

    def test_record_gate_note_writes_controller_evidence(self):
        mod = _load_task_cli("herdr_task_gate_note_test")
        task = {
            "task_id": "wf-cli-test-auto",
            "workflow_id": "wf-cli",
            "node": "test",
            "stage": "test",
            "agent": "codex",
            "status": "completed",
        }
        mod._record_gate_note(task, "blocked", "P0 未修")
        notes = wd.load_notes("wf-cli")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["kind"], "gate")
        self.assertEqual(notes[0]["source"], "controller")
        self.assertEqual(notes[0]["body"], "P0 未修")
        self.assertIn("blocked", notes[0]["title"])

    def test_record_gate_note_failure_never_raises(self):
        mod = _load_task_cli("herdr_task_gate_note_safe_test")
        with patch.object(
            wd, "append_note", side_effect=RuntimeError("disk full")
        ):
            mod._record_gate_note(
                {"task_id": "t", "workflow_id": "wf-cli", "node": "test"},
                "pass",
                None,
            )

    def _make_clone(self, task_id, branch):
        clone_root = Path(self.tmp.name) / "clones"
        clone = clone_root / task_id
        clone.mkdir(parents=True)
        for cmd in (
            ["git", "init", "-q", str(clone)],
            ["git", "-C", str(clone), "config", "user.email", "t@test"],
            ["git", "-C", str(clone), "config", "user.name", "t"],
        ):
            subprocess.run(cmd, check=True, capture_output=True)
        (clone / "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(clone), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(clone), "commit", "-qm", "init"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(clone), "checkout", "-qb", branch],
            check=True, capture_output=True,
        )
        return clone_root

    def test_gate_note_gets_base_sha_from_clone(self):
        task_id = "wf-cli-test-auto"
        branch = "agent/codex/test-wf-cli"
        clone_root = self._make_clone(task_id, branch)
        mod = _load_task_cli("herdr_task_gate_sha_test")
        task = {
            "task_id": task_id,
            "workflow_id": "wf-cli",
            "node": "test",
            "stage": "test",
            "agent": "codex",
            "branch": branch,
            "status": "completed",
        }
        with patch.dict(
            os.environ, {"HERDR_CLONES_DIR": str(clone_root)}, clear=False
        ):
            mod._record_gate_note(task, "pass", "ok")
        notes = wd.load_notes("wf-cli")
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["base_sha"])

    def test_gate_note_is_idempotent(self):
        mod = _load_task_cli("herdr_task_gate_idem_test")
        task = {
            "task_id": "wf-cli-review-auto",
            "workflow_id": "wf-cli",
            "node": "review",
            "stage": "review",
            "agent": "claude",
            "status": "completed",
        }
        mod._record_gate_note(task, "pass", None)
        mod._record_gate_note(task, "pass", None)
        notes = wd.load_notes("wf-cli")
        self.assertEqual(len(notes), 1)


if __name__ == "__main__":
    unittest.main()
