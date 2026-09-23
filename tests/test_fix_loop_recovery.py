#!/opt/homebrew/bin/python3
"""Tests for fix-loop dead-stall recovery (herdr/fix_loop.py).

Covers: notification persistence/redelivery (A), loop budget + same-verdict
escape (B), invalidation-latch advance guard (C), and old-data compat.
"""

import sys
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.fix_loop import (
    fix_loop_exhausted,
    is_repeat_verdict,
    latch_blocks_advance,
    redelivery_due,
    redelivery_handled,
    summarize_fix_loop_item,
    verdict_fingerprint,
)


def _task(task_id, node, status, updated_at=0.0, verdict=""):
    task = {
        "task_id": task_id,
        "node": node,
        "stage": node,
        "status": status,
        "updated_at": updated_at,
    }
    if verdict:
        task["stage_verdict"] = verdict
    return task


class FixLoopBudgetTest(unittest.TestCase):
    def test_at_budget_escalates(self):
        self.assertTrue(fix_loop_exhausted(3, 3))
        self.assertTrue(fix_loop_exhausted(5, 3))

    def test_below_budget_continues(self):
        self.assertFalse(fix_loop_exhausted(0, 3))
        self.assertFalse(fix_loop_exhausted(2, 3))

    def test_odd_inputs_fall_back_safe(self):
        self.assertFalse(fix_loop_exhausted(None, 3))
        self.assertFalse(fix_loop_exhausted(1, None))
        self.assertFalse(fix_loop_exhausted("x", "y"))


class VerdictFingerprintTest(unittest.TestCase):
    def test_same_blockers_same_fingerprint_regardless_of_order(self):
        blockers_a = [
            {"task_id": "t-1", "note": "missing engine"},
            {"task_id": "t-2", "note": "no e2e"},
        ]
        blockers_b = list(reversed(blockers_a))
        self.assertEqual(
            verdict_fingerprint("branch-x", blockers_a),
            verdict_fingerprint("branch-x", blockers_b),
        )

    def test_branch_or_note_change_changes_fingerprint(self):
        blockers = [{"task_id": "t-1", "note": "missing engine"}]
        base = verdict_fingerprint("branch-x", blockers)
        self.assertNotEqual(
            base, verdict_fingerprint("branch-y", blockers)
        )
        self.assertNotEqual(
            base,
            verdict_fingerprint(
                "branch-x", [{"task_id": "t-1", "note": "missing CLI"}]
            ),
        )

    def test_repeat_detection(self):
        fp = verdict_fingerprint("b", [{"task_id": "t", "note": "n"}])
        self.assertTrue(is_repeat_verdict(fp, fp))
        self.assertFalse(is_repeat_verdict(fp, None))
        self.assertFalse(is_repeat_verdict(fp, "other"))
        self.assertFalse(is_repeat_verdict("", ""))
        self.assertFalse(is_repeat_verdict(None, fp))


class InvalidationLatchTest(unittest.TestCase):
    def test_stale_completion_blocks(self):
        tasks = [_task("t1", "implementation", "cleaned", updated_at=100.0)]
        self.assertTrue(
            latch_blocks_advance(tasks, "implementation", latch_ts=200.0)
        )

    def test_fresh_completion_clears(self):
        tasks = [
            _task("t1", "implementation", "cleaned", updated_at=100.0),
            _task("t2", "implementation", "cleaned", updated_at=300.0),
        ]
        self.assertFalse(
            latch_blocks_advance(tasks, "implementation", latch_ts=200.0)
        )

    def test_superseded_never_counts_as_redo(self):
        tasks = [
            _task("t1", "implementation", "cleaned", updated_at=100.0),
            _task("t2", "implementation", "superseded", updated_at=300.0),
        ]
        self.assertTrue(
            latch_blocks_advance(tasks, "implementation", latch_ts=200.0)
        )

    def test_no_latch_never_blocks(self):
        tasks = [_task("t1", "implementation", "cleaned", updated_at=100.0)]
        self.assertFalse(
            latch_blocks_advance(tasks, "implementation", latch_ts=0)
        )
        self.assertFalse(
            latch_blocks_advance(tasks, "implementation", latch_ts=None)
        )


