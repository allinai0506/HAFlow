#!/usr/bin/env python3
"""C05b：多栈仓库不得凭根目录 package.json 猜测试命令。

台账裁决：显式 --test-cmd 契约优先；缺契约遇歧义必须可恢复拒绝，
不能猜 Java 默认。审计复现：前端+Java 后端并存项目、目标为后端任务，
仍生成 CI=1 npm test；纯 Java 仓库则落到错误的 pytest 回退。

边界（第二轮评审）：tests/ 目录是弱 python 信号——JS/Rust 仓库常见
根 tests/ 布局，package.json/Cargo.toml 在场时不得把它升级成 python 栈。
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from herdr.evaluator import select_default_test_command  # noqa: E402
from tests.test_fix_loop_pr1 import _load_module  # noqa: E402

_cli = _load_module("c05b_cli", ROOT / "bin" / "herdr-task")


class SelectDefaultTestCommandTest(unittest.TestCase):
    """纯决策真值表：栈标记 → 命令或可恢复拒绝。"""

    def test_single_javascript_stack_keeps_existing_command(self):
        self.assertEqual(
            select_default_test_command({"package_json": True}),
            {"command": "CI=1 npm test"},
        )

    def test_strong_python_marker_keeps_existing_command(self):
        self.assertEqual(
            select_default_test_command({"pytest_ini": True}),
            {"command": "pytest"},
        )

    def test_single_rust_stack_keeps_existing_command(self):
        self.assertEqual(
            select_default_test_command({"cargo_toml": True}),
            {"command": "cargo test"},
        )

    def test_mixed_stack_is_a_recoverable_refusal(self):
        decision = select_default_test_command(
            {"package_json": True, "pom_xml": True}
        )
        self.assertEqual(decision["refused"], "multi_stack_ambiguous")
        self.assertIn("package.json", decision["detail"])
        self.assertIn("pom.xml", decision["detail"])

    def test_strong_python_marker_with_java_is_multi_stack(self):
        decision = select_default_test_command(
            {"pytest_ini": True, "pom_xml": True}
        )
        self.assertEqual(decision["refused"], "multi_stack_ambiguous")

    def test_gradle_counts_as_java_stack(self):
        decision = select_default_test_command(
            {"package_json": True, "build_gradle": True}
        )
        self.assertEqual(decision["refused"], "multi_stack_ambiguous")

    def test_java_only_is_refused_not_guessed(self):
        """不能猜 Java 默认：纯 Java 仓库缺显式契约必须可恢复拒绝。"""
        for markers in ({"pom_xml": True}, {"build_gradle": True}):
            decision = select_default_test_command(markers)
            self.assertEqual(
                decision["refused"], "java_without_explicit_contract", markers
            )

    def test_tests_dir_alone_is_python(self):
        self.assertEqual(
            select_default_test_command({"tests_dir": True}),
            {"command": "pytest"},
        )

    def test_tests_dir_is_weak_beside_package_json(self):
        """JS 仓库常见的根 tests/ 布局不得被误判为多栈。"""
        self.assertEqual(
            select_default_test_command({"package_json": True, "tests_dir": True}),
            {"command": "CI=1 npm test"},
        )

    def test_tests_dir_is_weak_beside_cargo_toml(self):
        self.assertEqual(
            select_default_test_command({"cargo_toml": True, "tests_dir": True}),
            {"command": "cargo test"},
        )

    def test_tests_dir_is_weak_beside_java(self):
        """Java 仓库的 tests/ 目录不构成 python 栈：按 Java 拒绝。"""
        decision = select_default_test_command({"pom_xml": True, "tests_dir": True})
        self.assertEqual(decision["refused"], "java_without_explicit_contract")

    def test_no_markers_falls_back_to_existing_pytest_default(self):
        self.assertEqual(select_default_test_command({}), {"command": "pytest"})


class AutoInitTestCommandContractTest(unittest.TestCase):
    """壳层契约：探测→裁决透传；拒绝时 init_loop 不得被调用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _make_clone(self, markers):
        clone = self.base / f"clone-{abs(hash(tuple(sorted(markers))))}"
        clone.mkdir()
        files = {
            "package_json": ("package.json", "{}"),
            "pytest_ini": ("pytest.ini", "[pytest]\n"),
            "cargo_toml": ("Cargo.toml", "[package]\n"),
            "pom_xml": ("pom.xml", "<project/>\n"),
            "build_gradle": ("build.gradle", "\n"),
        }
        for key, (name, body) in files.items():
            if markers.get(key):
                (clone / name).write_text(body)
        if markers.get("tests_dir"):
            (clone / "tests").mkdir()
        return clone

    def test_multi_stack_clone_refuses_without_guessing(self):
        clone = self._make_clone({"package_json": True, "pom_xml": True})

        def forbidden_init(**kwargs):
            raise AssertionError(
                "refused launch must not init the loop with a guessed command"
            )

        with patch("herdr.evaluator.init_loop", forbidden_init):
            result = _cli.auto_init_task_loop(
                clone, "backend task", [], node="implementation", task_type="fix"
            )
        self.assertEqual(result["refused"], "multi_stack_ambiguous")

    def test_explicit_test_cmd_contract_takes_priority(self):
        """多栈仓库 + 显式契约：不拒绝，init_loop 收到显式命令。"""
        clone = self._make_clone({"package_json": True, "pom_xml": True})
        captured = {}
        with patch(
            "herdr.evaluator.init_loop",
            lambda **kwargs: captured.update(kwargs),
        ):
            result = _cli.auto_init_task_loop(
                clone, "backend task", [], test_cmd="mvn -q test",
                node="implementation", task_type="fix",
            )
        self.assertIsNone(result)
        self.assertEqual(captured["test_cmd"], "mvn -q test")

    def test_single_stack_clone_keeps_existing_default(self):
        clone = self._make_clone({"package_json": True})
        captured = {}
        with patch(
            "herdr.evaluator.init_loop",
            lambda **kwargs: captured.update(kwargs),
        ):
            result = _cli.auto_init_task_loop(
                clone, "frontend task", [], node="implementation", task_type="fix"
            )
        self.assertIsNone(result)
        self.assertEqual(captured["test_cmd"], "CI=1 npm test")

    def test_java_only_clone_refuses_instead_of_pytest_guess(self):
        clone = self._make_clone({"pom_xml": True})

        def forbidden_init(**kwargs):
            raise AssertionError("pytest must not be guessed for a Java-only repo")

        with patch("herdr.evaluator.init_loop", forbidden_init):
            result = _cli.auto_init_task_loop(
                clone, "java task", [], node="implementation", task_type="fix"
            )
        self.assertEqual(result["refused"], "java_without_explicit_contract")


