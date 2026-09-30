#!/usr/bin/env python3
"""Console buttons must be plain Chinese, not engineering jargon.

用户反馈 `redrive / steer / halt` 看不懂。控制台是给人点的地方，
`action_id` / `command_line` 这些标识可以留在折叠的「工程师技术详情」里，
但**标题、说明、点按钮后的效果**必须是中文人话，而且要能读出三者的区别。
"""

import re
import unittest
from pathlib import Path

from herdr import controller_actions as ca

ROOT = Path(__file__).resolve().parent.parent
CONSOLE_SRC = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")

# 只在面向用户的文案里禁止；task_id / 工位号是定位信息，允许出现。
JARGON = re.compile(
    r"re-?drive|steer\b|halt\b|herdr-task|herdr agent|prompt|"
    r"Controller|Worker|append-only|argv|subcommand|rebase|幂等|"
    r"finalize|integrate|commit\b|agent_done|paused|DAG",
    re.IGNORECASE,
)


def _task(status="working", **kw):
    task = {
        "task_id": "impl-x",
        "workflow_id": "wf-001",
        "node": "implementation",
        "stage": "implementation",
        "status": status,
        "agent": "opencode",
        "integration_mode": "git",
        "pane_id": "w1:p9",
    }
    task.update(kw)
    return task


def _all_actions():
    out = []
    for status in ("agent_done", "completed", "committed", "integrated",
                   "cleanup_ready", "working", "blocked", "rework"):
        out.extend(ca.generate_progress_actions(
            _task(status=status), {"workflow_id": "wf-001"}))
    out.extend(ca.generate_progress_actions(
        _task(status="committed", finalize_escalated=True,
              finalize_escalate_reason="并入时冲突"),
        {"workflow_id": "wf-001"}))
    out.extend(ca.collect_workflow_actions(
        [_task(status="blocked", stage_verdict="blocked", pane_id="")],
        {"workflow_id": "wf-001", "status": "paused"},
        workflow_paused=True,
    ))
    return out


class TestConsoleCopyIsPlainChinese(unittest.TestCase):
    def test_every_action_title_is_plain_chinese(self):
        actions = _all_actions()
        self.assertGreaterEqual(len(actions), 10, "fixture should produce many actions")
        for action in actions:
            with self.subTest(action=action.action_id):
                self.assertIsNone(
                    JARGON.search(action.title),
                    f"{action.action_id} 标题含术语: {action.title!r}",
                )

    def test_every_action_description_and_effect_is_plain_chinese(self):
        for action in _all_actions():
            for field in (action.description, action.effect):
                with self.subTest(action=action.action_id, text=field):
                    self.assertIsNone(
                        JARGON.search(field),
                        f"{action.action_id} 文案含术语: {field!r}",
                    )

    def test_titles_are_unique_per_task(self):
        """同一工位的按钮不能重名，否则用户无从选择。"""
        by_task = {}
        for action in ca.generate_progress_actions(
            _task(status="working"), {"workflow_id": "wf-001"},
        ):
            by_task.setdefault(action.blocker_task_id, []).append(action.title)
        for task_id, titles in by_task.items():
            self.assertEqual(len(titles), len(set(titles)), (task_id, titles))

    def test_live_pane_titles_convey_when_they_take_effect(self):
        """重推=立刻、留言=排队、叫停=中断，这是最容易混淆的三者。"""
        by_suffix = {
            a.action_id.rsplit(":", 1)[1]: a
            for a in ca.generate_progress_actions(
                _task(status="working"), {"workflow_id": "wf-001"},
            )
        }
        redrive, steer, halt = by_suffix["redrive"], by_suffix["steer"], by_suffix["halt"]

        self.assertRegex(redrive.title, r"立刻|马上")
        self.assertRegex(redrive.effect, r"立刻|马上")
        self.assertRegex(steer.title, r"排队|下一轮")
        self.assertRegex(steer.effect, r"下一轮|排队")
        self.assertRegex(halt.title, r"叫停|中断")
        self.assertRegex(halt.effect, r"中断|停止")

    def test_pipeline_titles_describe_the_outcome_not_the_command(self):
        titles = {
            a.action_id.rsplit(":", 1)[1]: a.title
            for status in ("agent_done", "completed", "committed",
                           "integrated", "cleanup_ready")
            for a in ca.generate_progress_actions(
                _task(status=status), {"workflow_id": "wf-001"},
            )
        }
        self.assertIn("目标分支", titles["integrate"], "并入动作应说明并入哪里")
        self.assertIn("归档", titles["finalize"])
        self.assertIn("归档", titles["cleanup"])

    # --- 前端静态文案 ---

    def test_console_has_no_user_visible_jargon(self):
        offenders = []
        for line in CONSOLE_SRC.splitlines():
            if "ctl-cheat-row" in line or "bin/herdr-task" in line:
                continue  # 折叠的工程师命令参考，允许保留原始命令
            for token in ("真实重驱", "实时插话指导", "工位紧急制动 (Halt)",
                          "insert 推进交付链路"):
                if token in line:
                    offenders.append((token, line.strip()[:120]))
        self.assertEqual(offenders, [], offenders)

    def test_console_uses_plain_section_names(self):
        self.assertIn("继续推进", CONSOLE_SRC)
        self.assertIn("留话指导", CONSOLE_SRC)
        self.assertIn("紧急叫停", CONSOLE_SRC)
        self.assertNotIn("推进交付链路", CONSOLE_SRC)

    def test_no_legacy_halt_wording_survives(self):
        """「紧急制动」曾以 halt reason 的形式留在源码里。

        它不渲染到页面（status_history 只显示 from → to），但会写进 halt
        事件的持久化记录 —— 属于用户可见文案体系的漏网之鱼，必须一起清掉。
        """
        for token in ("紧急制动", "实时插话", "真实重驱", "重驱工位"):
            with self.subTest(token=token):
                self.assertNotIn(
                    token, CONSOLE_SRC,
                    f"console 源码仍含旧术语: {token}",
                )


if __name__ == "__main__":
    unittest.main()
