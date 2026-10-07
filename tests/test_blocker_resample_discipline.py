"""显式恢复后，旧 BLOCKER 屏幕残留不得被重采样为新的阻塞事件 (C03c).

wf-project-0929 审计复现：屏幕出现 BLOCKER，任务 blocked；显式恢复为
working（版本递增），屏幕内容完全未变；下一次巡检把同一屏幕绑定任务**当前**
版本号记录新的 ``blocked_marker_observed``，Controller 的版本 CAS 无法识别
它来自恢复前的旧屏幕，任务再次 blocked。

修复纪律（与完成路径 ``observe_completion`` 的 epoch、absent->present 周期
防护同构）：持久跟踪 BLOCKER 标记在场状态；仅「首次出现」或「缺失->再现周
期」才允许采样为事件；连续在场的屏幕残留一律判为 residue。
"""

import importlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

from herdr import kernel
from herdr.completion import blocker_sample_action
from herdr.state_store import SQLiteStateStore


class BlockerSampleActionTest(unittest.TestCase):
    """纯决策真值表：什么样的 sighting 允许变成 blocked_marker_observed 事件。"""

    def test_marker_absent_is_only_state_tracking(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=False,
                last_blocker_present=True,
                prior_sample_exists=True,
            ),
            "absent",
        )

    def test_absent_to_present_cycle_authenticates_a_fresh_blocker(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=False,
                prior_sample_exists=True,
            ),
            "record",
        )

    def test_continuous_presence_with_prior_sample_is_residue(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=True,
                prior_sample_exists=True,
            ),
            "residue",
        )

    def test_continuous_presence_without_prior_sample_records(self):
        """事件写盘失败后的重试必须自愈，不能被 residue 判定永久吞掉。"""
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=True,
                prior_sample_exists=False,
            ),
            "record",
        )

    def test_first_sighting_without_history_records(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=None,
                prior_sample_exists=False,
            ),
            "record",
        )

    def test_unknown_presence_with_history_is_residue(self):
        """旧库升级后的首巡检：有历史事件时宁可等待也不重放旧屏幕。"""
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=None,
                prior_sample_exists=True,
            ),
            "residue",
        )


