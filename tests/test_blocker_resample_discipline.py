"""显式恢复后，旧 BLOCKER 屏幕残留不得被重采样为新的阻塞事件 (C03c).

wf-project-0929 审计复现：屏幕出现 BLOCKER，任务 blocked；显式恢复为
working（版本递增），屏幕内容完全未变；下一次巡检把同一屏幕绑定任务**当前**
版本号记录新的 ``blocked_marker_observed``，Controller 的版本 CAS 无法识别
它来自恢复前的旧屏幕，任务再次 blocked。

修复纪律：持久跟踪 BLOCKER 标记在场状态，并结合三个持久事实判定采样——
absent→present 周期、先前样本是否已被 Controller 消费、先前样本的屏幕指纹
是否与当前屏幕一致。residue 只抑制重复的阻塞事件采样，不得短路同一任务的
崩溃检测与待投递指令处理（PR #161 评审）。
"""

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
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


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


class BlockerSampleActionTest(unittest.TestCase):
    """纯决策真值表：什么样的 sighting 允许变成 blocked_marker_observed 事件。"""

    def test_marker_absent_is_only_state_tracking(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=False,
                last_blocker_present=True,
                prior_event_version=1,
                prior_event_consumed=True,
                prior_event_screen_sha256="a",
                screen_sha256="a",
            ),
            "absent",
        )

    def test_absent_to_present_cycle_records(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=False,
                prior_event_version=1,
                prior_event_consumed=True,
                prior_event_screen_sha256="a",
                screen_sha256="b",
            ),
            "record",
        )

    def test_first_sighting_without_history_records(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=None,
                prior_event_version=None,
                prior_event_consumed=False,
                prior_event_screen_sha256=None,
                screen_sha256="a",
            ),
            "record",
        )

    def test_unconsumed_prior_sample_stays_re_samplable(self):
        """场景 A：元数据写使未消费样本版本失效后，必须继续提供可消费样本。"""
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=True,
                prior_event_version=1,
                prior_event_consumed=False,
                prior_event_screen_sha256="a",
                screen_sha256="a",
            ),
            "record",
        )

    def test_consumed_prior_with_same_screen_bytes_is_residue(self):
        """C03c：已消费样本的屏幕逐字节未变，即旧残留。"""
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=True,
                prior_event_version=1,
                prior_event_consumed=True,
                prior_event_screen_sha256="a",
                screen_sha256="a",
            ),
            "residue",
        )

    def test_consumed_prior_with_changed_screen_bytes_records(self):
        """恢复后快速再耗尽（两次巡检之间完成 absent→present）也是新阻塞。"""
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=True,
                prior_event_version=3,
                prior_event_consumed=True,
                prior_event_screen_sha256="a",
                screen_sha256="b",
            ),
            "record",
        )

    def test_legacy_consumed_prior_without_fingerprint_fails_toward_no_reblock(self):
        self.assertEqual(
            blocker_sample_action(
                marker_present=True,
                last_blocker_present=True,
                prior_event_version=1,
                prior_event_consumed=True,
                prior_event_screen_sha256=None,
                screen_sha256="a",
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
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "working",
            "started_at": time.time(),
        })

    def _payload(self, version, sha):
        return {
            "next_status": "blocked",
            "reason": "inner_loop_exhausted",
            "observed_status": "working",
            "observed_version": version,
            "screen_sha256": sha,
        }

    def _observe(self, marker_present, sha=None):
        payload = self._payload(self.store.get_task("impl-t6")["version"], sha) \
            if marker_present else None
        return self.store.observe_blocker_marker(
            "impl-t6", marker_present=marker_present, event_payload=payload
        )

    def _blocked_events(self):
        return self.store.list_events(
            task_id="impl-t6", event_type="blocked_marker_observed"
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
        # 1. 首次真实耗尽：record 且事件由观察器原子落盘，Controller 置 blocked。
        self.assertEqual(self._observe(True, "sha-v1")["action"], "record")
        self.assertEqual(len(self._blocked_events()), 1)
        version_after_block = self._transition(
            "blocked", "inner_loop_exhausted", "herdr-controller"
        )

        # 2. 显式恢复：blocked -> working，版本递增，屏幕内容完全未变。
        version_after_resume = self._transition(
            "working", "cli_set_status", "herdr-task"
        )
        self.assertGreater(version_after_resume, version_after_block)

        # 3. 同屏幕重采样：已消费样本 + 指纹一致 -> residue，不产生新事件。
        self.assertEqual(self._observe(True, "sha-v1")["action"], "residue")
        self.assertEqual(len(self._blocked_events()), 1)

        # 4. Agent 重派后屏幕重绘（标记消失），再次真实耗尽 -> absent -> record。
        self.assertEqual(self._observe(False)["action"], "absent")
        self.assertEqual(self._observe(True, "sha-v2")["action"], "record")
        self.assertEqual(len(self._blocked_events()), 2)

    def test_record_appends_event_atomically_with_sample_payload(self):
        """record 裁决必须在同一事务内落盘事件与在场状态（场景 B 结构性消除）。"""
        result = self._observe(True, "sha-atomic")
        self.assertEqual(result["action"], "record")
        events = self._blocked_events()
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(payload["screen_sha256"], "sha-atomic")
        self.assertEqual(payload["observed_version"],
                         self.store.get_task("impl-t6")["version"])
        self.assertEqual(events[0]["source"], "herdr-sentinel")
        self.assertEqual(events[0]["task_id"], "impl-t6")
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT blocker_present FROM completion_observations "
                "WHERE task_id = 'impl-t6'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], 1)

    def test_metadata_write_invalidating_unconsumed_sample_stays_re_samplable(self):
        """场景 A：消费前普通元数据更新递增版本，未消费样本不得被 residue 吞掉。"""
        self.assertEqual(self._observe(True, "sha-a")["action"], "record")
        version_before = self.store.get_task("impl-t6")["version"]
        self.store.update_task_metadata("impl-t6", {
            "last_steered_at": time.time(),
            "steering_history": [{"steer_id": "s1"}],
        })
        version_after = self.store.get_task("impl-t6")["version"]
        self.assertGreater(version_after, version_before)

        # 同屏幕、同指纹，但先前事件从未被消费 -> 必须重新采样可消费的新样本。
        self.assertEqual(self._observe(True, "sha-a")["action"], "record")
        events = self._blocked_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["payload"]["observed_version"], version_after)

    def test_fast_re_exhaustion_without_absent_patrol_is_recorded(self):
        """两次巡检之间完成 absent→present 的再耗尽，凭指纹变化识别为新阻塞。"""
        self.assertEqual(self._observe(True, "sha-1")["action"], "record")
        self._transition("blocked", "inner_loop_exhausted", "herdr-controller")
        self._transition("working", "cli_set_status", "herdr-task")

        # 恢复后同屏幕 -> residue（C03c 保持修复）。
        self.assertEqual(self._observe(True, "sha-1")["action"], "residue")

        # 屏幕已变化但从未观察到 absent -> 指纹不同即为新发生。
        self.assertEqual(self._observe(True, "sha-2")["action"], "record")
        self.assertEqual(len(self._blocked_events()), 2)

    def test_legacy_prior_without_fingerprint_is_residue(self):
        """旧库升级前的样本没有指纹：宁可等待也不重放旧屏幕。"""
        self.store.record_event(
            "blocked_marker_observed",
            {"next_status": "blocked", "reason": "inner_loop_exhausted",
             "observed_status": "working", "observed_version": 1},
            workflow_id="wf", task_id="impl-t6", source="herdr-sentinel",
        )
        self._transition("blocked", "inner_loop_exhausted", "herdr-controller")
        self._transition("working", "cli_set_status", "herdr-task")
        self.assertEqual(self._observe(True, "sha-x")["action"], "residue")
        self.assertEqual(len(self._blocked_events()), 1)

    def test_event_payload_required_for_present_marker(self):
        with self.assertRaises(ValueError):
            self.store.observe_blocker_marker("impl-t6", marker_present=True)

    def test_presence_state_survives_completion_row_upsert(self):
        """observe_completion 的全行 upsert 不得抹掉 blocker 在场状态。"""
        self._observe(True, "sha-keep")
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


