"""Finalize rc triage tests (plan-arch 4.1.1-C/6-T4 + plan-attack 4).

Contract under test (services/herdr-controller.py):
- triage_finalize_rc分流表: 0/2/3/4/5/75/其他,75唯一可重试,
  3走FR-4不耗预算,4立即升级不耗预算,2/5确定性失败+升级.
- commit-exit3(空)与integrate-exit3(脏)极性相反,严禁共用规则.
- EMPTY(A)复用set cleanup_ready落点.
- finalize_completed_task全部return点带reason.
- registry_watcher两处共用handle_finalize_retry,禁双路径.
- git_finalize_pending_tasks排除finalize_escalated_at.
"""

import ast
import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_controller(name="ctrl_finalize_rc_triage_test"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _task(
    task_id="t1",
    status="completed",
    mode="git",
    workflow_id="wf-1",
    extra=None,
):
    base = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "node": "implementation",
        "stage": "implementation",
        "status": status,
        "integration_mode": mode,
        "clone_path": "/tmp/clone",
        "branch": "herdr/task-t1",
    }
    if extra:
        base.update(extra)
    return base


def _proc(rc, out="out", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class TriageTableTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller("ctrl_triage_table")

    def test_rc0_is_ok(self):
        for step in ("commit", "integrate"):
            with self.subTest(step=step):
                triage = self.ctrl.triage_finalize_rc(step, 0)
                self.assertEqual(triage["action"], "ok")
                self.assertFalse(triage["retryable"])
                self.assertFalse(triage["consume_budget"])
                self.assertFalse(triage["escalate"])

    def test_rc75_is_only_retryable(self):
        for step, expected in (
            ("commit", "commit_retry"),
            ("integrate", "integration_retry"),
        ):
            with self.subTest(step=step):
                triage = self.ctrl.triage_finalize_rc(step, 75)
                self.assertEqual(triage["action"], "retry")
                self.assertTrue(triage["retryable"])
                self.assertTrue(triage["consume_budget"])
                self.assertFalse(triage["escalate"])
                self.assertEqual(triage["reason"], expected)

    def test_non75_never_retryable(self):
        for rc in (0, 1, 2, 3, 4, 5, 6, 128):
            for step in ("commit", "integrate"):
                with self.subTest(step=step, rc=rc):
                    if rc == 75:
                        continue
                    triage = self.ctrl.triage_finalize_rc(step, rc)
                    self.assertFalse(
                        triage["retryable"],
                        f"step={step} rc={rc} must not be retryable",
                    )

    def test_rc3_never_consumes_budget(self):
        for step in ("commit", "integrate"):
            with self.subTest(step=step):
                triage = self.ctrl.triage_finalize_rc(step, 3)
                self.assertFalse(triage["consume_budget"])
                self.assertFalse(triage["retryable"])

    def test_commit_exit3_empty_vs_integrate_exit3_dirty(self):
        commit_tri = self.ctrl.triage_finalize_rc("commit", 3)
        integrate_tri = self.ctrl.triage_finalize_rc("integrate", 3)
        self.assertEqual(commit_tri["action"], "empty")
        self.assertEqual(commit_tri["reason"], "commit_empty")
        self.assertFalse(commit_tri["escalate"])
        self.assertEqual(integrate_tri["action"], "dirty")
        self.assertEqual(integrate_tri["reason"], "integrate_dirty")
        self.assertTrue(integrate_tri["escalate"])
        # 极性相反: 严禁共用规则.
        self.assertNotEqual(commit_tri["action"], integrate_tri["action"])
        self.assertNotEqual(commit_tri["reason"], integrate_tri["reason"])

    def test_exit4_immediate_escalate_without_budget(self):
        for step in ("commit", "integrate"):
            with self.subTest(step=step):
                triage = self.ctrl.triage_finalize_rc(step, 4)
                self.assertTrue(triage["escalate"])
                self.assertFalse(triage["consume_budget"])
                self.assertFalse(triage["retryable"])
                self.assertIn("exit4", triage["reason"])

    def test_deterministic_2_5_fail_and_escalate(self):
        for rc in (2, 5):
            for step in ("commit", "integrate"):
                with self.subTest(step=step, rc=rc):
                    triage = self.ctrl.triage_finalize_rc(step, rc)
                    self.assertEqual(triage["action"], "deterministic_fail")
                    self.assertTrue(triage["escalate"])
                    self.assertFalse(triage["retryable"])
                    self.assertFalse(triage["consume_budget"])
                    self.assertIn(str(rc), triage["reason"])

    def test_other_rc_escalates_without_budget(self):
        for rc in (1, 6, 128):
            for step in ("commit", "integrate"):
                with self.subTest(step=step, rc=rc):
                    triage = self.ctrl.triage_finalize_rc(step, rc)
                    self.assertTrue(triage["escalate"])
                    self.assertFalse(triage["retryable"])
                    self.assertFalse(triage["consume_budget"])

    def test_invalid_rc_escalates(self):
        for bad in (None, "x", ""):
            triage = self.ctrl.triage_finalize_rc("commit", bad)
            self.assertTrue(triage["escalate"])
            self.assertFalse(triage["retryable"])


class FinalizeReturnReasonTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller("ctrl_finalize_return_test")
        self.ctrl._finalize_retry_exhausted_logged.clear()

    def test_all_returns_carry_reason(self):
        source = (
            HERDR_ROOT / "services" / "herdr-controller.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == (
                "finalize_completed_task"
            ):
                target = node
                break
        self.assertIsNotNone(target)
        returns = [n for n in ast.walk(target) if isinstance(n, ast.Return)]
        # 11 个原始 return 点 (+成功落点),全部带 reason.
        self.assertGreaterEqual(len(returns), 11)
        for ret in returns:
            self.assertIsNotNone(
                ret.value, "finalize return must carry a value"
            )
            dumped = ast.dump(ret.value)
            self.assertIn("reason", dumped)

    def test_missing_task_returns_reason(self):
        with patch.object(self.ctrl, "get_task", return_value=None):
            result = self.ctrl.finalize_completed_task("missing")
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)

    def test_unexpected_status_returns_reason(self):
        task = _task(status="working")
        with patch.object(self.ctrl, "get_task", return_value=task):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unexpected_status")

    def test_git_busy_returns_reason(self):
        task = _task(status="completed")
        with (
                    patch.object(self.ctrl, "get_task", return_value=task),
                    patch.object(
                        self.ctrl,
                        "ensure_no_git_processes",
                        side_effect=RuntimeError("busy"),
                    ),
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "git_busy_wait")

    def _patch_commit(self, rc, task=None, cleanup_rc=0):
        task = task or _task(status="completed")
        states = {"phase": "commit"}

        def _fake_run(cmd, **kwargs):
            if "commit" in cmd:
                return _proc(rc)
            if "integrate" in cmd:
                states["phase"] = "integrate"
                return _proc(0)
            if "cleanup" in cmd:
                return _proc(cleanup_rc)
            return _proc(0)

        return task, _fake_run, states

    def test_commit_75_returns_retry_without_escalation(self):
        task, fake_run, _states = self._patch_commit(75)
        with (
                    patch.object(self.ctrl, "get_task", return_value=task),
                    patch.object(
                        self.ctrl.subprocess, "run", side_effect=fake_run
                    ),
                    patch.object(
                        self.ctrl, "mark_finalize_escalated"
                    ) as escalated,
                    patch.object(
                        self.ctrl, "set_task_status"
                    ) as set_status,
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "commit_retry")
        self.assertEqual(result["action"], "retry")
        escalated.assert_not_called()
        set_status.assert_not_called()

    def test_commit_empty_reuses_cleanup_ready_landing(self):
        task = _task(status="completed")
        committed = _task(status="cleanup_ready")
        get_calls = {"n": 0}

        def _fake_get(task_id):
            get_calls["n"] += 1
            if get_calls["n"] == 1:
                return task
            return committed

        def _fake_run(cmd, **kwargs):
            if not cmd:
                return _proc(0, out="")
            if cmd[0] == "ps":
                return _proc(0, out="")
            if "commit" in cmd:
                return _proc(3, out="", err="no changes")
            if "cleanup" in cmd:
                return _proc(0, out="cleaned")
            self.fail(f"integrate must be skipped on EMPTY, got {cmd}")

        with (
                    patch.object(self.ctrl, "get_task", side_effect=_fake_get),
                    patch.object(
                        self.ctrl.subprocess, "run", side_effect=_fake_run
                    ),
                    patch.object(
                        self.ctrl, "set_task_status", return_value=True
                    ) as set_status,
                    patch.object(self.ctrl, "attention_note") as noted,
                    patch.object(
                        self.ctrl, "enqueue_stage_advance"
                    ),
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "finalized")
        # EMPTY(A) 复用 set cleanup_ready 落点 + 留痕.
        set_status.assert_called_once_with("t1", "cleanup_ready")
        noted.assert_called_once()
        _kwargs = noted.call_args
        self.assertIn("commit_empty", str(_kwargs))

    def test_commit_exit4_escalates_without_budget(self):
        task = _task(status="completed")
        with (
                    patch.object(self.ctrl, "get_task", return_value=task),
                    patch.object(
                        self.ctrl.subprocess,
                        "run",
                        return_value=_proc(4, err="bad ref"),
                    ),
                    patch.object(
                        self.ctrl, "mark_finalize_escalated"
                    ) as escalated,
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertIn("exit4", result["reason"])
        self.assertFalse(result.get("action") == "retry")
        escalated.assert_called_once()

    def test_commit_deterministic_2_escalates(self):
        task = _task(status="completed")
        with (
                    patch.object(self.ctrl, "get_task", return_value=task),
                    patch.object(
                        self.ctrl.subprocess,
                        "run",
                        return_value=_proc(2, err="bad status"),
                    ),
                    patch.object(
                        self.ctrl, "mark_finalize_escalated"
                    ) as escalated,
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertIn("deterministic", result["reason"])
        escalated.assert_called_once()

    def test_integrate_75_returns_retry(self):
        committed = _task(status="committed")
        with (
                    patch.object(self.ctrl, "get_task", return_value=committed),
                    patch.object(
                        self.ctrl.subprocess,
                        "run",
                        return_value=_proc(75, err="busy"),
                    ),
                    patch.object(
                        self.ctrl, "mark_finalize_escalated"
                    ) as escalated,
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "integration_retry")
        escalated.assert_not_called()

    def test_integrate_dirty_escalates_not_cleanup_ready(self):
        committed = _task(status="committed")
        with (
                    patch.object(self.ctrl, "get_task", return_value=committed),
                    patch.object(
                        self.ctrl.subprocess,
                        "run",
                        return_value=_proc(3, err="dirty tree"),
                    ),
                    patch.object(
                        self.ctrl, "mark_finalize_escalated"
                    ) as escalated,
                    patch.object(
                        self.ctrl, "set_task_status"
                    ) as set_status,
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "integrate_dirty")
        escalated.assert_called_once()
        # 脏路径严禁复用 EMPTY 落点.
        set_status.assert_not_called()

    def test_exit4_precedes_retry_upgrade(self):
        # 即使预算将耗尽,exit4 仍走立即升级,不耗预算.
        task = _task(status="completed")
        with (
                    patch.object(self.ctrl, "get_task", return_value=task),
                    patch.object(
                        self.ctrl.subprocess,
                        "run",
                        return_value=_proc(4, err="missing repo"),
                    ),
                    patch.object(
                        self.ctrl, "mark_finalize_escalated"
                    ) as escalated,
                    patch.object(self.ctrl, "attention_get", return_value={
                        "attempts": 4,
                        "next_retry_at": 0,
                    }),
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertIn("exit4", result["reason"])
        escalated.assert_called_once()
        _reason = escalated.call_args[1].get("reason", "")
        self.assertIn("exit4", str(_reason))

    def test_unknown_mode_returns_reason(self):
        task = _task(status="completed", mode="weird")
        with patch.object(self.ctrl, "get_task", return_value=task):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_integration_mode")

    def test_cleanup_failed_returns_reason(self):
        task = _task(status="completed", mode="none")
        with (
                    patch.object(self.ctrl, "get_task", return_value=task),
                    patch.object(
                        self.ctrl, "set_task_status", return_value=True
                    ),
                    patch.object(
                        self.ctrl.subprocess,
                        "run",
                        return_value=_proc(1, err="cleanup boom"),
                    ),
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "cleanup_failed")

    def test_success_returns_finalized(self):
        task = _task(status="completed", mode="none")
        cleaned = _task(status="cleaned", mode="none")
        with (
                    patch.object(
                        self.ctrl,
                        "get_task",
                        side_effect=[task, cleaned],
                    ),
                    patch.object(
                        self.ctrl, "set_task_status", return_value=True
                    ),
                    patch.object(
                        self.ctrl.subprocess,
                        "run",
                        return_value=_proc(0, out="cleaned"),
                    ),
                    patch.object(
                        self.ctrl, "enqueue_stage_advance"
                    ),
                ):
            result = self.ctrl.finalize_completed_task("t1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "finalized")


class WatcherHelperTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller("ctrl_watcher_helper_test")
        self.ctrl._finalize_retry_exhausted_logged.clear()

    def test_helper_shared_by_both_watcher_paths(self):
        source = (
            HERDR_ROOT / "services" / "herdr-controller.py"
        ).read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("handle_finalize_retry"), 3)

    def test_completed_git_reaches_commit_retry(self):
        task = _task(status="completed", mode="git")
        with (
                    patch.object(
                        self.ctrl, "workflow_closed", return_value=False
                    ),
                    patch.object(
                        self.ctrl, "attention_get", return_value={}
                    ),
                    patch.object(
                        self.ctrl,
                        "finalize_completed_task",
                        return_value={
                            "ok": False,
                            "reason": "commit_retry",
                            "action": "retry",
                        },
                    ),
                    patch.object(self.ctrl, "get_task", return_value=task),
                    patch.object(self.ctrl, "attention_note") as noted,
                    patch.object(self.ctrl, "attention_throttle"),
                ):
            handled = self.ctrl.handle_finalize_retry(task, now=1000.0)
        self.assertTrue(handled)
        noted.assert_called_once()
        _kwargs = noted.call_args
        self.assertIn("commit_retry", str(_kwargs))

    def test_exhausted_marks_escalated_event(self):
        task = _task(status="completed", mode="git")
        with (
                    patch.object(
                        self.ctrl, "workflow_closed", return_value=False
                    ),
                    patch.object(
                        self.ctrl,
                        "attention_get",
                        return_value={"attempts": 5, "next_retry_at": 0},
                    ),
                    patch.object(
                        self.ctrl, "mark_finalize_escalated"
                    ) as escalated,
                    patch.object(
                        self.ctrl, "finalize_completed_task"
                    ) as finalized,
                ):
            handled = self.ctrl.handle_finalize_retry(task, now=1000.0)
        self.assertTrue(handled)
        finalized.assert_not_called()
        escalated.assert_called_once()
        _kwargs = escalated.call_args[1] if escalated.call_args else {}
        self.assertIn("finalize_retry_exhausted", str(_kwargs))

    def test_non_retryable_does_not_consume_budget(self):
        task = _task(status="completed", mode="git")
        with (
                    patch.object(
                        self.ctrl, "workflow_closed", return_value=False
                    ),
                    patch.object(
                        self.ctrl, "attention_get", return_value={}
                    ),
                    patch.object(
                        self.ctrl,
                        "finalize_completed_task",
                        return_value={
                            "ok": False,
                            "reason": "commit_empty",
                            "action": "empty",
                        },
                    ),
                    patch.object(self.ctrl, "attention_note") as noted,
                ):
            handled = self.ctrl.handle_finalize_retry(task, now=1000.0)
        self.assertTrue(handled)
        noted.assert_not_called()

    def test_escalated_task_skipped(self):
        task = _task(
            status="completed",
            mode="git",
            extra={"finalize_escalated_at": 1234.0},
        )
        with (
                    patch.object(
                        self.ctrl, "finalize_completed_task"
                    ) as finalized,
                ):
            handled = self.ctrl.handle_finalize_retry(task, now=1000.0)
        self.assertFalse(handled)
        finalized.assert_not_called()

    def test_non_git_skipped(self):
        task = _task(status="completed", mode="none")
        with (
                    patch.object(
                        self.ctrl, "finalize_completed_task"
                    ) as finalized,
                ):
            handled = self.ctrl.handle_finalize_retry(task, now=1000.0)
        self.assertFalse(handled)
        finalized.assert_not_called()


class PendingExclusionTest(unittest.TestCase):
    def setUp(self):
        self.ctrl = _load_controller("ctrl_pending_exclusion_test")

    def test_escalated_excluded(self):
        tasks = [
            _task(task_id="t-keep", status="completed", mode="git"),
            _task(
                task_id="t-out",
                status="completed",
                mode="git",
                extra={"finalize_escalated_at": 1234.0},
            ),
            _task(task_id="t-done", status="committed", mode="git"),
        ]
        with patch.object(self.ctrl, "load_tasks", return_value=tasks):
            pending = self.ctrl.git_finalize_pending_tasks("wf-1")
        self.assertIn("t-keep", pending)
        self.assertIn("t-done", pending)
        self.assertNotIn("t-out", pending)

    def test_non_git_never_pending(self):
        tasks = [_task(task_id="t-x", status="completed", mode="none")]
        with patch.object(self.ctrl, "load_tasks", return_value=tasks):
            pending = self.ctrl.git_finalize_pending_tasks("wf-1")
        self.assertNotIn("t-x", pending)


if __name__ == "__main__":
    unittest.main()