class ObserveBlockerMarkerTests(unittest.TestCase):
    """持久观察器契约：审计复现序列必须以 residue 告终，不得再次阻塞。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.store.save_workflow({"workflow_id": "wf", "status": "running"})
        self.store.save_task({
            "task_id": "impl-t6",
            "workflow_id": "wf",
            "run_id": "r1",
            "status": "working",
            "started_at": time.time(),
        })

    def _observe(self, marker_present):
        return self.store.observe_blocker_marker(
            "impl-t6", marker_present=marker_present
        )

    def _record_blocked_event(self):
        """Sentinel 在 action=record 之后写事件的壳层行为。"""
        task = self.store.get_task("impl-t6")
        self.store.record_event(
            "blocked_marker_observed",
            {
                "next_status": "blocked",
                "reason": "inner_loop_exhausted",
                "observed_status": task.get("status"),
                "observed_version": task.get("version"),
            },
            workflow_id="wf",
            task_id="impl-t6",
            source="herdr-sentinel",
        )

    def _blocked_event_count(self):
        return len(
            self.store.list_events(
                task_id="impl-t6",
                event_type="blocked_marker_observed",
            )
        )

    def _transition(self, to_status, reason, source):
        res = kernel.transition_task(
            task_id="impl-t6",
            to_status=to_status,
            reason=reason,
            source=source,
            store=self.store,
        )
        self.assertTrue(res.get("accepted", True), res)
        return self.store.get_task("impl-t6")["version"]

    def test_resumed_task_is_never_reblocked_by_its_old_screen(self):
        # 1. 首次真实耗尽：首次出现 -> record，Sentinel 记事件，Controller 置 blocked。
        self.assertEqual(self._observe(True)["action"], "record")
        self._record_blocked_event()
        version_after_block = self._transition(
            "blocked", "inner_loop_exhausted", "herdr-controller"
        )

        # 2. 显式恢复：blocked -> working，版本递增，屏幕内容完全未变。
        version_after_resume = self._transition(
            "working", "cli_set_status", "herdr-task"
        )
        self.assertGreater(version_after_resume, version_after_block)

        # 3. 同屏幕重采样：必须判为 residue，不产生新事件。
        self.assertEqual(self._observe(True)["action"], "residue")
        self.assertEqual(self._blocked_event_count(), 1)

        # 4. Agent 重派后屏幕重绘（标记消失），再次真实耗尽 -> absent -> record。
        self.assertEqual(self._observe(False)["action"], "absent")
        self.assertEqual(self._observe(True)["action"], "record")
        self._record_blocked_event()
        self.assertEqual(self._blocked_event_count(), 2)

    def test_presence_state_survives_completion_row_upsert(self):
        """observe_completion 的全行 upsert 不得抹掉 blocker 在场状态。"""
        self._observe(True)
        self.store.observe_completion(
            "impl-t6", marker_present=False, agent_status="busy"
        )
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT blocker_present FROM completion_observations "
                "WHERE task_id = 'impl-t6'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)

    def test_migration_adds_blocker_column_to_legacy_databases(self):
        """旧库无 blocker_present 列时，连接初始化必须补列且保留旧数据。"""
        legacy = Path(self._tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy)
        try:
            conn.execute(
                "CREATE TABLE completion_observations ("
                "task_id TEXT PRIMARY KEY, observed_version INTEGER, "
                "marker_present INTEGER NOT NULL DEFAULT 0, "
                "updated_at REAL NOT NULL)"
            )
            conn.execute(
                "INSERT INTO completion_observations "
                "(task_id, observed_version, marker_present, updated_at) "
                "VALUES ('t-legacy', 3, 1, 1.0)"
            )
            conn.commit()
        finally:
            conn.close()

        from herdr import state_db

        conn = sqlite3.connect(legacy)
        conn.row_factory = sqlite3.Row
        try:
            state_db._ensure_completion_columns(conn)
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(completion_observations)"
                )
            }
        finally:
            conn.close()
        self.assertIn("blocker_present", columns)

        reopened = sqlite3.connect(legacy)
        try:
            row = reopened.execute(
                "SELECT marker_present FROM completion_observations "
                "WHERE task_id = 't-legacy'"
            ).fetchone()
        finally:
            reopened.close()
        self.assertEqual(row[0], 1)


class PaneVisibleFailureSemanticsTest(unittest.TestCase):
    """pane read 失败（rc!=0）必须等同读取异常：空串，而非"无标记的屏幕"。

    独立评审实测：pane 不存在时 CLI 以 rc=1 退出且 stdout 输出 JSON 错误。
    若把该错误负载当作屏幕内容，残留判定会得到伪造的 absent 半周期，
    C03c 症状可经此缝隙复发。
    """

    def setUp(self):
        self.sentinel = importlib.import_module("services.herdr-sentinel")

    def test_failed_read_with_error_payload_is_unknown_not_absent(self):
        failed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout='{"error":{"code":"pane_not_found","message":"x"}}',
            stderr="",
        )
        with patch.object(self.sentinel, "run", return_value=failed):
            self.assertEqual(self.sentinel.pane_visible("w1:p1"), "")

    def test_successful_read_returns_screen_text(self):
        ok = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="HERDR_TASK_BLOCKER:impl-t6\n",
            stderr="",
        )
        with patch.object(self.sentinel, "run", return_value=ok):
            self.assertIn(
                "HERDR_TASK_BLOCKER", self.sentinel.pane_visible("w1:p1")
            )

    def test_raising_read_stays_empty(self):
        with patch.object(self.sentinel, "run", side_effect=OSError("timeout")):
            self.assertEqual(self.sentinel.pane_visible("w1:p1"), "")


if __name__ == "__main__":
    unittest.main()