class _StopSweeps(Exception):
    """驱动 main() 在第 N 次 sweep 后停止的哨兵。"""


class _FakeSteerAdapter:
    name = "codex-fake"
    protocol_level = "soft"
    supports_soft_steer = True

    def steer_soft(self, pane_id, instruction, operator):
        return {"ok": True, "injected": True, "interrupted": False}


class SentinelMainLoopResiduePatrolTest(unittest.TestCase):
    """真实 main() 巡检回归：residue 只抑制重复阻塞事件，不短路其余巡检。

    PR #161 评审以真实 Sentinel 复现：旧标记可见时，恢复指令连续三轮
    pending（投递尝试 0）、新崩溃事件为 0。此测试驱动真实 main()，用临时
    SQLite + 真实 steering 队列 + 受控 pane 读写锁定两条链路。
    """

    SCREEN_RESIDUE = "HERDR_TASK_BLOCKER:impl-t6"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "state.db"
        self.state_file = self.root / "sentinel-state.json"
        os.environ["HERDR_STATE_DB"] = str(self.db_path)
        self.addCleanup(os.environ.pop, "HERDR_STATE_DB", None)
        os.environ["TASKS_FILE"] = str(self.root / "tasks.json")
        self.addCleanup(os.environ.pop, "TASKS_FILE", None)

        self.sentinel = importlib.import_module("services.herdr-sentinel")
        import herdr.steering as herdr_steering
        self.steering = herdr_steering

        self.store = SQLiteStateStore(self.db_path)
        self.store.save_workflow({"workflow_id": "wf", "status": "running"})
        self.store.save_task({
            "task_id": "impl-t6",
            "workflow_id": "wf",
            "run_id": "r1",
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "working",
            "pane_id": "w1:p1",
            "started_at": time.time(),
        })
        # 播种一段已消费的阻塞 episode：record（含指纹）→ blocked → 显式恢复。
        fingerprint = self.sentinel._screen_fingerprint(self.SCREEN_RESIDUE + "\n")
        self.store.observe_blocker_marker(
            "impl-t6",
            marker_present=True,
            event_payload={
                "next_status": "blocked",
                "reason": "inner_loop_exhausted",
                "observed_status": "working",
                "observed_version": self.store.get_task("impl-t6")["version"],
                "screen_sha256": fingerprint,
            },
        )
        kernel.transition_task(
            task_id="impl-t6", to_status="blocked",
            reason="inner_loop_exhausted", source="herdr-controller",
            store=self.store,
        )
        kernel.transition_task(
            task_id="impl-t6", to_status="working",
            reason="cli_set_status", source="herdr-task",
            store=self.store,
        )
        self.assertEqual(self.store.get_task("impl-t6")["status"], "working")

        patcher = patch.object(self.sentinel, "STATE_FILE", self.state_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        adapter_patcher = patch(
            "herdr.steering.get_agent_adapter", return_value=_FakeSteerAdapter()
        )
        adapter_patcher.start()
        self.addCleanup(adapter_patcher.stop)

    def _drive_main(self, screen_text, sweeps=2):
        def fake_run(cmd, timeout=10):
            if cmd[:3] == ["herdr", "pane", "read"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=screen_text, stderr=""
                )
            if cmd[:2] == ["herdr", "agent"] and "get" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0,
                    stdout=json.dumps(
                        {"result": {"agent": {"agent_status": "idle"}}}
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        run_patcher = patch.object(self.sentinel, "run", side_effect=fake_run)
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        calls = {"n": 0}

        def fake_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] >= sweeps:
                raise _StopSweeps()

        sleep_patcher = patch("time.sleep", side_effect=fake_sleep)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

        errors = []

        def target():
            try:
                self.sentinel.main()
            except _StopSweeps:
                return
            except Exception as exc:  # pragma: no cover - 诊断用
                errors.append(exc)

        thread = threading.Thread(target=target, name="sentinel-main")
        thread.start()
        thread.join(timeout=60)
        self.assertFalse(thread.is_alive(), "main() did not stop in time")
        self.assertEqual(errors, [])

    def test_residue_does_not_block_steering_delivery_or_reblock(self):
        """旧标记可见时：恢复指令照常投递，且不再产生新的阻塞事件。"""
        queued = self.steering.queue_steer(
            "impl-t6", "继续推进当前修复", operator="human",
            execute_dispatch=False,
        )
        self.assertTrue(queued.get("ok"), queued)

        self._drive_main(self.SCREEN_RESIDUE, sweeps=2)

        data = self.steering.load_steering_data()
        item = data["steering_queues"]["impl-t6"][0]
        self.assertEqual(item["status"], "dispatched")
        self.assertGreaterEqual(item["delivery_attempt_count"], 1)
        events = self.store.list_events(
            task_id="impl-t6", event_type="blocked_marker_observed"
        )
        self.assertEqual(
            len(events), 1, "residue must not produce a new blocker sample"
        )
        self.assertEqual(self.store.get_task("impl-t6")["status"], "working")

    def test_residue_does_not_mask_a_real_process_crash(self):
        """旧标记与新的 Bun has crashed 同时可见时，崩溃事件照常记录并置 failed。"""
        self._drive_main(
            self.SCREEN_RESIDUE + "\nBun has crashed", sweeps=2
        )
        crash_events = self.store.list_events(
            task_id="impl-t6", event_type="agent_process_crash_observed"
        )
        self.assertGreaterEqual(
            len(crash_events), 1, "a real crash must not be masked by residue"
        )
        self.assertEqual(self.store.get_task("impl-t6")["status"], "failed")
        events = self.store.list_events(
            task_id="impl-t6", event_type="blocked_marker_observed"
        )
        self.assertEqual(len(events), 1, "residue must not re-block")


if __name__ == "__main__":
    unittest.main()
