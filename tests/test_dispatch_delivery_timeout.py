"""Bounded prompt delivery tests for herdr-task dispatch.

Regression cover for the 2026-09-17 incident where the initial
`herdr agent prompt` delivery hung indefinitely (opencode never acked),
blocking `herdr-task launch`, which in turn blocked the controller's
direct-dispatch thread (no timeout there either) while holding the
per-workflow lock. The whole workflow stalled with zero log output.

Contract under test: the primary prompt submission must be time-bounded;
on timeout the existing verify/fallback path proceeds instead of hanging.
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _import_herdr_task():
    task_bin = HERDR_ROOT / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader(
        "herdr_task_dispatch_timeout_test",
        importlib.machinery.SourceFileLoader(
            "herdr_task_dispatch_timeout_test",
            str(task_bin),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _import_herdr_task()


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
        self.task_file = str(Path(self.tmp.name) / "tasks.json")
        _ht.TASKS_FILE = self.task_file
        with open(self.task_file, "w", encoding="utf-8") as f:
            json.dump({"tasks": [_pending_task()]}, f)
        clock = _FastClock()
        self.patchers = [
            patch.object(_ht.time, "time", side_effect=clock),
            patch.object(_ht.time, "sleep", return_value=None),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self):
        with open(self.task_file, encoding="utf-8") as f:
            tasks = json.load(f)["tasks"]
        return next(t for t in tasks if t["task_id"] == "t-dispatch-1")

    def test_primary_prompt_timeout_is_bounded_and_recovers(self):
        seen = []

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["herdr", "agent", "prompt"]:
                seen.append(kwargs.get("timeout"))
                raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout"))
            if cmd[:3] == ["herdr", "agent", "get"]:
                return subprocess.CompletedProcess(cmd, 0, _agent_get("working"), "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(_ht.subprocess, "run", side_effect=fake_run):
            _ht.dispatch_task("t-dispatch-1", "做需求分析")

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], _ht.PROMPT_DELIVERY_TIMEOUT)
        self.assertGreater(_ht.PROMPT_DELIVERY_TIMEOUT, 0)
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

        with patch.object(_ht.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as ctx:
                _ht.dispatch_task("t-dispatch-1", "做需求分析")

        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(self._task()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