class RedeliveryTest(unittest.TestCase):
    def test_due_when_retry_time_passed(self):
        self.assertTrue(redelivery_due({"next_retry_at": 100.0}, now=200.0))

    def test_not_due_in_future(self):
        self.assertFalse(redelivery_due({"next_retry_at": 300.0}, now=200.0))

    def test_missing_episode_never_due(self):
        self.assertFalse(redelivery_due(None, now=200.0))
        self.assertFalse(redelivery_due({}, now=200.0))

    def test_handled_when_retry_node_progressed(self):
        tasks = [
            _task("fix-1", "implementation", "working", updated_at=300.0),
        ]
        self.assertTrue(
            redelivery_handled(
                tasks, "implementation", first_seen_at=200.0
            )
        )

    def test_not_handled_without_progress(self):
        tasks = [
            _task("fix-1", "implementation", "superseded", updated_at=300.0),
        ]
        self.assertFalse(
            redelivery_handled(
                tasks, "implementation", first_seen_at=200.0
            )
        )
        self.assertFalse(redelivery_handled([], "implementation", 200.0))
        self.assertFalse(
            redelivery_handled(tasks, "implementation", None)
        )


class SummarizeBoundedTest(unittest.TestCase):
    def test_long_notes_truncated(self):
        item = {
            "workflow_id": "wf-1",
            "gate_stage": "test",
            "retry_node": "implementation",
            "loop_count": 4,
            "max_loops": 3,
            "suggested_branch": "b",
            "blockers": [
                {"task_id": "t-1", "note": "x" * 5000},
            ],
        }
        summary = summarize_fix_loop_item(item, budget=500)
        dumped = str(summary)
        self.assertLessEqual(len(dumped), 1500)
        self.assertEqual(summary["workflow_id"], "wf-1")
        self.assertEqual(summary["loop_count"], 4)


