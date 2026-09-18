"""Tests for Inner Loop Protocol (Task 1 — Phase 0).

Covers:
1. evaluator.generate_blocker_report: BLOCKER.md created when loop exhausted
2. evaluator.generate_blocker_report: contains required escalation fields
3. herdr-sentinel: HERDR_TASK_BLOCKER signal source-level verification
4. herdr-task: prompt iron-rule prohibitions source-level verification
"""

import sys
import tempfile
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))


# ---------------------------------------------------------------------------
# 1 & 2: generate_blocker_report (via herdr.evaluator)
# ---------------------------------------------------------------------------

class BlockerReportGenerationTest(unittest.TestCase):
    """generate_blocker_report() must write a valid BLOCKER.md."""

    def setUp(self):
        from herdr.evaluator import MetricVector, generate_blocker_report, LOOP_DIR_NAME
        self.MetricVector = MetricVector
        self.generate_blocker_report = generate_blocker_report
        self.LOOP_DIR_NAME = LOOP_DIR_NAME

    def _make_loop_dir(self, tmp_path):
        loop_dir = Path(tmp_path) / self.LOOP_DIR_NAME
        loop_dir.mkdir(parents=True, exist_ok=True)
        return loop_dir

    def test_blocker_md_created_on_exhaustion(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            loop_dir = self._make_loop_dir(tmpdir)
            metrics = self.MetricVector(
                correctness=50.0,
                quality=80.0,
                scope=100.0,
                repro=0.0,
                composite_score=55.0,
                failing_tests=["test_foo", "test_bar"],
                lint_errors=2,
                has_repro_test=True,
            )
            result = self.generate_blocker_report(loop_dir, metrics, iteration=3, max_iter=3)
            self.assertTrue(result.exists(), "BLOCKER.md should be created")
            self.assertEqual(result.name, "BLOCKER.md")

    def test_blocker_md_contains_failing_tests(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            loop_dir = self._make_loop_dir(tmpdir)
            metrics = self.MetricVector(
                correctness=0.0,
                quality=70.0,
                scope=100.0,
                repro=0.0,
                composite_score=20.0,
                failing_tests=["tests/test_alpha.py::test_x", "tests/test_beta.py::test_y"],
                lint_errors=0,
                has_repro_test=True,
            )
            result = self.generate_blocker_report(loop_dir, metrics, iteration=5, max_iter=5)
            content = result.read_text(encoding="utf-8")
            self.assertIn("Escalation Blocker Report", content, "Must have report title")
            self.assertIn("test_alpha", content, "Must list failing test names")
            self.assertIn("test_beta", content, "Must list failing test names")
            self.assertIn("HERDR_TASK_BLOCKER", content, "Must mention escalation signal")
            self.assertIn("5 次重试", content, "Must state the max retry count")

    def test_blocker_md_repro_status_when_failing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            loop_dir = self._make_loop_dir(tmpdir)
            metrics = self.MetricVector(
                correctness=80.0,
                quality=100.0,
                scope=100.0,
                repro=0.0,
                composite_score=60.0,
                failing_tests=[],
                lint_errors=0,
                has_repro_test=True,
            )
            result = self.generate_blocker_report(loop_dir, metrics, iteration=3, max_iter=3)
            content = result.read_text(encoding="utf-8")
            self.assertIn("未通过", content, "Repro failure must be marked in BLOCKER.md")

    def test_blocker_md_repro_status_when_passing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            loop_dir = self._make_loop_dir(tmpdir)
            metrics = self.MetricVector(
                correctness=70.0,
                quality=80.0,
                scope=100.0,
                repro=100.0,
                composite_score=77.0,
                failing_tests=["test_z"],
                lint_errors=0,
                has_repro_test=False,
            )
            result = self.generate_blocker_report(loop_dir, metrics, iteration=3, max_iter=3)
            content = result.read_text(encoding="utf-8")
            self.assertIn("无复现用例", content, "No repro test must say '无复现用例'")

    def test_blocker_md_is_written_atomically(self):
        """BLOCKER.md must not leave a .md.tmp file behind."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            loop_dir = self._make_loop_dir(tmpdir)
            metrics = self.MetricVector(composite_score=10.0)
            self.generate_blocker_report(loop_dir, metrics, iteration=2, max_iter=2)
            tmp_files = list(loop_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [], "No .tmp files should remain after atomic write")


# ---------------------------------------------------------------------------
# 3: Sentinel BLOCKER signal detection (source-level verification)
# ---------------------------------------------------------------------------

class SentinelBlockerDetectionTest(unittest.TestCase):
    """Sentinel source must contain BLOCKER marker detection with correct status."""

    def _sentinel_source(self):
        return (HERDR_ROOT / "services" / "herdr-sentinel.py").read_text(encoding="utf-8")

    def test_blocker_marker_defined_in_sentinel(self):
        src = self._sentinel_source()
        self.assertIn("HERDR_TASK_BLOCKER:{task_id}", src,
                      "Sentinel must define blocker_marker variable")

    def test_blocker_transitions_to_blocked_not_agent_done(self):
        src = self._sentinel_source()
        idx = src.find("blocker_marker in screen")
        self.assertGreater(idx, 0, "Sentinel must check blocker_marker in screen")
        section = src[idx:idx + 300]
        self.assertIn('"blocked"', section, "Blocker must set status to 'blocked'")
        self.assertNotIn('"agent_done"', section, "Blocker must NOT set 'agent_done'")

    def test_blocker_reason_is_inner_loop_exhausted(self):
        src = self._sentinel_source()
        self.assertIn("inner_loop_exhausted", src,
                      "Blocked reason must be 'inner_loop_exhausted'")

    def test_sentinel_log_message_for_blocker(self):
        src = self._sentinel_source()
        self.assertIn("SENTINEL BLOCKER", src,
                      "Sentinel must print [SENTINEL BLOCKER] log on escalation")


# ---------------------------------------------------------------------------
# 4: Prompt iron-rule injection (source-level verification)
# ---------------------------------------------------------------------------

class PromptIronRuleInjectionTest(unittest.TestCase):
    """herdr-task dispatch_task() prompt must contain all inner loop iron rules."""

    def _herdr_task_source(self):
        return (HERDR_ROOT / "bin" / "herdr-task").read_text(encoding="utf-8")

    def test_prohibit_done_before_self_check(self):
        src = self._herdr_task_source()
        self.assertIn("严禁在自检未通过前输出完成标记", src)

    def test_prohibit_abandoning_workstation(self):
        src = self._herdr_task_source()
        self.assertIn("严禁在自检未通过时放弃工位", src)

    def test_prohibit_escalating_micro_issues(self):
        src = self._herdr_task_source()
        self.assertIn("严禁向上层汇报局部问题", src)

    def test_blocker_escalation_protocol_included(self):
        src = self._herdr_task_source()
        self.assertIn("HERDR_TASK_BLOCKER:", src)
        self.assertIn("熔断求助流程", src)

    def test_inner_loop_protocol_header(self):
        src = self._herdr_task_source()
        self.assertIn("Inner Loop Protocol", src)

    def test_normal_flow_steps_included(self):
        src = self._herdr_task_source()
        self.assertIn("GOAL.md", src)
        self.assertIn("herdr-loop eval", src)


# ---------------------------------------------------------------------------
# 5: Controller arbitration card (inner_loop_exhausted -> BLOCKER.md)
# ---------------------------------------------------------------------------

def _load_controller(name="ctrl_inner_loop_arbitration_test"):
    import importlib.machinery
    import importlib.util
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(
            name, str(HERDR_ROOT / "services" / "herdr-controller.py")
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BlockerArbitrationEventTest(unittest.TestCase):
    """内环耗尽必须走专属仲裁卡(BLOCKER.md + 三选一裁决)。

    回归背景:Phase 0 内环协议(2026-09-13)的最后一跳在 stash 中丢失,
    内环耗尽时 Controller 只发通用 blocked 卡,工位自述 BLOCKER.md 被丢弃。
    """

    def setUp(self):
        self.ctrl = _load_controller()
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-blocker-card-")
        self.addCleanup(self.tmp.cleanup)

    def _task(self, clone_path="", sentinel_reason="inner_loop_exhausted"):
        return {
            "task_id": "wf-1-impl-x",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "pane_id": "w1:p2",
            "agent": "opencode",
            "goal": "实现功能",
            "acceptance_criteria": ["a"],
            "clone_path": clone_path,
            "sentinel_reason": sentinel_reason,
        }

    def _write_blocker(self, content):
        loop_dir = Path(self.tmp.name) / ".herdr-loop"
        loop_dir.mkdir(parents=True, exist_ok=True)
        (loop_dir / "BLOCKER.md").write_text(content, encoding="utf-8")
        return self.tmp.name

    def test_blocked_event_type_routes_inner_loop_exhausted(self):
        task = self._task()
        self.assertEqual(
            self.ctrl.blocked_event_type(task), "inner_loop_exhausted"
        )
        self.assertEqual(
            self.ctrl.blocked_event_type(self._task(sentinel_reason="")),
            "blocked",
        )
        self.assertEqual(self.ctrl.blocked_event_type(None), "blocked")

    def test_arbitration_card_includes_blocker_md(self):
        clone = self._write_blocker("# 求助\n\n评分停滞在 0.4,缺接口定义。")
        message = self.ctrl.build_coordinator_message(
            self._task(clone_path=clone), "inner_loop_exhausted"
        )
        self.assertIn("HERDR_CONTROLLER_BLOCKER_EVENT", message)
        self.assertIn("inner_loop_exhausted", message)
        self.assertIn("评分停滞在 0.4", message)
        self.assertIn("rework", message)
        self.assertIn("failed", message)
        self.assertIn("仲裁前不得把 Task 置为 completed", message)
        self.assertIn("效率纪律", message)
        self.assertIn("~/HAFlow/bin/herdr-task", message)
        self.assertNotIn("~/herdr/bin/herdr-task", message)

    def test_arbitration_card_falls_back_without_blocker_md(self):
        message = self.ctrl.build_coordinator_message(
            self._task(clone_path=self.tmp.name), "inner_loop_exhausted"
        )
        self.assertIn("BLOCKER.md 未找到", message)

    def test_generic_blocked_message_unchanged(self):
        message = self.ctrl.build_coordinator_message(
            self._task(sentinel_reason=""), "blocked"
        )
        self.assertIn("HERDR_CONTROLLER_BLOCKED_EVENT", message)
        self.assertNotIn("HERDR_CONTROLLER_BLOCKER_EVENT", message)


if __name__ == "__main__":
    unittest.main()
