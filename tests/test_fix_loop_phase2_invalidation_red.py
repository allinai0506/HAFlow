"""RED tests for Fix-Loop Phase 2 Invalidation.

Proves two defects on main (@ beac3fc861b2d5f4353a064b47a8509c766ac40e):
1. Integrated tasks can and should be invalidated through the existing finalize path
   (FINALIZE_ADVANCE: integrated -> cleanup_ready -> cleaned -> superseded),
   but currently fix_invalidation omits "integrated" from ("completed", "cleanup_ready"),
   causing integrated tasks to be skipped and never finalized or superseded.
2. Committed tasks (and other non-auto-converging tasks) must NOT be reported as success
   (all_targets_applied=True, pending=[], failed={}) and must NOT be silently skipped.
"""

import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
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


_ht = _load_module("herdr_task_red_test", HERDR_ROOT / "bin" / "herdr-task")
_ctl = _load_module(
    "herdr_controller_red_test",
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


class TestIntegratedTaskInvalidationThroughFinalize(unittest.TestCase):
    """Claim 1: integrated 任务能够通过现有 finalize 路径完成作废。

    In herdr-task:
        FINALIZE_ADVANCE = {
            "completed": "cleanup_ready",
            "integrated": "cleanup_ready",
            "cleanup_ready": "cleaned",
        }
    integrated tasks can advance via finalize to cleanup_ready -> cleaned,
    and cleaned can transition to superseded.
    However, fix_invalidation checks:
        if status in ("completed", "cleanup_ready"):
            ... finalize ...
    omitting "integrated", which causes integrated tasks to be skipped with:
        [FIX LOOP INVALIDATE SKIP] task=... status=integrated cannot be superseded
    """

    def setUp(self):
        self.finalized_calls = []
        self.superseded_calls = []
        self.tasks_store = {}

    def _fake_subprocess_run(self, cmd, **kwargs):
        subcmd = cmd[1]
        tid = cmd[2]
        if subcmd == "finalize":
            self.finalized_calls.append(tid)
            # Simulate real herdr-task finalize behavior advancing through FINALIZE_ADVANCE
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

    def test_integrated_downstream_task_advanced_via_finalize_and_superseded_in_phase2(self):
        """In Fix-Loop Phase 2, a downstream task in 'integrated' status must be

        advanced to cleaned via finalize, then superseded. It must NOT be skipped.
        """
        self.tasks_store = {
            "target-impl": _task("target-impl", status="working", node="implementation", stage="implementation"),
            "downstream-integrated": _task("downstream-integrated", status="integrated", node="review", stage="review"),
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
            result = _ctl.invalidate_for_fix_loop(
                "wf-1",
                "review",
                cfg,
                retry_node="implementation",
                selective_target_task_ids=["target-impl"],
            )

        # On main:
        # finalize is NEVER called for downstream-integrated (L316 only checks completed/cleanup_ready).
        # downstream-integrated is skipped (L332: status not in supersedeable).
        # These assertions will fail (RED) on main:
        self.assertIn(
            "downstream-integrated",
            self.finalized_calls,
            "integrated downstream task must be finalized via finalize command",
        )
        self.assertIn(
            "downstream-integrated",
            self.superseded_calls,
            "integrated downstream task must be superseded after finalize advances it to cleaned",
        )
        self.assertIn(
            "downstream-integrated",
            result,
            "downstream-integrated must be included in the invalidated result list",
        )
        self.assertEqual(
            self.tasks_store["downstream-integrated"]["status"],
            "superseded",
            "downstream-integrated final status must be superseded",
        )

    def test_integrated_task_invalidated_via_single_task_path(self):
        """_invalidate_single_task must also finalize and supersede an 'integrated' task."""
        self.tasks_store = {
            "t-integrated": _task("t-integrated", status="integrated", node="review", stage="review"),
        }

        with patch.object(_ctl, "get_task", side_effect=lambda tid: self.tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=self._fake_subprocess_run):
            ok, err = _ctl._invalidate_single_task("t-integrated", "review")

        # On main:
        # L125 only checks ("completed", "cleanup_ready"), omitting "integrated".
        # L136 rejects integrated with "status_not_supersedeable:integrated".
        # These assertions will fail (RED) on main:
        self.assertTrue(
            ok,
            f"Expected _invalidate_single_task to succeed for integrated task, but got error: {err}",
        )
        self.assertIn(
            "t-integrated",
            self.finalized_calls,
            "t-integrated must be finalized via finalize command",
        )
        self.assertIn(
            "t-integrated",
            self.superseded_calls,
            "t-integrated must be superseded after finalize",
        )

    def test_integrated_gate_task_in_legacy_mode_advanced_via_finalize_and_superseded(self):
        """In legacy fix-loop invalidation (selective_target_task_ids=None), an

        'integrated' gate task must also be finalized and superseded.
        """
        self.tasks_store = {
            "gate-integrated": _task("gate-integrated", status="integrated", node="review", stage="review"),
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
            result = _ctl.invalidate_for_fix_loop(
                "wf-1",
                "review",
                cfg,
                retry_node="implementation",
                selective_target_task_ids=None,
            )

        # On main: gate-integrated is skipped, not finalized, not superseded.
        self.assertIn(
            "gate-integrated",
            self.finalized_calls,
            "gate-integrated must be finalized in legacy mode",
        )
        self.assertIn(
            "gate-integrated",
            self.superseded_calls,
            "gate-integrated must be superseded in legacy mode",
        )
        self.assertIn(
            "gate-integrated",
            result,
            "gate-integrated must be in returned invalidated list in legacy mode",
        )


class TestCommittedTaskCannotSilentlyPassOrReportSuccess(unittest.TestCase):
    """Claim 2: committed 等无法自动收敛的任务绝不能被报告为成功，也不能静默跳过。

    A 'committed' task has unintegrated Git commits.
    It CANNOT be advanced via FINALIZE_ADVANCE.
    It CANNOT transition to superseded in TASK_TRANSITIONS.
    Therefore, during Fix-Loop Phase 2 downstream invalidation:
    - It must NOT be silently skipped via continue.
    - Selective invalidation must NOT report all_targets_applied=True (with pending=[], failed={}).
    - The un-invalidated task must be recorded in failed or pending, failing-closed.
    """

    def setUp(self):
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

    def test_committed_downstream_task_cannot_report_all_targets_applied(self):
        """When downstream has a 'committed' task that cannot be automatically

        converged/superseded, SelectiveInvalidationResult.all_targets_applied MUST
        be False.
        """
        self.tasks_store = {
            "target-impl": _task("target-impl", status="working", node="implementation", stage="implementation"),
            "downstream-committed": _task("downstream-committed", status="committed", node="review", stage="review"),
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
            result = _ctl.invalidate_for_fix_loop(
                "wf-1",
                "review",
                cfg,
                retry_node="implementation",
                selective_target_task_ids=["target-impl"],
            )

        # On main:
        # L380 returns:
        # SelectiveInvalidationResult(combined, applied=list(phase1_applied), pending=[], failed={})
        # which means result.all_targets_applied == True!
        # This falsely reports complete invalidation success when downstream-committed was skipped.
        # This assertion will fail (RED) on main:
        self.assertFalse(
            result.all_targets_applied,
            "Selective invalidation must NOT report all_targets_applied=True when downstream has an un-invalidated committed task",
        )

    def test_committed_downstream_task_not_silently_skipped(self):
        """A downstream 'committed' task must be tracked in result.failed or

        result.pending, rather than being silently dropped with pending=[] and failed={}.
        """
        self.tasks_store = {
            "target-impl": _task("target-impl", status="working", node="implementation", stage="implementation"),
            "downstream-committed": _task("downstream-committed", status="committed", node="review", stage="review"),
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
            result = _ctl.invalidate_for_fix_loop(
                "wf-1",
                "review",
                cfg,
                retry_node="implementation",
                selective_target_task_ids=["target-impl"],
            )

        # On main:
        # downstream-committed hits L332 `if status not in supersedeable: continue`.
        # It is silently skipped, and result.failed is {}, result.pending is [].
        # This assertion will fail (RED) on main:
        has_failure_or_pending_record = (
            "downstream-committed" in result.failed
            or "downstream-committed" in result.pending
        )
        self.assertTrue(
            has_failure_or_pending_record,
            f"downstream-committed must be tracked in failed or pending, but got failed={result.failed}, pending={result.pending}",
        )

    def test_committed_task_with_mixed_downstream_does_not_mask_failure(self):
        """Even if other downstream tasks succeed, a committed task must not be

        masked as success.
        """
        self.tasks_store = {
            "target-impl": _task("target-impl", status="working", node="implementation", stage="implementation"),
            "downstream-cleaned": _task("downstream-cleaned", status="cleaned", node="review", stage="review"),
            "downstream-committed": _task("downstream-committed", status="committed", node="review", stage="review"),
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
            result = _ctl.invalidate_for_fix_loop(
                "wf-1",
                "review",
                cfg,
                retry_node="implementation",
                selective_target_task_ids=["target-impl"],
            )

        # downstream-cleaned is superseded, but downstream-committed cannot be.
        # all_targets_applied must be False, and downstream-committed must be recorded in failed or pending.
        self.assertFalse(
            result.all_targets_applied,
            "Mixed downstream invalidation must fail-closed if committed task cannot be invalidated",
        )
        self.assertTrue(
            "downstream-committed" in result.failed or "downstream-committed" in result.pending,
            f"downstream-committed must be recorded in failed or pending, got failed={result.failed}, pending={result.pending}",
        )

    def test_legacy_mode_mixed_downstream_with_committed_fails_closed(self):
        """In legacy mode (selective_target_task_ids=None), if downstream has one

        cleaned task and one committed task, result.all_targets_applied must be False.
        """
        self.tasks_store = {
            "downstream-cleaned": _task("downstream-cleaned", status="cleaned", node="review", stage="review"),
            "downstream-committed": _task("downstream-committed", status="committed", node="review", stage="review"),
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
            result = _ctl.invalidate_for_fix_loop(
                "wf-1",
                "review",
                cfg,
                retry_node="implementation",
                selective_target_task_ids=None,
            )

        self.assertFalse(
            result.all_targets_applied,
            "Legacy mode must be fail-closed when committed downstream task cannot be superseded",
        )
        self.assertIn("downstream-committed", result.pending)
        self.assertIn("downstream-committed", result.failed)
        self.assertIn("downstream-cleaned", result.applied)


class TestFinalizeStateVerification(unittest.TestCase):
    """integrated 任务 finalize 命令成功但最终状态未实际进入 cleaned 时，不得报告作废成功。"""

    def test_integrated_finalize_success_but_unreached_cleaned_not_reported_as_success(self):
        finalized_calls = []
        superseded_calls = []

        tasks_store = {
            "target-impl": _task("target-impl", status="working", node="implementation", stage="implementation"),
            "downstream-integrated": _task("downstream-integrated", status="integrated", node="review", stage="review"),
        }

        def fake_run(cmd, **kwargs):
            subcmd = cmd[1]
            tid = cmd[2]
            if subcmd == "finalize":
                finalized_calls.append(tid)
                # finalize CLI returns 0, but task only advanced to cleanup_ready, NOT cleaned
                tasks_store[tid]["status"] = "cleanup_ready"
                return subprocess.CompletedProcess(cmd, 0, "", "")
            elif subcmd == "supersede":
                superseded_calls.append(tid)
                tasks_store[tid]["status"] = "superseded"
                return subprocess.CompletedProcess(cmd, 0, "", "")
            elif subcmd == "rework":
                tasks_store[tid]["status"] = "rework"
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }

        with patch.object(_ctl, "load_tasks", side_effect=lambda: list(tasks_store.values())), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=fake_run):
            result = _ctl.invalidate_for_fix_loop(
                "wf-1",
                "review",
                cfg,
                retry_node="implementation",
                selective_target_task_ids=["target-impl"],
            )

        self.assertIn("downstream-integrated", finalized_calls)
        self.assertNotIn("downstream-integrated", superseded_calls, "supersede must NOT be called if finalize did not reach cleaned")
        self.assertFalse(result.all_targets_applied)
        self.assertIn("downstream-integrated", result.pending)
        self.assertIn("downstream-integrated", result.failed)
        self.assertNotIn("downstream-integrated", result.applied)

        # Also verify single-task path
        tasks_store["downstream-integrated"]["status"] = "integrated"
        with patch.object(_ctl, "get_task", side_effect=lambda tid: tasks_store.get(tid)), \
             patch.object(_ctl.subprocess, "run", side_effect=fake_run):
            ok, err = _ctl._invalidate_single_task("downstream-integrated", "review")

        self.assertFalse(ok)
        self.assertIn("status_not_supersedeable", err)


class TestHandleFixLoopProgressionGuard(unittest.TestCase):
    """上层 handle_fix_loop 收到 Phase 2 未完成结果时，不得增加循环计数、写入 pending_redo 或投递推进事件。"""

    def setUp(self):
        import tempfile
        import queue
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-guard-")
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

    def test_handle_fix_loop_aborts_when_phase2_has_pending_tasks(self):
        """When Phase 2 downstream has committed task (pending invalidation),

        handle_fix_loop must abort progression.
        """
        tasks = [
            _task("blocker-1", status="blocked", stage_verdict="blocked", node="review", stage="review"),
            _task("downstream-committed", status="committed", node="review", stage="review"),
        ]

        cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "review", "depends_on": ["implementation"]},
            ]
        }

        with patch.object(_ctl, "load_tasks", return_value=tasks), \
             patch.object(_ctl, "get_task", side_effect=lambda tid: next((t for t in tasks if t["task_id"] == tid), None)), \
             patch.object(_ctl.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            _ctl.handle_fix_loop(
                "wf-1",
                "review",
                {"retry_node": "implementation", "max_loops": 3},
                cfg,
            )

        # 1. coordinator_queue must be empty (no fix_loop event queued)
        self.assertTrue(_ctl.coordinator_queue.empty(), "No fix_loop event should be emitted when Phase 2 invalidation has pending tasks")

        # 2. Stage state must not have pending_redo
        import json
        state = json.loads(self.stage_file.read_text())
        self.assertNotIn("wf-1|fixloop|implementation|pending_redo", state)
        self.assertNotIn("wf-1|fixloop|implementation", state)


if __name__ == "__main__":
    unittest.main()
