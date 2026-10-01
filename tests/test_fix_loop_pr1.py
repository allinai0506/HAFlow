"""Fix-loop PR-1 tests: worker --onto checkout, reopen-workflow, suppress_auto_close latch."""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_worker = _load_module(
    "herdr_worker_pr1_test",
    HERDR_ROOT / "services" / "herdr-worker.py",
)
_ht = _load_module(
    "herdr_task_pr1_test",
    HERDR_ROOT / "bin" / "herdr-task",
)
_ctl = _load_module(
    "herdr_controller_pr1_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)


def _resp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class CheckoutOntoBranchTest(unittest.TestCase):
    def _git_side_effect(self, local_exists=True, remote_exists=True,
                         local_ahead=True):
        def side_effect(cmd, **kwargs):
            args = list(cmd)
            if "fetch" in args:
                return _resp(0 if remote_exists else 0, stderr="" if remote_exists else "err")
            if "rev-parse" in args:
                return _resp(0 if remote_exists else 1)
            if "show-ref" in args:
                return _resp(0 if local_exists else 1)
            if "merge-base" in args:
                return _resp(0 if local_ahead else 1)
            if "switch" in args:
                return _resp(0)
            return _resp(1, stderr=f"unexpected git call: {args}")

        return side_effect

    def test_creates_local_branch_from_origin_when_missing(self):
        calls = []

        def record(cmd, **kwargs):
            resp = self._git_side_effect(local_exists=False)(cmd, **kwargs)
            calls.append(list(cmd))
            return resp

        with patch.object(_worker.subprocess, "run", side_effect=record):
            branch = _worker.checkout_onto_branch("/tmp/clone", "agent/x/feat-fix")

        self.assertEqual(branch, "agent/x/feat-fix")
        switch = [c for c in calls if "switch" in c][0]
        self.assertIn("-c", switch)
        self.assertIn("origin/agent/x/feat-fix", switch)

    def test_checks_out_existing_local_branch(self):
        calls = []

        def record(cmd, **kwargs):
            resp = self._git_side_effect(local_exists=True)(cmd, **kwargs)
            calls.append(list(cmd))
            return resp

        with patch.object(_worker.subprocess, "run", side_effect=record):
            _worker.checkout_onto_branch("/tmp/clone", "agent/x/feat-fix")

        switch = [c for c in calls if "switch" in c][0]
        self.assertNotIn("-c", switch)

    def test_missing_remote_fails_fast(self):
        with patch.object(
            _worker.subprocess,
            "run",
            side_effect=self._git_side_effect(remote_exists=False),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _worker.checkout_onto_branch("/tmp/clone", "nope/branch")

        self.assertIn("Onto branch not found", str(ctx.exception))

    def test_fetch_failure_fails_fast(self):
        def side_effect(cmd, **kwargs):
            if "fetch" in cmd:
                return _resp(1, stderr="fatal: could not read remote")
            return _resp(1)

        with patch.object(_worker.subprocess, "run", side_effect=side_effect):
            with self.assertRaises(RuntimeError) as ctx:
                _worker.checkout_onto_branch("/tmp/clone", "some/branch")

        self.assertIn("Onto branch not found", str(ctx.exception))

    def test_diverged_local_branch_fails_fast(self):
        def side_effect(cmd, **kwargs):
            args = list(cmd)
            if "fetch" in args:
                return _resp(0)
            if "rev-parse" in args:
                return _resp(0)
            if "show-ref" in args:
                return _resp(0)
            if "merge-base" in args:
                return _resp(1)
            return _resp(1, stderr=f"unexpected: {args}")

        with patch.object(_worker.subprocess, "run", side_effect=side_effect):
            with self.assertRaises(RuntimeError) as ctx:
                _worker.checkout_onto_branch("/tmp/clone", "agent/x/pr")

        self.assertIn("diverged", str(ctx.exception))


class ReopenWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-reopen-")
        root = Path(self.tmp.name)
        self.workflows_file = root / "workflows.json"
        self.stage_state_file = root / "stage-state.json"
        self.tasks_file = root / "tasks.json"
        _ht.WORKFLOWS_FILE = str(self.workflows_file)
        _ht.STAGE_STATE_FILE = str(self.stage_state_file)
        _ht.TASKS_FILE = str(self.tasks_file)

    def _write_workflow(self, entry):
        self.workflows_file.write_text(
            json.dumps({"workflows": {"wf-1": entry}}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _write_stage_state(self, state):
        self.stage_state_file.write_text(
            json.dumps(state), encoding="utf-8"
        )

    def test_refuses_unknown_or_noncompleted(self):
        with patch.object(_ht, "_herdr", return_value=_resp(1)):
            with self.assertRaises(SystemExit):
                _ht.reopen_workflow("wf-missing")

        self._write_workflow({"status": "in_progress"})
        with patch.object(_ht, "_herdr", return_value=_resp(1)):
            with self.assertRaises(SystemExit):
                _ht.reopen_workflow("wf-1")

    def test_refuses_when_coordinator_pane_dead(self):
        self._write_workflow(
            {
                "status": "completed",
                "coordinator_pane_id": "wA:p1",
            }
        )

        with patch.object(_ht, "_herdr", return_value=_resp(1)):
            with self.assertRaises(SystemExit):
                _ht.reopen_workflow("wf-1")

    def test_reopen_flips_status_sets_latch_and_resets_stages(self):
        self._write_workflow(
            {
                "status": "completed",
                "coordinator_pane_id": "wA:p1",
            }
        )
        self._write_stage_state({"wf-1:wrapup": "notified"})

        panes = [{"pane_id": "wA:p1"}, {"pane_id": "wA:p2N"}]
        listing = _resp(0, stdout=json.dumps({"result": {"panes": panes}}))
        with patch.object(_ht, "_herdr", return_value=listing):
            _ht.reopen_workflow("wf-1")

        entry = json.loads(self.workflows_file.read_text())["workflows"]["wf-1"]
        self.assertEqual(entry["status"], "in_progress")
        self.assertTrue(entry["suppress_auto_close"])
        self.assertIn("reopened_at", entry)
        self.assertEqual(
            json.loads(self.stage_state_file.read_text()),
            {},
        )


class SuppressAutoCloseLatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-latch-")
        root = Path(self.tmp.name)
        self.workflows_file = root / "workflows.json"
        self.tasks_file = root / "tasks.json"
        self.addCleanup(self.tmp.cleanup)
        for module in (_ht, _ctl):
            for name, value in (("TASKS_FILE", self.tasks_file),
                                ("WORKFLOWS_FILE", self.workflows_file)):
                binding = patch.object(module, name, str(value))
                binding.start()
                self.addCleanup(binding.stop)
        _ctl._workflow_close_inflight.clear()

    def _write(self, tasks, workflows):
        self.tasks_file.write_text(
            json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8"
        )
        self.workflows_file.write_text(
            json.dumps({"workflows": workflows}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _workflow_entry(self):
        return json.loads(self.workflows_file.read_text())["workflows"]["wf-1"]

    def test_first_active_task_clears_latch(self):
        self._write(
            [{"task_id": "t1", "status": "pending", "workflow_id": "wf-1"}],
            {
                "wf-1": {
                    "status": "in_progress",
                    "suppress_auto_close": True,
                }
            },
        )

        _ht.set_status("t1", "dispatched")

        self.assertNotIn("suppress_auto_close", self._workflow_entry())

    def test_non_active_transition_keeps_latch(self):
        self._write(
            [
                {
                    "task_id": "t1",
                    "status": "agent_done",
                    "workflow_id": "wf-1",
                    "status_history": [],
                }
            ],
            {
                "wf-1": {
                    "status": "in_progress",
                    "suppress_auto_close": True,
                }
            },
        )

        _ht.set_status("t1", "completed")

        self.assertTrue(self._workflow_entry()["suppress_auto_close"])

    def test_controller_skips_auto_close_while_latched(self):
        self._write(
            [],
            {
                "wf-1": {
                    "status": "in_progress",
                    "suppress_auto_close": True,
                }
            },
        )

        with patch.object(_ctl.subprocess, "run") as run_mock:
            _ctl.maybe_close_completed_workflow("wf-1")

        run_mock.assert_not_called()
        self.assertNotIn("wf-1", _ctl._workflow_close_inflight)


if __name__ == "__main__":
    unittest.main()
