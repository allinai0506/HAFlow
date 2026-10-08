#!/usr/bin/env python3
"""C07：parse_test_output 无证据输入不得虚构 1/1 计数。

审计复现：parse_test_output("", 0) 返回 (1, 1, []) → correctness=100，误导验收判据。
台账 C07 裁决：无证据输入不得虚构计数。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from herdr.evaluator import parse_test_output  # noqa: E402


class TestParseTestOutputC07:
    """C07 回归测试：无证据输入的 fallback 语义。"""

    def test_empty_output_exit_zero_returns_zero_zero_empty(self):
        """空字符串、exit_code=0 → (0, 0, [])，不虚构通过。"""
        assert parse_test_output("", 0) == (0, 0, [])

    def test_whitespace_only_exit_zero_returns_zero_zero_empty(self):
        """纯空白字符、exit_code=0 → (0, 0, [])。"""
        assert parse_test_output("   \n\t\n  ", 0) == (0, 0, [])

    def test_noise_output_exit_zero_returns_zero_one_failure(self):
        """不可识别噪音、exit_code=0 → (0, 1, [err_msg])。"""
        passed, total, failing = parse_test_output("random noise\n", 0)
        assert passed == 0
        assert total == 1
        assert len(failing) == 1

    def test_noise_output_exit_nonzero_returns_zero_one_failure(self):
        """不可识别噪音、exit_code=1 → (0, 1, [err_msg])。"""
        passed, total, failing = parse_test_output("boom\n", 1)
        assert passed == 0
        assert total == 1
        assert len(failing) == 1

    def test_newline_only_exit_nonzero_returns_zero_one_failure(self):
        """仅换行、exit_code=1 → (0, 1, [err_msg])。"""
        passed, total, failing = parse_test_output("\n", 1)
        assert passed == 0
        assert total == 1
        assert len(failing) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])