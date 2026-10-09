#!/usr/bin/env python3
"""#158 返工判重指纹依赖 Task ID 的回归测试。

审计复现：verdict_fingerprint 按 task_id 排序，导致后继代任务（task_id 变化）
重记录同一语义结论时，指纹发生变化，is_repeat_verdict 失配，
fix-loop 反复下发并扣预算。

修复：排序键从 task_id 改为阻塞器的语义内容（note），保持指纹跨代稳定。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from herdr.fix_loop import verdict_fingerprint, is_repeat_verdict


class TestVerdictFingerprintCrossGeneration(unittest.TestCase):
    """跨代任务指纹稳定性真值表。"""

    def test_same_semantics_different_task_ids_same_fingerprint(self):
        """同一语义结论，不同代任务 ID（r3→r4），指纹必须一致。"""
        fp_r3 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "test-01-r3", "note": "missing engine"}]
        )
        fp_r4 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "test-01-r4", "note": "missing engine"}]
        )
        self.assertEqual(fp_r3, fp_r4,
            "跨代任务 ID 变化时，指纹必须保持一致")

    def test_different_notes_different_fingerprint(self):
        """Note 变化时指纹必须变化（语义不同）。"""
        fp1 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "test-01-r3", "note": "missing engine"}]
        )
        fp2 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "test-01-r3", "note": "missing CLI"}]
        )
        self.assertNotEqual(fp1, fp2,
            "Note 变化时指纹必须变化")

    def test_different_branch_different_fingerprint(self):
        """Branch 变化时指纹必须变化。"""
        fp1 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "test-01-r3", "note": "missing engine"}]
        )
        fp2 = verdict_fingerprint(
            "agent/c",
            [{"task_id": "test-01-r3", "note": "missing engine"}]
        )
        self.assertNotEqual(fp1, fp2,
            "Branch 变化时指纹必须变化")

    def test_affected_task_ids_sensitivity_preserved(self):
        """affected_task_ids 的目标敏感性保持（PR #110）。"""
        fp1 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "test-01-r3", "note": "missing engine"}],
            affected_task_ids=["task-B"]
        )
        fp2 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "test-01-r3", "note": "missing engine"}],
            affected_task_ids=["task-C"]
        )
        self.assertNotEqual(fp1, fp2,
            "affected_task_ids 变化时指纹必须变化")

    def test_blocker_order_irrelevant(self):
        """阻塞器顺序不影响指纹（去重后的语义一致性）。"""
        blockers_a = [
            {"task_id": "t1", "note": "blocker A"},
            {"task_id": "t2", "note": "blocker B"},
        ]
        blockers_b = [
            {"task_id": "t2", "note": "blocker B"},
            {"task_id": "t1", "note": "blocker A"},
        ]
        fp_a = verdict_fingerprint("branch-x", blockers_a)
        fp_b = verdict_fingerprint("branch-x", blockers_b)
        self.assertEqual(fp_a, fp_b,
            "阻塞器顺序不应影响指纹")

    def test_multiple_blockers_same_note_different_ids(self):
        """多个阻塞器同 note 不同 ID，视为同一语义去重。"""
        fp1 = verdict_fingerprint(
            "agent/b",
            [
                {"task_id": "t1", "note": "same note"},
                {"task_id": "t2", "note": "same note"},
            ]
        )
        fp2 = verdict_fingerprint(
            "agent/b",
            [{"task_id": "t1", "note": "same note"}]
        )
        self.assertEqual(fp1, fp2,
            "同 note 的多阻塞器应去重，不增加指纹复杂度")


class TestIsRepeatVerdict(unittest.TestCase):
    """is_repeat_verdict 语义回归。"""

    def test_same_fingerprint_is_repeat(self):
        fp = verdict_fingerprint("b", [{"task_id": "t", "note": "n"}])
        self.assertTrue(is_repeat_verdict(fp, fp))

    def test_different_fingerprint_not_repeat(self):
        fp1 = verdict_fingerprint("b", [{"task_id": "t", "note": "n"}])
        fp2 = verdict_fingerprint("c", [{"task_id": "t", "note": "n"}])
        self.assertFalse(is_repeat_verdict(fp1, fp2))

    def test_none_or_empty_not_repeat(self):
        fp = verdict_fingerprint("b", [{"task_id": "t", "note": "n"}])
        for bad in (None, "", "other"):
            self.assertFalse(is_repeat_verdict(fp, bad), bad)
            self.assertFalse(is_repeat_verdict(bad, fp), bad)
        self.assertFalse(is_repeat_verdict("", ""))
        self.assertFalse(is_repeat_verdict(None, fp))


if __name__ == "__main__":
    unittest.main()