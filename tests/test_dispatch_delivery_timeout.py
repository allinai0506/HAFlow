"""Bounded prompt delivery tests for herdr-task dispatch.

Regression cover for the 2026-09-17 incident where the initial
`herdr agent prompt` delivery hung indefinitely (opencode never acked),
blocking `herdr-task launch`, which in turn blocked the controller's
direct-dispatch thread (no timeout there either) while holding the
per-workflow lock. The whole workflow stalled with zero log output.

Contract under test: the primary prompt submission must be time-bounded;
on timeout the existing verify/fallback path proceeds instead of hanging.
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from herdr.state_store import SQLiteStateStore
from herdr import kernel
from herdr import task_cli_execution as _execution

def _pending_task(task_id="t-dispatch-1"):
    return {
        "task_id": task_id,
        "workflow_id": "wf-dispatch",
        "node": "requirements",
        "stage": "requirements",
        "status": "pending",
        "pane_id": "wX:p9",
        "agent": "opencode",
        "clone_path": "/tmp/herdr-no-such-clone",
        "goal": "目标",
        "acceptance_criteria": ["验收"],
    }


class _FastClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 10.0
        return self.now


def _agent_get(status):
    return json.dumps({"result": {"agent": {"agent_status": status}}})


class DispatchDeliveryTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-dispatch-timeout-")
        self.store = SQLiteStateStore(Path(self.tmp.name) / "state.db")
        self.store.save_task(_pending_task())
        clock = _FastClock()
        self.patchers = [
            patch.object(time, "time", side_effect=clock),
            patch.object(time, "sleep", return_value=None),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self):
        return self.store.get_task("t-dispatch-1")

    def _dispatch(self):
        return _execution.handle_dispatch_task(
            "t-dispatch-1",
            "做需求分析",
            get_store=lambda: self.store,
            set_status=lambda task_id, status: kernel.transition_task(
                task_id, status, "dispatch delivery test",
                source="herdr-task", store=self.store,
            ),
            compile_context_ref=lambda _task: None,
            fetch_rules_block=lambda _task: "",
            prefetch_rules=lambda _task: "",
        )

    def test_primary_prompt_timeout_is_bounded_and_recovers(self):
        seen = []

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["herdr", "agent", "prompt"]:
                seen.append(kwargs.get("timeout"))
                raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))
            if cmd[:3] == ["herdr", "agent", "get"]:
                return subprocess.CompletedProcess(cmd, 0, _agent_get("working"), "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(subprocess, "run", side_effect=fake_run):
            self._dispatch()

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], _execution.PROMPT_DELIVERY_TIMEOUT)
        self.assertGreater(_execution.PROMPT_DELIVERY_TIMEOUT, 0)
        self.assertEqual(self._task()["status"], "dispatched")

    def test_primary_timeout_and_fallback_failure_marks_failed(self):
        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["herdr", "agent", "prompt"]:
                raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))
            if cmd[:3] == ["herdr", "agent", "get"]:
                return subprocess.CompletedProcess(cmd, 0, _agent_get("idle"), "")
            if cmd[:3] == ["herdr", "pane", "read"]:
                return subprocess.CompletedProcess(cmd, 0, "blank screen", "")
            if cmd[:3] == ["herdr", "pane", "send-text"]:
                return subprocess.CompletedProcess(cmd, 1, "", "send failed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(subprocess, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as ctx:
                self._dispatch()

        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(self._task()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