class RefuseLoopLaunchTest(unittest.TestCase):
    """拒绝处理器：回收资源 + 持久审计事件 + 横幅 + exit 2。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_refusal_reclaims_records_and_exits_2(self):
        """第三轮评审 B1：键名不一致曾让真实拒绝路径 KeyError、exit 1。"""
        reclaimed = []
        recorded = []
        store = SimpleNamespace(
            record_event=lambda event, payload, **kwargs: recorded.append(
                (event, dict(payload), kwargs)
            )
        )
        args = SimpleNamespace(workflow_id="wf", node="implementation")
        refusal = {
            "refused": "multi_stack_ambiguous",
            "detail": "multiple stacks detected at repo root",
        }

        with patch.object(
            _cli, "_reclaim_unregistered_launch_resources",
            side_effect=lambda *a, **k: reclaimed.append(k),
        ), patch.object(_cli, "_get_store", return_value=store), patch(
            "builtins.print"
        ) as fake_print:
            with self.assertRaises(SystemExit) as ctx:
                _cli._refuse_loop_launch(
                    args, refusal,
                    clone_path=self._tmp.name, pane_id="w2:p9",
                    pane_source="dynamic",
                )

        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(
            reclaimed,
            [{"clone_path": self._tmp.name, "pane_id": "w2:p9",
              "pane_source": "dynamic"}],
        )
        self.assertEqual(len(recorded), 1)
        event, payload, kwargs = recorded[0]
        self.assertEqual(event, "test_cmd_refused")
        self.assertEqual(payload["refusal_code"], "multi_stack_ambiguous")
        self.assertTrue(payload["actionable"])
        self.assertEqual(kwargs.get("workflow_id"), "wf")
        banner = " ".join(str(call) for call in fake_print.call_args_list)
        self.assertIn("[TEST CMD REFUSED]", banner)
        self.assertIn("multi_stack_ambiguous", banner)

    def test_refusal_exit_survives_event_store_failure(self):
        """审计事件写盘失败不得改变退出语义（仍 exit 2）。"""
        store = SimpleNamespace(
            record_event=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db busy"))
        )
        args = SimpleNamespace(workflow_id="wf", node="implementation")

        with patch.object(
            _cli, "_reclaim_unregistered_launch_resources", return_value=None
        ), patch.object(_cli, "_get_store", return_value=store), patch(
            "builtins.print"
        ):
            with self.assertRaises(SystemExit) as ctx:
                _cli._refuse_loop_launch(
                    args, {"refused": "java_without_explicit_contract", "detail": "x"},
                    clone_path=self._tmp.name, pane_id=None,
                    pane_source="dynamic",
                )

        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
