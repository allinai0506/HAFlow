"""hard_timeout_watchdog auto-recover regressions from wf-haflow-1024-02.

The 2026-10-24 incident: a silently-grinding craft task was watchdog-killed at
idle 3601s while still mid-loop; ``hard_timeout_watchdog`` was absent from
AUTO_RECOVER_REASONS, so the recovery operation sat in waiting_human with
attempts=0 for ~10h overnight. INV-RECOVERY-04: a watchdog kill is an
infrastructure verdict, not a quality verdict — it enters the same bounded
auto-supersede pipeline (lineage attempt cap, node-has-no-live-task gate,
redispatch SLA verification) as the fuse and crash reasons.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from herdr.liveness import select_infra_failures_for_recovery
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WatchdogReasonSelectionTest(unittest.TestCase):
    """The pure selector accepts watchdog kills under the same attempt cap."""

    REASONS = {"dispatch_delivery_fuse", "agent_process_crash", "hard_timeout_watchdog"}

    @staticmethod
    def _failed(task_id, reason, *, node="craft"):
        return {
            "task_id": task_id,
            "workflow_id": "wf-1024-02",
            "node": node,
            "stage": node,
            "status": "failed",
            "status_history": [{"to": "failed", "reason": reason}],
            "created_at": 100.0,
        }

    def test_watchdog_kill_is_selected(self):
        task = self._failed("wf-1024-02-craft-auto", "hard_timeout_watchdog")
        chosen = select_infra_failures_for_recovery([task], self.REASONS)
        self.assertEqual([t["task_id"] for t in chosen], [task["task_id"]])

    def test_watchdog_lineage_cap_still_binds(self):
        r1 = self._failed("wf-1024-02-craft-auto", "hard_timeout_watchdog")
        r2 = self._failed("wf-1024-02-craft-auto-r2", "hard_timeout_watchdog")
        chosen = select_infra_failures_for_recovery([r1, r2], self.REASONS)
        self.assertEqual(chosen, [])

    def test_one_watchdog_retry_remains_eligible(self):
        """A lineage with a single watchdog failure keeps one retry budget."""
        r1 = self._failed("wf-1024-02-craft-auto", "hard_timeout_watchdog")
        r2 = self._failed("wf-1024-02-craft-auto-r2", "manually_superseded")
        r2["status"] = "superseded"
        r2["superseded_by"] = "wf-1024-02-craft-auto-r3"
        chosen = select_infra_failures_for_recovery([r1, r2], self.REASONS)
        self.assertEqual([t["task_id"] for t in chosen], [r1["task_id"]])

    def test_quality_failure_stays_excluded(self):
        task = self._failed("wf-1024-02-craft-auto", "supervisor_rejected")
        chosen = select_infra_failures_for_recovery([task], self.REASONS)
        self.assertEqual(chosen, [])


class WatchdogControllerConstantTest(unittest.TestCase):
    """The deployed controller whitelist itself must carry the reason."""

    def test_auto_recover_reasons_include_watchdog(self):
        controller = load_script(
            "controller_wdt_test", "services/herdr-controller.py"
        )
        self.assertIn("hard_timeout_watchdog", controller.AUTO_RECOVER_REASONS)
        self.assertIn("dispatch_delivery_fuse", controller.AUTO_RECOVER_REASONS)


class WatchdogRecoverPipelineTest(unittest.TestCase):
    """recover_infra_failed_tasks supersedes a watchdog-killed task end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.tmp_dir / "state.db")
        self.controller = load_script(
            "controller_wdt_pipe_test", "services/herdr-controller.py"
        )

        from herdr.liveness import EpisodeStore

        self.store.save_workflow({"workflow_id": "wf-1024-02", "status": "running"})
        self.store.save_task(
            {
                "task_id": "wf-1024-02-craft-auto",
                "workflow_id": "wf-1024-02",
                "node": "craft",
                "stage": "craft",
                "agent": "agy",
                "status": "working",
                "pane_id": "w19:p7N",
                "clone_path": str(self.tmp_dir / "clone"),
                "created_at": 100.0,
                "started_at": 100.0,
            }
        )
        task = self.store.get_task("wf-1024-02-craft-auto")
        self.store.compare_and_set_task_transition(
            task_id="wf-1024-02-craft-auto",
            to_status="failed",
            reason="hard_timeout_watchdog",
            source="herdr-sentinel",
            expected_status=task["status"],
            expected_version=task["version"],
        )

        self.superseded = []

        def fake_run(command, *args, **kwargs):
            current = self.store.get_task("wf-1024-02-craft-auto")
            if current["status"] != "superseded":
                self.store.compare_and_set_task_transition(
                    task_id="wf-1024-02-craft-auto",
                    to_status="superseded",
                    reason="auto-recover: infrastructure failure",
                    source="test",
                    expected_status=current["status"],
                    expected_version=current["version"],
                )
            self.superseded.append(list(command))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        for name, value in {
            "_get_store": lambda: self.store,
            "load_tasks": self.store.list_tasks,
            "get_task": self.store.get_task,
            "workflow_closed": lambda _: False,
            "_workflow_entry": lambda _: {"status": "running"},
            "clear_stage_advance": lambda *a, **k: None,
            "_attention_store": EpisodeStore(self.tmp_dir / "attention.json"),
            "notify_attention": lambda *a, **k: True,
            "time": SimpleNamespace(time=lambda: 10_000.0),
            "subprocess": SimpleNamespace(run=fake_run),
        }.items():
            setattr(self.controller, name, value)

    def tearDown(self):
        self.tmp.cleanup()

    def test_watchdog_kill_is_superseded_for_redispatch(self):
        from services.controller.infra_recovery import recover_infra_failed_tasks

        recovered = recover_infra_failed_tasks(
            self.controller, "wf-1024-02", tasks=self.store.list_tasks()
        )
        self.assertTrue(recovered)
        self.assertEqual(self.store.get_task("wf-1024-02-craft-auto")["status"], "superseded")
        self.assertTrue(self.superseded)


if __name__ == "__main__":
    unittest.main()
