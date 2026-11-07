"""RED tests for PR #409 P1 issue: Phase 2 Partial Invalidation Obligation Loss.

Reproduces P1 defects on HEAD (f3aced5946ebd8152cbf503270a4454956d654eb):
When Phase 2 partial invalidation occurs (e.g., blocker gate task is superseded,
but downstream task like 'committed' cannot be superseded):
1. The superseded gate task loses its 'blocked' effect in gate_verdict.
2. The controller has no persistent record of the pending invalidation obligation.
3. In subsequent sweeps, the controller fails to detect the pending obligation,
   leaving the workflow stalled or advancing prematurely without human escalation.
"""

import importlib.machinery
import importlib.util
import json
import queue
import subprocess
import sys
import tempfile
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


_ht = _load_module("herdr_task_p1_red", HERDR_ROOT / "bin" / "herdr-task")
_ctl = _load_module(
    "herdr_controller_p1_red",
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


class TestFixLoopPhase2ObligationP1(unittest.TestCase):
    """P1 reproduction tests: Obligation persistence, recovery, and sweep progression."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-p1-")
        self.stage_file = Path(self.tmp.name) / "stage-state.json"
        self.stage_file.write_text("{}")
        _ctl.STAGE_STATE_FILE = str(self.stage_file)
        self.addCleanup(self.tmp.cleanup)

        # Clear coordinator queue
        while not _ctl.coordinator_queue.empty():
            try:
                _ctl.coordinator_queue.get_nowait()
            except queue.Empty:
                break

        self.finalized_calls = []
        self.superseded_calls = []
        self.tasks_store = {}

    def _fake_subprocess_run(self, cmd, **kwargs):
        subcmd = cmd[1]
        tid = cmd[2]
        if subcmd == "finalize":
            self.finalized_calls.append(tid)
            st = self.tasks_store[tid]["status"]
            while st in _ht.FINALIZE_ADVANCE:
                st = _ht.FINALIZE_ADVANCE[st]
            self.tasks_store[tid]["status"] = st
            return subprocess.CompletedProcess(cmd, 0, "", "")
        elif subcmd == "supersede":
            self.superseded_calls.append(tid)
            self.tasks_store[tid]["status"] = "superseded"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        elif subcmd == "rework":
            self.tasks_store[tid]["status"] = "rework"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_legacy_mode_partial_invalidation_persists_obligation(self):
        """Scenario 1: Legacy mode partial invalidation.

        Blocker task 'gate-1' is superseded, but 'downstream-committed' cannot be superseded.
        The system must persist the pending invalidation obligation in stage_state.
        """
        self.tasks_store = {
            "gate-1": _task("gate-1", status="blocked", stage_verdict="blocked", stage_verdict_note="B1"),
            "downstream-committed": _task("downstream-committed", status="committed"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.handle_fix_loop(
                "wf-1", "review", {"retry_node": "implementation", "max_loops": 3}, cfg
            )

        # gate-1 was superseded:
        self.assertEqual(self.tasks_store["gate-1"]["status"], "superseded")
        # downstream-committed remains committed:
        self.assertEqual(self.tasks_store["downstream-committed"]["status"], "committed")

        # Current HEAD failure: no durable obligation persisted!
        obligation = _ctl.get_invalidation_obligation("wf-1", "review")
        self.assertIsNotNone(
            obligation,
            "Durable invalidation obligation must be persisted when Phase 2 invalidation has pending tasks",
        )
        self.assertIn("downstream-committed", obligation["pending_tasks"])

    def test_selective_mode_partial_invalidation_persists_obligation(self):
        """Scenario 2: Selective mode Phase 1 completes, Phase 2 partial.

        Target task is reworked in Phase 1, blocker is superseded in Phase 2, but committed task fails.
        Obligation must be persisted with pending tasks.
        """
        self.tasks_store = {
            "target-impl": _task("target-impl", status="working", node="implementation", stage="implementation"),
            "gate-1": _task("gate-1", status="blocked", stage_verdict="blocked", stage_verdict_note="B1"),
            "downstream-committed": _task("downstream-committed", status="committed", node="review", stage="review"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ],
            "selective_replan": {"retry_node": "implementation"},
        }

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "_resolve_selective_replan", return_value=(["target-impl"], {"target_lineage_roots": ["target-impl"]})), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.handle_fix_loop(
                "wf-1", "review", {"retry_node": "implementation", "max_loops": 3}, cfg
            )

        obligation = _ctl.get_invalidation_obligation("wf-1", "review")
        self.assertIsNotNone(obligation, "Selective mode must persist obligation on Phase 2 partial failure")
        self.assertIn("downstream-committed", obligation["pending_tasks"])

    def test_merged_fix_loop_partial_invalidation_persists_obligation(self):
        """Scenario 3: Multi-gate merged fix-loop partial failure persists obligation."""
        self.tasks_store = {
            "test-1": _task("test-1", status="blocked", node="test", stage="test", stage_verdict="blocked", stage_verdict_note="T1"),
            "review-1": _task("review-1", status="blocked", node="review", stage="review", stage_verdict="blocked", stage_verdict_note="R1"),
            "downstream-committed": _task("downstream-committed", status="committed", node="review", stage="review"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            # Run merged fix loop
            _ctl._handle_merged_fix_loops(
                "wf-1",
                "implementation",
                [("test", {"retry_node": "implementation"}), ("review", {"retry_node": "implementation"})],
                cfg,
            )

        # Obligation must be persisted for the merged gates or affected gate
        obligations = _ctl.list_invalidation_obligations("wf-1")
        self.assertTrue(
            bool(obligations),
            "Merged fix-loop must persist obligation when Phase 2 invalidation has pending tasks",
        )

    def test_controller_restart_discovers_unresolved_obligation(self):
        """Scenario 4: Controller restart recovery.

        After restart (clean in-memory state), reading from disk stage-state discovers the obligation.
        """
        # Directly write an obligation into stage-state.json
        state = {
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation", "max_loops": 3},
                "pending_tasks": ["downstream-committed"],
                "failed_tasks": {"downstream-committed": "status_not_supersedeable:committed"},
                "applied_tasks": ["gate-1"],
            }
        }
        self.stage_file.write_text(json.dumps(state))

        # New controller instance read
        obligation = _ctl.get_invalidation_obligation("wf-1", "review")
        self.assertIsNotNone(obligation)
        self.assertEqual(obligation["pending_tasks"], ["downstream-committed"])

        # And list_invalidation_obligations finds it
        all_ob = _ctl.list_invalidation_obligations("wf-1")
        self.assertIn("review", all_ob)

    def test_superseded_blocker_with_pending_obligation_drives_sweep(self):
        """Scenario 5: Original blocker is superseded, but pending obligation prevents

        workflow completion and drives subsequent sweep.
        """
        self.tasks_store = {
            "impl-1": _task("impl-1", status="cleaned", node="implementation", stage="implementation"),
            "gate-1": _task("gate-1", status="superseded", node="review", stage="review", stage_verdict="blocked"),
            "downstream-committed": _task("downstream-committed", status="committed", node="review", stage="review"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }

        # Blocker task is superseded, so gate_verdict returns None!
        self.assertIsNone(_ctl.gate_verdict("wf-1", "review"))

        # But obligation is persisted:
        state = {
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation", "max_loops": 3},
                "pending_tasks": ["downstream-committed"],
                "failed_tasks": {"downstream-committed": "status_not_supersedeable:committed"},
                "applied_tasks": ["gate-1"],
            }
        }
        self.stage_file.write_text(json.dumps(state))

        # Check that check_stage_advancement detects the pending obligation and does NOT close workflow
        closed = []
        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "workflow_config_for", return_value=cfg), \
             patch.object(_ctl, "maybe_close_completed_workflow", side_effect=lambda wid: closed.append(wid)), \
             patch.object(_ctl, "handle_fix_loop") as mock_handle:
            _ctl.check_workflow_stage_advance("wf-1")

        self.assertNotIn("wf-1", closed, "Workflow must NOT close while invalidation obligation is pending")
        mock_handle.assert_called()

    def test_repeated_obligation_execution_is_idempotent(self):
        """Scenario 6: Repeated sweep execution does not duplicate side effects."""
        self.tasks_store = {
            "gate-1": _task("gate-1", status="superseded", stage_verdict="blocked"),
            "downstream-committed": _task("downstream-committed", status="committed"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }

        state = {
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation", "max_loops": 3},
                "pending_tasks": ["downstream-committed"],
                "failed_tasks": {"downstream-committed": "status_not_supersedeable:committed"},
                "applied_tasks": ["gate-1"],
            }
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            # Sweep executes handle_fix_loop
            _ctl.handle_fix_loop(
                "wf-1", "review", {"retry_node": "implementation", "max_loops": 3}, cfg
            )

        # gate-1 was already superseded, must NOT be superseded again:
        self.assertEqual(self.superseded_calls, [])
        # coordinator queue must be empty (no duplicate events):
        self.assertTrue(_ctl.coordinator_queue.empty())
        # The obligation must still remain active with downstream-committed pending
        ob = _ctl.get_invalidation_obligation("wf-1", "review")
        self.assertIsNotNone(ob)
        self.assertIn("downstream-committed", ob["pending_tasks"])

    def test_obligation_cleared_only_after_all_tasks_resolved(self):
        """Scenario 7: Obligation remains while committed task persists, and is cleared

        once committed task is resolved.
        """
        self.tasks_store = {
            "gate-1": _task("gate-1", status="superseded", stage_verdict="blocked"),
            "downstream-committed": _task("downstream-committed", status="committed"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }

        # Step 1: While downstream is committed, obligation cannot be cleared
        state = {
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation", "max_loops": 3},
                "pending_tasks": ["downstream-committed"],
                "failed_tasks": {"downstream-committed": "status_not_supersedeable:committed"},
                "applied_tasks": ["gate-1"],
            }
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.handle_fix_loop(
                "wf-1", "review", {"retry_node": "implementation", "max_loops": 3}, cfg
            )

        self.assertIsNotNone(_ctl.get_invalidation_obligation("wf-1", "review"))

        # Step 2: Now human/system resolves downstream-committed (e.g. advances to cleaned or superseded)
        self.tasks_store["downstream-committed"]["status"] = "cleaned"

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.handle_fix_loop(
                "wf-1", "review", {"retry_node": "implementation", "max_loops": 3}, cfg
            )

        # Now all tasks were applied, obligation must be CLEARED:
        self.assertIsNone(
            _ctl.get_invalidation_obligation("wf-1", "review"),
            "Obligation must be cleared once all invalidation tasks are resolved",
        )
        # And fix-loop progression resumes:
        self.assertFalse(_ctl.coordinator_queue.empty())

    def test_pending_obligation_blocks_ready_node_sweep_advance(self):
        """Scenario 8 (Adversarial Finding 1): When a gate task is superseded and an obligation

        is pending, sweep must NOT dispatch the gate as a ready node or advance downstream stages.
        """
        self.tasks_store = {
            "impl-1": _task("impl-1", status="cleaned", node="implementation", stage="implementation"),
            "gate-1": _task("gate-1", status="superseded", node="review", stage="review", stage_verdict="blocked"),
            "commit-task": _task("commit-task", status="committed", node="commit", stage="commit"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
                {"id": "commit", "depends_on": ["review"]},
            ]
        }

        state = {
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation", "max_loops": 3},
                "pending_tasks": ["commit-task"],
                "failed_tasks": {"commit-task": "status_not_supersedeable:committed"},
                "applied_tasks": ["gate-1"],
            }
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "workflow_config_for", return_value=cfg), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.check_workflow_stage_advance("wf-1")

        # 1. No stage_advance event must be queued for review or commit!
        queued_items = []
        while not _ctl.coordinator_queue.empty():
            queued_items.append(_ctl.coordinator_queue.get_nowait())

        stage_advance_events = [it for it in queued_items if it.get("kind") == "stage_advance"]
        self.assertEqual(
            stage_advance_events,
            [],
            "Sweep must NOT dispatch any stage_advance while an invalidation obligation is pending",
        )

    def test_merged_fix_loop_retry_preserves_merged_group_and_single_bump(self):
        """Scenario 9 (Adversarial Finding 2): When multiple gates are merged into a fix-loop,

        subsequent retry after resolving pending tasks must NOT execute as separate loops,
        double-bump loop counts, or emit duplicate coordinator events.
        """
        self.tasks_store = {
            "impl-1": _task("impl-1", status="cleaned", node="implementation", stage="implementation"),
            "test-1": _task("test-1", status="superseded", node="test", stage="test", stage_verdict="blocked"),
            "review-1": _task("review-1", status="superseded", node="review", stage="review", stage_verdict="blocked"),
            "downstream-task": _task("downstream-task", status="cleaned", node="review", stage="review"),
        }

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["implementation"]},
            ],
            "selective_replan": {"retry_node": "implementation", "mode": "explicit_task_targets"},
        }

        # Merged obligations recorded for both test and review
        state = {
            "wf-1|fixloop_obligation|test": {
                "workflow_id": "wf-1",
                "gate_node_id": "test",
                "merged_gates": ["review", "test"],
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation", "max_loops": 3},
                "pending_tasks": [],
                "applied_tasks": ["test-1"],
                "blockers": [{"task_id": "test-1", "note": "T1"}],
            },
            "wf-1|fixloop_obligation|review": {
                "workflow_id": "wf-1",
                "gate_node_id": "review",
                "merged_gates": ["review", "test"],
                "retry_node": "implementation",
                "gate_cfg": {"retry_node": "implementation", "max_loops": 3},
                "pending_tasks": [],
                "applied_tasks": ["review-1"],
                "blockers": [{"task_id": "review-1", "note": "R1"}],
            },
        }
        self.stage_file.write_text(json.dumps(state))

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(self.tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl, "workflow_config_for", return_value=cfg), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            _ctl.check_workflow_stage_advance("wf-1")

        # 1. Exactly ONE fix_loop event should be queued (merged):
        queued_items = []
        while not _ctl.coordinator_queue.empty():
            queued_items.append(_ctl.coordinator_queue.get_nowait())

        fix_loop_events = [it for it in queued_items if it.get("kind") == "fix_loop"]
        self.assertEqual(
            len(fix_loop_events),
            1,
            f"Merged retry must emit exactly 1 fix_loop event, but emitted {len(fix_loop_events)}",
        )
        self.assertEqual(fix_loop_events[0]["gate_stage"], "review+test")

        # 2. Loop count must be bumped ONCE (loop_count == 1, not 2):
        final_state = json.loads(self.stage_file.read_text())
        self.assertEqual(
            final_state.get("wf-1|fixloop|implementation"),
            1,
            "Merged retry must bump loop count exactly once",
        )

        # 3. Both obligations must be cleared:
        self.assertIsNone(_ctl.get_invalidation_obligation("wf-1", "test"))
        self.assertIsNone(_ctl.get_invalidation_obligation("wf-1", "review"))

    def test_clear_workflow_stage_state_removes_obligation_keys(self):
        """Scenario 10 (Adversarial Finding 3): clear_workflow_stage_state must clear

        obligation keys starting with workflow_id| pipe prefix as well.
        """
        state = {
            "wf-1:review": "queued",
            "wf-1|fixloop_obligation|review": {"workflow_id": "wf-1"},
            "wf-2:review": "queued",
            "wf-2|fixloop_obligation|review": {"workflow_id": "wf-2"},
        }
        self.stage_file.write_text(json.dumps(state))

        _ctl.clear_workflow_stage_state("wf-1")

        final_state = json.loads(self.stage_file.read_text())
        self.assertNotIn("wf-1:review", final_state)
        self.assertNotIn("wf-1|fixloop_obligation|review", final_state)
        self.assertIn("wf-2:review", final_state)
        self.assertIn("wf-2|fixloop_obligation|review", final_state)


if __name__ == "__main__":
    unittest.main()
