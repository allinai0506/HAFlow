"""Task-type vocabulary: node default_task_type must resolve to a launch --task-type choice.

D4 regression: the controller passes spec["task_type"] straight into
`herdr-task launch --task-type`, whose argparse choices are
fix/feat/refactor/docs/test/chore/perf/ci. A node with default_task_type: plan
(business-research-v1.yaml) made every dispatch die in argparse with
"invalid choice: 'plan'".
"""
import sys
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import direct_dispatch as dd


def _node(**overrides):
    node = {"id": "n1", "label": "节点", "purpose": "做点事", "default_integration_mode": "none"}
    node.update(overrides)
    return node


class TaskTypeVocabularyTest(unittest.TestCase):
    def test_choices_match_launch_argparse(self):
        self.assertEqual(
            set(dd.TASK_TYPE_CHOICES),
            {"fix", "feat", "refactor", "docs", "test", "chore", "perf", "ci"},
        )

    def test_business_task_type_is_kept_and_dispatched_safely(self):
        # default_task_type 也承载业务任务类型（如 quote），CLI 的技术枚举不适用于它：
        # 必须回落到默认技术类型安全派发，并保留原始声明。
        node = _node(default_task_type="quote")
        plan = dd.plan_stage_dispatch("wf-1", node, [], "需求")
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(plan["specs"][0]["task_type"], dd.DEFAULT_TASK_TYPE)
        self.assertEqual(dd.normalize_node(node)["declared_task_type"], "quote")

    def test_legacy_template_values_map_to_choices(self):
        expected = {"plan": "docs", "explore": "docs", "review": "test"}
        for legacy, mapped in expected.items():
            with self.subTest(legacy=legacy):
                node = _node(default_task_type=legacy)
                plan = dd.plan_stage_dispatch("wf-1", node, [], "需求")
                self.assertEqual(plan["mode"], "dispatch")
                self.assertEqual(plan["specs"][0]["task_type"], mapped)

    def test_missing_task_type_uses_default(self):
        plan = dd.plan_stage_dispatch("wf-1", _node(), [], "需求")
        self.assertEqual(plan["specs"][0]["task_type"], dd.DEFAULT_TASK_TYPE)

    def test_shipped_templates_only_use_valid_task_types(self):
        import pytest
        pytest.importorskip("yaml")  # test-only helper; the repo does not declare PyYAML
        import yaml
        for path in sorted((HERDR_ROOT / "workflow_templates").glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for node in data.get("nodes") or []:
                task_type = node.get("default_task_type")
                if task_type:
                    with self.subTest(template=path.name, node=node.get("id")):
                        self.assertIn(task_type, dd.TASK_TYPE_CHOICES)
