"""Watchdog idle-basis regressions from wf-haflow-1024-02: loop artifacts are
progress evidence.

The 2026-10-24 incident: the craft agent ground silently through evaluator
rounds for an hour. The task row never transitioned (``updated_at`` frozen at
21:16), the pane liveness probe could not confirm ``working`` for the TTY
agent, so afcf422's alive-grace never engaged and the watchdog killed a
productive loop at idle 3601s. INV-SENTINEL-05: fresh ``.herdr-loop`` artifacts
under the task's clone count as task activity — a loop that keeps writing
test logs and evaluation receipts is not idle, regardless of status
transitions or pane probes.
"""

from __future__ import annotations

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class LoopProgressLivenessTest(unittest.TestCase):
    def setUp(self):
        self.sentinel = importlib.import_module("services.herdr-sentinel")
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.clone = Path(self.tmp.name) / "clone"
        loop_dir = self.clone / ".herdr-loop"
        (loop_dir / "logs").mkdir(parents=True)
        (loop_dir / "GOAL.md").write_text("# goal", encoding="utf-8")
        (loop_dir / "STATE.md").write_text("# state", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self, now, idle, *, clone_path=None):
        return {
            "task_id": "wf-1024-02-craft-auto",
            "workflow_id": "wf-1024-02",
            "node": "craft",
            "stage": "craft",
            "status": "working",
            "pane_id": "w19:p7N",
            "clone_path": str(self.clone) if clone_path is None else clone_path,
            "updated_at": now - idle,
            "version": 2,
        }

    def _touch_fresh(self, now, age):
        stamp = now - age
        loop_dir = self.clone / ".herdr-loop"
        for path in (
            loop_dir / "GOAL.md",
            loop_dir / "STATE.md",
            loop_dir / "logs" / "test.log",
        ):
            path.write_text("...", encoding="utf-8")
            os.utime(path, (stamp, stamp))

    def test_fresh_loop_artifacts_prevent_watchdog_kill(self):
        now = 100_000.0
        self._touch_fresh(now, age=300)
        task = self._task(now, idle=4000)
        state = {"stalls": {}, "timeouts": {}}

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "idle"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls([task], state, notify_fn=lambda *a: None, now=now)

        fail_mock.assert_not_called()

    def test_stale_loop_artifacts_still_fail_at_hard_timeout(self):
        now = 100_000.0
        self._touch_fresh(now, age=4000)
        task = self._task(now, idle=4000)
        state = {"stalls": {}, "timeouts": {}}

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "idle"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls([task], state, notify_fn=lambda *a: None, now=now)

        fail_mock.assert_called_once()

    def test_missing_clone_path_keeps_legacy_behaviour(self):
        now = 100_000.0
        task = self._task(now, idle=4000, clone_path="")
        state = {"stalls": {}, "timeouts": {}}

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "idle"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls([task], state, notify_fn=lambda *a: None, now=now)

        fail_mock.assert_called_once()

    def test_missing_loop_dir_keeps_legacy_behaviour(self):
        now = 100_000.0
        absent = self.clone.parent / "other-clone"
        task = self._task(now, idle=4000, clone_path=str(absent))
        state = {"stalls": {}, "timeouts": {}}

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "idle"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls([task], state, notify_fn=lambda *a: None, now=now)

        fail_mock.assert_called_once()

    def test_fresh_artifacts_reset_the_idle_basis(self):
        """Fresh progress younger than the last recorded kill must not re-fire."""
        now = 100_000.0
        self._touch_fresh(now, age=300)
        task = self._task(now, idle=4000)
        state = {"stalls": {}, "timeouts": {}}

        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "idle"},
        ), patch.object(self.sentinel, "update_statuses"):
            self.sentinel.check_task_stalls([task], state, notify_fn=lambda *a: None, now=now)

        # A later sweep with the same frozen snapshot stays quiet.
        with patch.object(
            self.sentinel,
            "_pane_delivery_evidence",
            return_value={"has_marker": True, "agent_status": "idle"},
        ), patch.object(self.sentinel, "update_statuses") as fail_mock:
            self.sentinel.check_task_stalls(
                [task], state, notify_fn=lambda *a: None, now=now + 60
            )
        fail_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
