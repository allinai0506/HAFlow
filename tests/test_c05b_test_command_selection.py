#!/usr/bin/env python3
"""C05b：多栈仓库不得凭根目录 package.json 猜测试命令。

台账裁决：显式 --test-cmd 契约优先；缺契约遇歧义必须可恢复拒绝，
不能猜 Java 默认。审计复现：前端+Java 后端并存项目、目标为后端任务，
仍生成 CI=1 npm test；纯 Java 仓库则落到错误的 pytest 回退。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from herdr.evaluator import select_default_test_command  # noqa: E402
from tests.test_fix_loop_pr1 import _load_module  # noqa: E402

_cli = _load_module("c05b_cli", ROOT / "bin" / "herdr-task")


class SelectDefaultTestCommandTest:
    """纯决策真值表：栈标记 → 命令或可恢复拒绝。"""

    def test_single_javascript_stack_keeps_existing_command(self):
        assert select_default_test_command({"package_json": True}) == {
            "command": "CI=1 npm test"
        }

    def test_single_python_stack_keeps_existing_command(self):
        for markers in ({"pytest_ini": True}, {"tests_dir": True}):
            assert select_default_test_command(markers) == {"command": "pytest"}

    def test_single_rust_stack_keeps_existing_command(self):
        assert select_default_test_command({"cargo_toml": True}) == {
            "command": "cargo test"
        }

    def test_mixed_stack_is_a_recoverable_refusal(self):
        decision = select_default_test_command(
            {"package_json": True, "pom_xml": True}
        )
        assert decision["refusal"] == "multi_stack_ambiguous"
        assert "package.json" in decision["detail"]
        assert "pom.xml" in decision["detail"]

    def test_gradle_counts_as_java_stack(self):
        decision = select_default_test_command(
            {"package_json": True, "build_gradle": True}
        )
        assert decision["refusal"] == "multi_stack_ambiguous"

    def test_java_only_is_refused_not_guessed(self):
        """不能猜 Java 默认：纯 Java 仓库缺显式契约必须可恢复拒绝。"""
        for markers in ({"pom_xml": True}, {"build_gradle": True}):
            decision = select_default_test_command(markers)
            assert decision["refusal"] == "java_without_explicit_contract"

    def test_java_with_python_tests_is_multi_stack(self):
        decision = select_default_test_command({"pom_xml": True, "tests_dir": True})
        assert decision["refusal"] == "multi_stack_ambiguous"

    def test_no_markers_falls_back_to_existing_pytest_default(self):
        assert select_default_test_command({}) == {"command": "pytest"}


class AutoInitTestCommandContractTest:
    """壳层契约：探测→裁决透传；拒绝时 init_loop 不得被调用。"""

    @staticmethod
    def _make_clone(tmp_path, markers):
        clone = tmp_path / "clone"
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

    def test_multi_stack_clone_refuses_without_guessing(self, tmp_path, monkeypatch):
        clone = self._make_clone(tmp_path, {"package_json": True, "pom_xml": True})

        def forbidden_init(**kwargs):
            pytest.fail("refused launch must not init the loop with a guessed command")

        monkeypatch.setattr("herdr.evaluator.init_loop", forbidden_init)
        result = _cli.auto_init_task_loop(
            clone, "backend task", [], node="implementation", task_type="fix"
        )
        assert result and result["refused"] == "multi_stack_ambiguous"

    def test_explicit_test_cmd_contract_takes_priority(self, tmp_path, monkeypatch):
        """多栈仓库 + 显式契约：不拒绝，init_loop 收到显式命令。"""
        clone = self._make_clone(tmp_path, {"package_json": True, "pom_xml": True})
        captured = {}
        monkeypatch.setattr(
            "herdr.evaluator.init_loop", lambda **kwargs: captured.update(kwargs)
        )
        result = _cli.auto_init_task_loop(
            clone, "backend task", [], test_cmd="mvn -q test",
            node="implementation", task_type="fix",
        )
        assert result is None
        assert captured["test_cmd"] == "mvn -q test"

    def test_single_stack_clone_keeps_existing_default(self, tmp_path, monkeypatch):
        clone = self._make_clone(tmp_path, {"package_json": True})
        captured = {}
        monkeypatch.setattr(
            "herdr.evaluator.init_loop", lambda **kwargs: captured.update(kwargs)
        )
        result = _cli.auto_init_task_loop(
            clone, "frontend task", [], node="implementation", task_type="fix"
        )
        assert result is None
        assert captured["test_cmd"] == "CI=1 npm test"

    def test_java_only_clone_refuses_instead_of_pytest_guess(
            self, tmp_path, monkeypatch):
        clone = self._make_clone(tmp_path, {"pom_xml": True})

        def forbidden_init(**kwargs):
            pytest.fail("pytest must not be guessed for a Java-only repo")

        monkeypatch.setattr("herdr.evaluator.init_loop", forbidden_init)
        result = _cli.auto_init_task_loop(
            clone, "java task", [], node="implementation", task_type="fix"
        )
        assert result and result["refused"] == "java_without_explicit_contract"
