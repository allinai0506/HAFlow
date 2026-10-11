import importlib
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parent.parent


def load_sentinel():
    path = ROOT / "services" / "herdr-sentinel.py"
    spec = importlib.util.spec_from_file_location("herdr_sentinel_mod", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSentinelHardTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.sentinel = load_sentinel()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_stalled_task_exceeding_hard_timeout_is_auto_failed(self):
        now = 10000.0
        # Task was last updated at 10000 - 3700 (idle for 3700s > default 3600s)
        task = {
            "task_id": "t-stalled-long",
            "workflow_id": "wf-1",
            "status": "working",
            "updated_at": now - 3700.0,
            "version": 1,
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        state = {}
        with patch.object(self.sentinel, "_get_store", return_value=self.store), \
             patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT": "3600"}):
            changed = self.sentinel.check_task_stalls([task], state, now=now)

        # The task should have been auto-failed by the hard timeout watchdog
        reloaded = self.store.get_task("t-stalled-long")
        self.assertEqual(reloaded["status"], "failed")
        self.assertEqual(reloaded["failure_reason"], "hard_timeout_watchdog")

        # Verify audit event recorded
        events = self.store.list_events(task_id="t-stalled-long")
        event_types = [e["event_type"] for e in events]
        self.assertIn("task_hard_timeout", event_types)
        timeout_event = next(e for e in events if e["event_type"] == "task_hard_timeout")
        self.assertGreaterEqual(timeout_event["payload"]["idle_seconds"], 3600)
        self.assertEqual(timeout_event["payload"]["previous_status"], "working")

    def test_stalled_task_within_hard_timeout_only_alerts_without_failing(self):
        now = 10000.0
        # Task was last updated at 10000 - 2000 (idle for 2000s: > stall 1800s, but < hard timeout 3600s)
        task = {
            "task_id": "t-stalled-short",
            "workflow_id": "wf-1",
            "status": "working",
            "updated_at": now - 2000.0,
            "version": 1,
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        state = {}
        alerts_recorded = []

        def capture_notify(tid, wid, msg):
            alerts_recorded.append((tid, wid, msg))

        with patch.object(self.sentinel, "_get_store", return_value=self.store), \
             patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT": "3600"}):
            self.sentinel.check_task_stalls([task], state, notify_fn=capture_notify, now=now)

        # Status should remain working (not failed)
        reloaded = self.store.get_task("t-stalled-short")
        self.assertEqual(reloaded["status"], "working")
        # But an alert was sent
        self.assertEqual(len(alerts_recorded), 1)
        self.assertEqual(alerts_recorded[0][0], "t-stalled-short")

    def test_hard_timeout_can_be_disabled_via_env(self):
        now = 10000.0
        task = {
            "task_id": "t-disabled-timeout",
            "workflow_id": "wf-1",
            "status": "working",
            "updated_at": now - 5000.0,
            "version": 1,
        }
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        self.store.save_task(task)

        state = {}
        with patch.object(self.sentinel, "_get_store", return_value=self.store), \
             patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT_ENABLED": "0"}):
            self.sentinel.check_task_stalls([task], state, now=now)

        reloaded = self.store.get_task("t-disabled-timeout")
        self.assertEqual(reloaded["status"], "working")

    def test_concurrent_progress_rejects_timeout_cas(self):
        now = 10000.0
        self.store.save_workflow({"workflow_id": "wf-1", "status": "running"})
        # Task created initially at version 1
        self.store.save_task({
            "task_id": "t-concurrent-race",
            "workflow_id": "wf-1",
            "status": "working",
        })
        stale_view = {
            "task_id": "t-concurrent-race",
            "workflow_id": "wf-1",
            "status": "working",
            "updated_at": now - 4000.0,
            "version": 1,
        }
        # In store, the task makes concurrent progress and bumps to version 2!
        self.store.save_task({
            "task_id": "t-concurrent-race",
            "workflow_id": "wf-1",
            "status": "working",
            "updated_at": now - 10.0,
        })

        state = {}
        with patch.object(self.sentinel, "_get_store", return_value=self.store), \
             patch.dict(os.environ, {"HERDR_TASK_HARD_TIMEOUT": "3600"}):
            changed = self.sentinel.check_task_stalls([stale_view], state, now=now)

        # CAS must reject: task should NOT have been failed!
        reloaded = self.store.get_task("t-concurrent-race")
        self.assertEqual(reloaded["status"], "working")
        self.assertEqual(reloaded["version"], 2)
        self.assertNotIn("t-concurrent-race", state.get("timeouts", {}))

        # No task_hard_timeout event recorded
        events = self.store.list_events(task_id="t-concurrent-race")
        event_types = [e["event_type"] for e in events]
        self.assertNotIn("task_hard_timeout", event_types)
