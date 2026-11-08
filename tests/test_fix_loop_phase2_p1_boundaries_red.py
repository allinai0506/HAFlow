"""RED tests for PR #409 P1 follow-up boundaries:

1. Partial invalidation recovery preserves target_lineage_roots in pending_redo,
   preventing unrelated tasks from prematurely lifting the redo wait latch.
2. Invalidation obligation is cleared even when all tasks are already superseded
   upon controller restart/sweep, preventing permanent workflow hang/deadlock.
"""

import importlib.machinery
import importlib.util
import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_ctl = _load_module(
    "herdr_controller",
    HERDR_ROOT / "services" / "herdr-controller.py",
)


def _task(task_id, status, node="review", stage="review", workflow_id="wf-1", **extra):
    t = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "node": node,
        "stage": stage,
        "status": status,
        "status_history": [],
        "agent": "codex",
        "pane_id": "wA:pZ",
    }
    t.update(extra)
    return t


class TestFixLoopPhase2Boundaries(unittest.TestCase):
    """Reproduction tests for the two conditional risk boundaries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-boundaries-")
        self.stage_file = Path(self.tmp.name) / "stage-state.json"
        self.stage_file.write_text("{}")
        _ctl.STAGE_STATE_FILE = str(self.stage_file)
        self.addCleanup(self.tmp.cleanup)

        while not _ctl.coordinator_queue.empty():
            try:
                _ctl.coordinator_queue.get_nowait()
            except queue.Empty:
                break

    def _fake_subprocess_run(self, cmd, *args, **kwargs):
        if len(cmd) >= 3 and cmd[1] == "supersede":
            tid = cmd[2]
            if tid in self.tasks_store:
                self.tasks_store[tid]["status"] = "superseded"
            return MagicMock(returncode=0, stdout="superseded", stderr="")
        if len(cmd) >= 3 and cmd[1] == "finalize":
            tid = cmd[2]
            if tid in self.tasks_store:
                self.tasks_store[tid]["status"] = "cleaned"
            return MagicMock(returncode=0, stdout="cleaned", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def test_selective_replan_partial_recovery_preserves_target_lineage_roots(self):
        """Boundary 1: When selective replan recovers from partial invalidation,

        target_lineage_roots MUST be preserved in pending_redo so unrelated task
        completion does NOT prematurely release the redo latch.
        """
        self.tasks_store = {
            "task-A": _task("task-A", status="superseded", node="implementation", stage="implementation", lineage_id="task-A"),
            "task-B": _task("task-B", status="completed", node="implementation", stage="implementation", lineage_id="task-B", updated_at=1500.0),
            "task-committed": _task("task-committed", status="cleaned", node="implementation", stage="implementation", lineage_id="task-committed"),
            "review-1": _task("review-1", status="superseded", node="review", stage="review", stage_verdict="blocked"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ],
            "selective_replan": {
                "retry_node": "implementation",
                "mode": "explicit_task_targets",
            },
        }

        # Obligation recorded in round 1 with target_lineage_roots
        state = {
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation"},
                "selective_ids": ["task-A", "task-committed"],
                "target_lineage_roots": ["task-A"],
                "pending_tasks": ["task-committed"],
                "applied_tasks": ["task-A"],
                "blockers": [{"task_id": "review-1", "note": "blocked on task-A"}],
                "created_at": 1000.0,
                "updated_at": 1000.0,
            }
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "workflow_config_for", return_value=cfg), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.check_workflow_stage_advance("wf-1")

        final_state = json.loads(self.stage_file.read_text())
        pending_redo = final_state.get("wf-1|fixloop|implementation|pending_redo")
        self.assertIsNotNone(pending_redo, "pending_redo must be written upon resolving pending tasks")

        # Crucial check: target_lineage_roots MUST NOT be empty []
        roots = pending_redo.get("target_lineage_roots")
        self.assertEqual(
            roots,
            ["task-A"],
            f"target_lineage_roots must be preserved as ['task-A'], but got {roots}",
        )

        # Latch check: unrelated task-B completed at 1500.0 must NOT release the latch!
        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())):
            latch_blocks = _ctl._fix_loop_latch_blocks("wf-1", "implementation")
        self.assertTrue(
            latch_blocks,
            "Redo latch must continue blocking implementation advance because task-A has not redone yet",
        )

    def test_obligation_cleared_when_all_targets_already_superseded_on_restart(self):
        """Boundary 2: When controller restarts and all pending tasks in obligation

        are already superseded, the obligation MUST be cleared from stage-state.json
        and redo MUST proceed, avoiding permanent hang/deadlock.
        """
        self.tasks_store = {
            "impl-1": _task("impl-1", status="superseded", node="implementation", stage="implementation"),
            "review-1": _task("review-1", status="superseded", node="review", stage="review", stage_verdict="blocked"),
            "task-committed": _task("task-committed", status="superseded", node="implementation", stage="implementation"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ],
        }

        # Obligation exists, but task-committed was already superseded (e.g. by operator or before crash)
        state = {
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation"},
                "pending_tasks": ["task-committed"],
                "applied_tasks": ["impl-1"],
                "blockers": [{"task_id": "review-1", "note": "blocked"}],
                "created_at": 1000.0,
                "updated_at": 1000.0,
            }
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "workflow_config_for", return_value=cfg), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.check_workflow_stage_advance("wf-1")

        final_state = json.loads(self.stage_file.read_text())

        # Crucial check 1: Obligation MUST be cleared!
        self.assertNotIn(
            "wf-1|fixloop_obligation|review",
            final_state,
            "Residual obligation must be cleared when all tasks are already superseded",
        )
        self.assertIsNone(_ctl.get_invalidation_obligation("wf-1", "review"))

        # Crucial check 2: pending_redo must be written so redo can proceed
        self.assertIn("wf-1|fixloop|implementation|pending_redo", final_state)

        # Crucial check 3: fix_loop event must be emitted
        queued = []
        while not _ctl.coordinator_queue.empty():
            queued.append(_ctl.coordinator_queue.get_nowait())
        fix_events = [e for e in queued if e.get("kind") == "fix_loop"]
        self.assertEqual(len(fix_events), 1, "Must emit exactly one fix_loop event")
        self.assertIn("impl-1", fix_events[0]["invalidated"])

    def test_merged_fix_loop_partial_recovery_preserves_target_lineage_roots(self):
        """Boundary 1 (Merged): Multiple gates merged in fix-loop must also preserve

        target_lineage_roots upon partial invalidation recovery.
        """
        self.tasks_store = {
            "task-A": _task("task-A", status="superseded", node="implementation", stage="implementation", lineage_id="task-A"),
            "task-B": _task("task-B", status="completed", node="implementation", stage="implementation", lineage_id="task-B", updated_at=1500.0),
            "task-committed": _task("task-committed", status="cleaned", node="implementation", stage="implementation", lineage_id="task-committed"),
            "test-1": _task("test-1", status="superseded", node="test", stage="test", stage_verdict="blocked"),
            "review-1": _task("review-1", status="superseded", node="review", stage="review", stage_verdict="blocked"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["implementation"]},
            ],
            "selective_replan": {
                "retry_node": "implementation",
                "mode": "explicit_task_targets",
            },
        }

        state = {
            "wf-1|fixloop_obligation|test": {
                "workflow_id": "wf-1",
                "gate_node_id": "test",
                "merged_gates": ["review", "test"],
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation"},
                "selective_ids": ["task-A", "task-committed"],
                "target_lineage_roots": ["task-A"],
                "pending_tasks": ["task-committed"],
                "applied_tasks": ["task-A"],
                "blockers": [{"task_id": "test-1", "note": "T1"}],
            },
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "merged_gates": ["review", "test"],
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation"},
                "selective_ids": ["task-A", "task-committed"],
                "target_lineage_roots": ["task-A"],
                "pending_tasks": ["task-committed"],
                "applied_tasks": ["task-A"],
                "blockers": [{"task_id": "review-1", "note": "R1"}],
            },
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "workflow_config_for", return_value=cfg), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.check_workflow_stage_advance("wf-1")

        final_state = json.loads(self.stage_file.read_text())
        pending_redo = final_state.get("wf-1|fixloop|implementation|pending_redo")
        self.assertIsNotNone(pending_redo)
        self.assertEqual(pending_redo.get("target_lineage_roots"), ["task-A"])

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())):
            latch_blocks = _ctl._fix_loop_latch_blocks("wf-1", "implementation")
        self.assertTrue(latch_blocks)

    def test_merged_fix_loop_obligation_cleared_when_all_targets_already_superseded_on_restart(self):
        """Boundary 2 (Merged): Merged obligations must both be cleared when all tasks

        are already superseded upon restart, and single merged fix_loop event emitted.
        """
        self.tasks_store = {
            "impl-1": _task("impl-1", status="superseded", node="implementation", stage="implementation"),
            "test-1": _task("test-1", status="superseded", node="test", stage="test", stage_verdict="blocked"),
            "review-1": _task("review-1", status="superseded", node="review", stage="review", stage_verdict="blocked"),
            "task-committed": _task("task-committed", status="superseded", node="implementation", stage="implementation"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["implementation"]},
            ],
            "selective_replan": {
                "retry_node": "implementation",
                "mode": "explicit_task_targets",
            },
        }

        state = {
            "wf-1|fixloop_obligation|test": {
                "workflow_id": "wf-1",
                "gate_node_id": "test",
                "merged_gates": ["review", "test"],
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation"},
                "pending_tasks": ["task-committed"],
                "applied_tasks": ["impl-1"],
                "blockers": [{"task_id": "test-1", "note": "T1"}],
            },
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "merged_gates": ["review", "test"],
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation"},
                "pending_tasks": ["task-committed"],
                "applied_tasks": ["impl-1"],
                "blockers": [{"task_id": "review-1", "note": "R1"}],
            },
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "workflow_config_for", return_value=cfg), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.check_workflow_stage_advance("wf-1")

        final_state = json.loads(self.stage_file.read_text())
        self.assertNotIn("wf-1|fixloop_obligation|test", final_state)
        self.assertNotIn("wf-1|fixloop_obligation|review", final_state)
        self.assertIn("wf-1|fixloop|implementation|pending_redo", final_state)

        queued = []
        while not _ctl.coordinator_queue.empty():
            queued.append(_ctl.coordinator_queue.get_nowait())
        fix_events = [e for e in queued if e.get("kind") == "fix_loop"]
        self.assertEqual(len(fix_events), 1)


if __name__ == "__main__":
    unittest.main()
