"""Controller finalize grading tests (AC-4, AC-5, AC-6).

`finalize_completed_task` must grade commit exit codes instead of silently
returning: empty (3) auto-releases with a credible anchor or escalates,
refused (4) and rebase-conflict (6) escalate immediately, and completed+git
tasks are reachable via commit_retry with an escalation event on exhaustion.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

_TMP = tempfile.TemporaryDirectory(prefix="herdr-finalize-empty-")
os.environ["HERDR_ATTENTION_FILE"] = str(Path(_TMP.name) / "attention.json")


def _load_controller(name="ctrl_finalize_empty_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ctrl = _load_controller()


def _task(task_id="t-empty", status="completed", **extra):
    task = {
        "task_id": task_id,
        "workflow_id": "wf-empty",
        "run_id": "run-empty",
        "node": "implementation",
        "stage": "implementation",
        "status": status,
        "agent": "opencode",
        "integration_mode": "git",
    }
    task.update(extra)
    return task


def _commit_result_payload(**fields):
    payload = {"task_id": "t-empty", "result": "empty",
               "head": "abc123", "basis": "baseline_commit"}
    payload.update(fields)
    return payload


def _run_with_commit(task, commit_rc, commit_payload, get_sequence):
    """Run finalize with a scripted commit outcome; return output + mocks."""
    stdout_lines = [
        "Task has no changes to commit: t-empty",
        "HERDR_COMMIT_RESULT=" + json.dumps(commit_payload),
    ]
    commit_proc = subprocess.CompletedProcess(
        [], commit_rc, "\n".join(stdout_lines) + "\n", ""
    )
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "commit" in cmd:
            return commit_proc
        return subprocess.CompletedProcess([], 0, "", "")

    store = MagicMock()
    get_iter = iter(get_sequence)

    with patch.object(ctrl.subprocess, "run", side_effect=_fake_run), \
        patch.object(ctrl, "get_task", side_effect=lambda _tid: next(get_iter)), \
        patch.object(ctrl, "set_task_status", return_value=True) as set_status, \
        patch.object(ctrl, "_get_store", return_value=store), \
        patch.object(ctrl, "enqueue_stage_advance"), \
        patch.object(ctrl, "ensure_no_git_processes", return_value=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ctrl.finalize_completed_task(task["task_id"])
    return buf.getvalue(), calls, set_status, store


class FinalizeEmptyTest(unittest.TestCase):
    def test_empty_with_anchor_auto_releases(self):
        task = _task(baseline_commit="base" * 10)
        released = _task(status="cleanup_ready", baseline_commit="base" * 10)
        output, calls, set_status, store = _run_with_commit(
            task, 3, _commit_result_payload(),
            [task, task, released, released],
        )

        self.assertIn("[FINALIZE EMPTY]", output)
        self.assertIn("[FINALIZE EMPTY RELEASED]", output)
        self.assertIn("[FINALIZED]", output)
        set_status.assert_called_once_with("t-empty", "cleanup_ready")
        kinds = [cmd for cmd in calls]
        self.assertTrue(any("commit" in cmd for cmd in kinds))
        self.assertTrue(any("cleanup" in cmd for cmd in kinds))
        self.assertFalse(any("integrate" in cmd for cmd in kinds))
        event_types = [call.args[0] for call in store.record_event.call_args_list]
        self.assertIn("finalize_empty", event_types)

    def test_empty_without_anchor_escalates(self):
        task = _task()
        with patch("herdr.kernel.update_task_metadata",
                   return_value={}) as meta:
            output, calls, set_status, _store = _run_with_commit(
                task, 3, _commit_result_payload(basis="time"),
                [task, task],
            )

        self.assertIn("[FINALIZE EMPTY]", output)
        self.assertIn("[FINALIZE ESCALATED]", output)
        self.assertNotIn("[FINALIZED]", output)
        set_status.assert_not_called()
        self.assertFalse(any("cleanup" in cmd for cmd in calls))
        self.assertFalse(any("integrate" in cmd for cmd in calls))
        meta.assert_called_once()
        updates = meta.call_args.args[1]
        self.assertTrue(updates.get("finalize_escalated"))
        self.assertEqual(updates.get("finalize_escalate_reason"),
                         "empty_unreleasable")
        self.assertEqual(meta.call_args.args[0], "t-empty")

    def test_refused_escalates_immediately(self):
        task = _task(onto_branch="pr-branch")
        payload = _commit_result_payload(result="refused",
                                         reason="no_baseline_onto")
        with patch("herdr.kernel.update_task_metadata",
                   return_value={}):
            output, calls, set_status, store = _run_with_commit(
                task, 4, payload, [task, task],
            )

        self.assertIn("[FINALIZE REFUSED]", output)
        self.assertIn("no_baseline_onto", output)
        self.assertIn("[FINALIZE ESCALATED]", output)
        set_status.assert_not_called()
        self.assertEqual(
            [cmd for cmd in calls if "integrate" in cmd], []
        )
        escalations = [
            call for call in store.record_event.call_args_list
            if call.args[0] == "finalize_escalated"
        ]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0].args[1]["reason"], "commit_refused")


class FinalizeRetryReachabilityTest(unittest.TestCase):
    def tearDown(self):
        ctrl.attention_clear("t-retry:finalize")
        ctrl._finalize_retry_exhausted_logged.discard("t-retry")
        ctrl._finalize_retry_exhausted_logged.discard("t-gone")

    def test_completed_drives_commit_retry(self):
        task = _task(task_id="t-retry")
        progressed = _task(task_id="t-retry", status="committed")
        with patch.object(ctrl, "finalize_completed_task") as fin, \
            patch.object(ctrl, "get_task", return_value=progressed), \
            patch.object(ctrl, "workflow_closed", return_value=False):
            buf = io.StringIO()
            with redirect_stdout(buf):
                reached = ctrl._check_finalize_retry(task, "completed", 1700000000.0)

        self.assertTrue(reached)
        fin.assert_called_once_with("t-retry")
        self.assertIn(
            "status=completed -> retry finalize (commit_retry)", buf.getvalue()
        )

    def test_exhausted_records_escalation_event(self):
        task = _task(task_id="t-gone")
        ctrl.attention_note("t-gone:finalize", task, "finalize",
                            reason="commit_retry",
                            attempts=ctrl.FINALIZE_RETRY_MAX)
        with patch.object(ctrl, "finalize_completed_task") as fin, \
            patch.object(ctrl, "get_task", return_value=task), \
            patch.object(ctrl, "workflow_closed", return_value=False), \
            patch.object(ctrl, "_escalate_finalize") as esc:
            buf = io.StringIO()
            with redirect_stdout(buf):
                reached = ctrl._check_finalize_retry(task, "completed", 1700000000.0)

        self.assertTrue(reached)
        fin.assert_not_called()
        esc.assert_called_once()
        self.assertIn("[FINALIZE RETRY EXHAUSTED]", buf.getvalue())
        self.assertIn("t-gone", ctrl._finalize_retry_exhausted_logged)

    def test_pending_excludes_escalated(self):
        escalated = _task(task_id="t-esc", finalize_escalated=True)
        normal = _task(task_id="t-ok")
        committed = _task(task_id="t-cm", status="committed")
        with patch.object(ctrl, "load_tasks",
                          return_value=[escalated, normal, committed]):
            pending = ctrl.git_finalize_pending_tasks("wf-empty")

        self.assertEqual(pending, ["t-ok", "t-cm"])


if __name__ == "__main__":
    unittest.main()
