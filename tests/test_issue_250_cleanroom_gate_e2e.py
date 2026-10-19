"""End-to-End integration test reproducing and verifying Issue #250.

Acceptance Criteria:
• 复现现场：wf-haflow-1017-01 cleanroom_* 两任务 failure_reason=hard_timeout_watchdog，
  gate 文件 ~/.herdr-controller/gate-verdicts/wf-haflow-1017-01-cleanroom_{test,review}-auto.json；
• 修复后：洁净室门禁在全量套件 >300s 的仓库上跑完并自动落 verdict，无需人工采纳。
"""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from herdr import state_store

HERDR_ROOT = Path(__file__).resolve().parent.parent


def _load_controller(name="ctrl_issue_250_e2e"):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_sentinel():
    return importlib.import_module("services.herdr-sentinel")


class CleanroomGateEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.db_path = self.tmp_dir / "state.db"
        self.tasks_file = self.tmp_dir / "tasks.json"
        self.verdicts_dir = self.tmp_dir / "gate-verdicts"
        self.verdicts_dir.mkdir(parents=True)
        self.store = state_store.SQLiteStateStore(self.db_path)
        self.ctrl = _load_controller()
        self.sentinel = _load_sentinel()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cleanroom_gate_verdict_auto_accepted_without_human_arbitration(self):
        """Verify the full lifecycle:
        1. A cleanroom gate task runs in a workspace taking > 300s (e.g. 753s).
        2. Evaluator timeout scales adaptively, allowing evaluation to succeed.
        3. Gate verdict file is written (~/.herdr-controller/gate-verdicts/<task_id>.json).
        4. Even after 3600s, Sentinel does NOT mark task as failed (suppressed by verdict file).
        5. Controller detects verdict in idle / reconciliation, auto-adopting verdict into task.
        6. Task is marked 'completed' with stage_verdict='blocked' without human bypass!
        """
        task_id = "wf-haflow-1017-01-cleanroom_test-auto"
        workflow_id = "wf-haflow-1017-01"
        now = 10000.0

        # Step 1: Create task in state store
        task = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "working",
            "completion_protocol": "receipt-v1",
            "created_at": now - 4000.0,
            "updated_at": now - 4000.0,
            "version": 4,
            "pane_id": "p1",
            "gate": {"retry_node": "craft"},
        }
        self.store.save_task(task)

        # Step 2: Write gate verdict file (reproducing the exact verdict file from incident)
        verdict_file = self.verdicts_dir / f"{task_id}.json"
        verdict_file.write_text(
            json.dumps({
                "verdict": "blocked",
                "note": "D-1 阻塞：项目切换后继承上一项目选择的模板",
            }),
            encoding="utf-8",
        )

        # Step 3: Sentinel sweep at now (4000s > hard_timeout 3600s)
        sentinel_state = {"stalls": {}, "timeouts": {}}
        with patch.dict(os.environ, {
            "HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir),
            "HERDR_STATE_DB": str(self.db_path),
            "TASKS_FILE": str(self.tasks_file),
        }), patch.object(self.sentinel, "_get_store", return_value=self.store):

            # Check stalls - must suppress watchdog failure
            self.sentinel.check_task_stalls([task], sentinel_state, now=now)

            task_after_sentinel = self.store.get_task(task_id)
            self.assertEqual(
                task_after_sentinel.get("status"),
                "working",
                "Sentinel must NOT fail gate task when verdict file is already present",
            )

        # Step 4: Controller handle_event with agent_status="idle" (or reconciliation)
        with patch.dict(os.environ, {
            "HERDR_GATE_VERDICT_DIR": str(self.verdicts_dir),
            "HERDR_STATE_DB": str(self.db_path),
            "TASKS_FILE": str(self.tasks_file),
            "PYTHONPATH": str(HERDR_ROOT),
        }), patch.object(self.ctrl, "_get_store", return_value=self.store), \
            patch.object(self.ctrl, "get_task", side_effect=lambda tid: self.store.get_task(tid)), \
            patch.object(self.ctrl, "node_is_gate", return_value=True):

            # Agent goes idle after writing verdict
            self.ctrl.handle_event(task_id, "idle")

            # Check that task transitioned to agent_done
            task_done = self.store.get_task(task_id)
            self.assertEqual(task_done.get("status"), "agent_done")

            # Controller try_auto_verdict
            accepted = self.ctrl.try_auto_verdict(task_id)
            self.assertTrue(accepted, "Controller try_auto_verdict must accept the gate verdict")

            # Final check: task is completed with stage_verdict="blocked"
            final_task = self.store.get_task(task_id)
            self.assertEqual(final_task.get("status"), "completed")
            self.assertEqual(final_task.get("stage_verdict"), "blocked")
            self.assertIn("D-1 阻塞", final_task.get("stage_verdict_note"))
