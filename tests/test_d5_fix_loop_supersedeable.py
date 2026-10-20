"""Tests for D5 fix-loop supersedeable status set and single source of truth for task state transitions (SC-001, SC-002, SC-014)."""

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.transitions import (
    TASK_TRANSITIONS,
    ACTIVE_TASK_STATUSES,
    COMPLETED_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
)


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _load_module("herdr_task_d5_test", HERDR_ROOT / "bin" / "herdr-task")
_ctl = _load_module(
    "herdr_controller_d5_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)


def _resp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _task(task_id="t1", status="paused", stage="review", workflow_id="wf-1", **extra):
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "node": stage,
        "stage": stage,
        "status": status,
        "status_history": [],
        "agent": "codex",
        "pane_id": "wA:pZ",
    }
    task.update(extra)
    return task


class TestTaskTransitionsSingleSourceOfTruth(unittest.TestCase):
    """SC-001 & SC-002: Verification that TASK_TRANSITIONS is the single source of truth."""

    def test_transitions_has_15_statuses(self):
        # 15 statuses: pending, dispatched, working, blocked, paused, agent_done,
        # rework, interrupted, completed, committed, integrated, cleanup_ready,
        # cleaned, failed, superseded.
        self.assertEqual(len(TASK_TRANSITIONS), 15)

    def test_bin_herdr_task_uses_canonical_transitions(self):
        # bin/herdr-task must import directly from herdr.transitions
        self.assertIs(_ht.TRANSITIONS, TASK_TRANSITIONS)
        self.assertIs(_ht.ACTIVE_TASK_STATUSES, ACTIVE_TASK_STATUSES)
        self.assertIs(_ht.COMPLETED_TASK_STATUSES, COMPLETED_TASK_STATUSES)
        self.assertIs(_ht.TERMINAL_STATUSES, TERMINAL_TASK_STATUSES)

    def test_paused_and_interrupted_allow_superseded(self):
        self.assertIn("superseded", TASK_TRANSITIONS["paused"])
        self.assertIn("superseded", TASK_TRANSITIONS["interrupted"])


class TestD5FixLoopSupersedeable(unittest.TestCase):
    """SC-014: Verification that FIX_LOOP_SUPERSEDEABLE covers paused and interrupted."""

    def test_fix_loop_supersedeable_contains_paused_and_interrupted(self):
        self.assertIn("paused", _ctl.FIX_LOOP_SUPERSEDEABLE)
        self.assertIn("interrupted", _ctl.FIX_LOOP_SUPERSEDEABLE)

    def test_all_fix_loop_supersedeable_statuses_are_valid_in_task_transitions(self):
        # Invariant: Every status in FIX_LOOP_SUPERSEDEABLE must be capable of transitioning to 'superseded'
        allowed_supersede_sources = {
            status for status, next_statuses in TASK_TRANSITIONS.items()
            if "superseded" in next_statuses
        }
        for status in _ctl.FIX_LOOP_SUPERSEDEABLE:
            self.assertIn(
                status,
                allowed_supersede_sources,
                f"Status '{status}' in FIX_LOOP_SUPERSEDEABLE cannot transition to 'superseded' in TASK_TRANSITIONS!",
            )

    def test_invalidate_single_task_supersedes_paused_and_interrupted(self):
        superseded_calls = []

        def fake_run(cmd, **kwargs):
            if "supersede" in cmd:
                superseded_calls.append((cmd[cmd.index("supersede") + 1], cmd))
            return _resp(0)

        tasks_map = {
            "t-paused": _task("t-paused", status="paused"),
            "t-interrupted": _task("t-interrupted", status="interrupted"),
            "t-pending": _task("t-pending", status="pending"),
        }

        with patch.object(_ctl, "get_task", side_effect=lambda tid: tasks_map.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=fake_run):
            # Test paused task: successfully superseded
            ok_paused, err_paused = _ctl._invalidate_single_task("t-paused", "review")
            self.assertTrue(ok_paused)
            self.assertEqual(err_paused, "")

            # Test interrupted task: successfully superseded
            ok_interrupted, err_interrupted = _ctl._invalidate_single_task("t-interrupted", "review")
            self.assertTrue(ok_interrupted)
            self.assertEqual(err_interrupted, "")

            # Test pending task: not in FIX_LOOP_SUPERSEDEABLE, should be skipped
            ok_pending, err_pending = _ctl._invalidate_single_task("t-pending", "review")
            self.assertFalse(ok_pending)
            self.assertEqual(err_pending, "status_not_supersedeable:pending")

        called_ids = [cid for cid, _ in superseded_calls]
        self.assertIn("t-paused", called_ids)
        self.assertIn("t-interrupted", called_ids)
        self.assertNotIn("t-pending", called_ids)

    def test_invalidate_for_fix_loop_includes_paused_and_interrupted(self):
        superseded_calls = []

        def fake_run(cmd, **kwargs):
            if "supersede" in cmd:
                superseded_calls.append(cmd[cmd.index("supersede") + 1])
            return _resp(0)

        tasks = [
            _task("t-paused", status="paused", stage="review"),
            _task("t-interrupted", status="interrupted", stage="review"),
            _task("t-working", status="working", stage="review"),
            _task("t-pending", status="pending", stage="review"),
        ]

        patchers = [
            patch.object(_ctl, "load_tasks", return_value=tasks),
            patch.object(
                _ctl, "get_task",
                side_effect=lambda tid: next((t for t in tasks if t["task_id"] == tid), None),
            ),
            patch.object(_ctl.subprocess, "run", side_effect=fake_run),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        invalidated = _ctl.invalidate_for_fix_loop("wf-1", "review", {"nodes": []})

        # t-paused, t-interrupted, t-working are superseded
        self.assertIn("t-paused", invalidated)
        self.assertIn("t-interrupted", invalidated)
        self.assertIn("t-working", invalidated)
        # t-pending is NOT supersedeable directly by fix-loop and must be skipped
        self.assertNotIn("t-pending", invalidated)
        self.assertEqual(sorted(superseded_calls), ["t-interrupted", "t-paused", "t-working"])


if __name__ == "__main__":
    unittest.main()