class ControllerWiringTest(unittest.TestCase):
    """Assembly: real controller functions, isolated files, mocked edges."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        import queue as _queue
        from unittest.mock import patch as _patch

        self._patch = _patch
        spec = importlib.util.spec_from_loader(
            "herdr_controller_recovery_test",
            importlib.machinery.SourceFileLoader(
                "herdr_controller_recovery_test",
                str(HERDR_ROOT / "services" / "herdr-controller.py"),
            ),
        )
        self.ctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ctl)
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-fixloop-")
        self.root = Path(self.tmp.name)
        self.ctl.STAGE_STATE_FILE = str(self.root / "stage-state.json")
        from herdr.liveness import EpisodeStore

        self.ctl._attention_store = EpisodeStore(
            str(self.root / "attention.json")
        )
        self.queue = _queue.Queue()
        self.patches = [
            _patch.object(self.ctl, "coordinator_queue", new=self.queue),
            _patch.object(
                self.ctl, "latest_branch_for_node", return_value="agent/b"
            ),
        ]
        for started in self.patches:
            started.start()

    def tearDown(self):
        for started in reversed(self.patches):
            started.stop()
        self.tmp.cleanup()

    def _write_stage(self, state):
        (self.root / "stage-state.json").write_text(
            __import__("json").dumps(state), encoding="utf-8"
        )

    def _blocked_task(self, task_id="t-blocked", note="missing engine"):
        return {
            "task_id": task_id,
            "workflow_id": "wf-1",
            "node": "test",
            "stage": "test",
            "status": "completed",
            "stage_verdict": "blocked",
            "stage_verdict_note": note,
            "updated_at": 1000.0,
        }

    def _cfg(self):
        return {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
            ]
        }

    def _gate(self, max_loops=3):
        return {"retry_node": "implementation", "max_loops": max_loops}

    def test_budget_exhausted_escalates_without_invalidate(self):
        self._write_stage({"wf-1|fixloop|implementation": 3})
        with self._patch.object(
            self.ctl, "load_tasks", return_value=[self._blocked_task()]
        ), self._patch.object(
            self.ctl, "invalidate_for_fix_loop"
        ) as invalidated:
            self.ctl.handle_fix_loop(
                "wf-1", "test", self._gate(3), self._cfg()
            )
        invalidated.assert_not_called()
        item = self.queue.get_nowait()
        self.assertTrue(item["exhausted"])
        self.assertEqual(item["escalation_reason"], "max_loops_exhausted")
        episode = self.ctl._attention_store.get(
            "wf-1:fix_loop_exhausted:test"
        )
        self.assertIsNotNone(episode)
        self.assertEqual(episode["reason"], "max_loops_exhausted")

    def test_repeat_verdict_escalates(self):
        from herdr.fix_loop import verdict_fingerprint

        fp = verdict_fingerprint(
            "agent/b", [{"task_id": "t-blocked", "note": "missing engine"}]
        )
        self._write_stage(
            {
                "wf-1|fixloop|implementation": 1,
                "wf-1|fixloop|implementation|fp": fp,
            }
        )
        with self._patch.object(
            self.ctl, "load_tasks", return_value=[self._blocked_task()]
        ), self._patch.object(
            self.ctl, "invalidate_for_fix_loop"
        ) as invalidated:
            self.ctl.handle_fix_loop(
                "wf-1", "test", self._gate(3), self._cfg()
            )
        invalidated.assert_not_called()
        item = self.queue.get_nowait()
        self.assertTrue(item["exhausted"])
        self.assertEqual(item["escalation_reason"], "repeat_verdict")

    def test_new_verdict_after_exhaustion_reescalates(self):
        self._write_stage({"wf-1|fixloop|implementation": 3})
        blocked = self._blocked_task(note="missing engine")
        with self._patch.object(
            self.ctl, "load_tasks", return_value=[blocked]
        ), self._patch.object(
            self.ctl, "invalidate_for_fix_loop"
        ) as invalidated:
            self.ctl.handle_fix_loop(
                "wf-1", "test", self._gate(3), self._cfg()
            )
        invalidated.assert_not_called()
        first = self.queue.get_nowait()
        self.assertTrue(first["exhausted"])
        changed = self._blocked_task(note="missing CLI instead")
        with self._patch.object(
            self.ctl, "load_tasks", return_value=[changed]
        ), self._patch.object(
            self.ctl, "invalidate_for_fix_loop"
        ) as invalidated2:
            self.ctl.handle_fix_loop(
                "wf-1", "test", self._gate(3), self._cfg()
            )
        invalidated2.assert_not_called()
        second = self.queue.get_nowait()
        self.assertTrue(second["exhausted"])
        self.assertNotEqual(
            first["blockers"], second["blockers"]
        )
        with self._patch.object(
            self.ctl, "load_tasks", return_value=[changed]
        ), self._patch.object(
            self.ctl, "invalidate_for_fix_loop"
        ) as invalidated3:
            self.ctl.handle_fix_loop(
                "wf-1", "test", self._gate(3), self._cfg()
            )
        invalidated3.assert_not_called()
        self.assertTrue(self.queue.empty())

    def test_normal_loop_invalidates_and_sets_latch(self):
        self._write_stage({})
        with self._patch.object(
            self.ctl, "load_tasks", return_value=[self._blocked_task()]
        ), self._patch.object(
            self.ctl,
            "invalidate_for_fix_loop",
            return_value=["t-blocked"],
        ):
            self.ctl.handle_fix_loop(
                "wf-1", "test", self._gate(3), self._cfg()
            )
        item = self.queue.get_nowait()
        self.assertFalse(item.get("exhausted"))
        self.assertEqual(item["loop_count"], 1)
        state = __import__("json").loads(
            (self.root / "stage-state.json").read_text()
        )
        self.assertEqual(state["wf-1|fixloop|implementation"], 1)
        latch = state["wf-1|fixloop|implementation|pending_redo"]
        self.assertGreater(latch["ts"], 0)
        self.assertEqual(latch["gate"], "test")
        self.assertTrue(state["wf-1|fixloop|implementation|fp"])

    def test_latch_guard_blocks_and_clears(self):
        import time as _time

        latch_ts = _time.time() - 10
        self._write_stage(
            {
                "wf-1|fixloop|implementation|pending_redo": {
                    "ts": latch_ts,
                    "gate": "test",
                }
            }
        )
        self.ctl._attention_store.upsert(
            "wf-1:fix_loop_exhausted:test",
            {
                "task_id": "fix_loop:test:implementation",
                "workflow_id": "wf-1",
                "event_type": "fix_loop",
                "reason": "max_loops_exhausted",
            },
        )
        stale = [
            {
                "task_id": "t1",
                "workflow_id": "wf-1",
                "node": "implementation",
                "stage": "implementation",
                "status": "cleaned",
                "updated_at": latch_ts - 100,
            }
        ]
        self.assertTrue(
            self.ctl._fix_loop_latch_blocks("wf-1", "implementation", stale)
        )
        fresh = stale + [
            {
                "task_id": "t2",
                "workflow_id": "wf-1",
                "node": "implementation",
                "stage": "implementation",
                "status": "cleaned",
                "updated_at": latch_ts + 100,
            }
        ]
        self.assertFalse(
            self.ctl._fix_loop_latch_blocks("wf-1", "implementation", fresh)
        )
        state = __import__("json").loads(
            (self.root / "stage-state.json").read_text()
        )
        self.assertNotIn(
            "wf-1|fixloop|implementation|pending_redo", state
        )
        self.assertIsNone(
            self.ctl._attention_store.get("wf-1:fix_loop_exhausted:test")
        )

    def test_busy_timeout_persists_for_redelivery(self):
        item = {
            "kind": "fix_loop",
            "workflow_id": "wf-1",
            "gate_stage": "test",
            "retry_node": "implementation",
            "blockers": [{"task_id": "t", "note": "n"}],
            "invalidated": ["t"],
            "loop_count": 1,
            "max_loops": 3,
            "suggested_branch": "agent/b",
        }
        with self._patch.object(
            self.ctl, "coordinator_pane_for_workflow", return_value="wZ:p1"
        ), self._patch.object(
            self.ctl, "coordinator_status", return_value="busy"
        ), self._patch.object(
            self.ctl, "project_for_workflow", return_value={}
        ), self._patch.object(
            self.ctl, "build_fix_loop_message", return_value="MSG"
        ), self._patch(
            "time.sleep", return_value=None
        ):
            self.ctl._handle_fix_loop_item(item)
        self.assertTrue(self.queue.empty())
        episode = self.ctl._attention_store.get("wf-1:fix_loop:test")
        self.assertIsNotNone(episode)
        self.assertEqual(episode["reason"], "coordinator_busy")
        summary = __import__("json").loads(episode["detail"])
        self.assertEqual(summary["retry_node"], "implementation")

    def test_redelivery_refires_and_skips_when_handled(self):
        import json as _json
        import time as _time

        summary = {
            "workflow_id": "wf-1",
            "gate_stage": "test",
            "retry_node": "implementation",
            "loop_count": 1,
            "max_loops": 3,
            "suggested_branch": "agent/b",
            "blockers": [],
            "exhausted": False,
        }
        self.ctl._attention_store.upsert(
            "wf-1:fix_loop:test",
            {
                "task_id": "fix_loop:test:implementation",
                "workflow_id": "wf-1",
                "event_type": "fix_loop",
                "reason": "coordinator_busy",
                "attempts": 1,
                "first_seen_at": _time.time() - 100,
                "next_retry_at": _time.time() - 10,
                "detail": _json.dumps(summary),
            },
        )
        with self._patch.object(
            self.ctl, "workflow_closed", return_value=False
        ), self._patch.object(
            self.ctl, "coordinator_status", return_value="idle"
        ), self._patch.object(
            self.ctl, "load_tasks", return_value=[]
        ):
            self.assertTrue(self.ctl.redeliver_pending_fix_loop("wf-1"))
        item = self.queue.get_nowait()
        self.assertTrue(item.get("redelivered"))
        self.assertEqual(item["retry_node"], "implementation")
        with self._patch.object(
            self.ctl, "workflow_closed", return_value=False
        ), self._patch.object(
            self.ctl, "coordinator_status", return_value="idle"
        ), self._patch.object(
            self.ctl,
            "load_tasks",
            return_value=[
                {
                    "task_id": "fix-9",
                    "workflow_id": "wf-1",
                    "node": "implementation",
                    "stage": "implementation",
                    "status": "working",
                    "updated_at": _time.time(),
                }
            ],
        ):
            self.assertFalse(self.ctl.redeliver_pending_fix_loop("wf-1"))
        self.assertTrue(self.queue.empty())
        self.assertIsNone(
            self.ctl._attention_store.get("wf-1:fix_loop:test")
        )


if __name__ == "__main__":
    unittest.main()
