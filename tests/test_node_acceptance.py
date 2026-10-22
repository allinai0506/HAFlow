"""Same-node redo for a node that is not a code gate.

A returned blocked result is sent back to that node once. A second blocked
result stays blocked and is complete enough to advance. Code gates stay on
the existing fix loop.
"""

import unittest

from herdr.direct_dispatch import plan_stage_dispatch
from herdr.scheduler import node_verdict
from herdr.workflow_progress import assess_workflow


def _task(task_id, status, node, **extra):
    task = {
        "task_id": task_id,
        "workflow_id": "wf",
        "node": node,
        "stage": node,
        "status": status,
        "version": 3,
        "created_at": 100.0,
    }
    task.update(extra)
    return task


def _nodes():
    return {
        "nodes": [
            {"id": "requirements", "label": "需求", "purpose": "写清需求"},
            {"id": "plan", "label": "计划"},
            {"id": "test", "label": "测试"},
            {"id": "review", "gate": {"retry_node": "implementation"}},
        ]
    }


class AcceptanceDecisionTest(unittest.TestCase):
    def _actions(self, tasks, nodes=None, gate_ids=()):
        from herdr.node_acceptance import acceptance_actions

        return acceptance_actions(
            nodes if nodes is not None else _nodes(),
            tasks,
            "wf",
            gate_ids=gate_ids,
        )

    def test_first_blocked_result_redoes_every_live_task_once(self):
        actions = self._actions([
            _task("req-exec", "cleaned", "requirements", stage_verdict="pass"),
            _task("req-ch", "cleaned", "requirements", stage_verdict="blocked",
                  stage_verdict_note="B1 端口绑错"),
        ])
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action["op"], "rework")
        self.assertEqual(action["node_id"], "requirements")
        self.assertEqual(
            sorted(task["task_id"] for task in action["tasks"]),
            ["req-ch", "req-exec"],
        )
        self.assertIn("已知限制", action["note"])
        self.assertIn("B1 端口绑错", action["note"])

    def test_second_blocked_result_is_accepted_without_another_redo(self):
        actions = self._actions([
            _task("req-ch", "superseded", "requirements", stage_verdict="blocked",
                  stage_verdict_note="B1"),
            _task("req-exec", "cleaned", "requirements", stage_verdict="pass",
                  supersedes="req-exec-old"),
            _task("req-ch2", "cleaned", "requirements", stage_verdict="blocked",
                  stage_verdict_note="B1 仍在", supersedes="req-ch"),
        ])
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action["op"], "accept")
        self.assertEqual([task["task_id"] for task in action["tasks"]], ["req-ch2"])

    def test_running_task_is_left_alone(self):
        actions = self._actions([
            _task("req-exec", "cleaned", "requirements", stage_verdict="blocked",
                  stage_verdict_note="B1"),
            _task("req-ch", "working", "requirements"),
        ])
        self.assertEqual(actions, [])

    def test_empty_gate_object_does_not_make_requirements_a_code_gate(self):
        nodes = {"nodes": [
            {"id": "requirements", "node_type": "agent", "gate": {}},
            {"id": "plan", "node_type": "agent", "gate": {}},
            {"id": "test", "node_type": "agent", "gate": {}},
            {"id": "review", "node_type": "agent", "gate": {}},
        ]}
        actions = self._actions([
            _task("req-old", "superseded", "requirements", stage_verdict="blocked"),
            _task("req-exec", "cleaned", "requirements", stage_verdict="pass"),
            _task("req-ch2", "cleaned", "requirements", stage_verdict="blocked",
                  stage_verdict_note="B1 仍在"),
        ], nodes=nodes)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["op"], "accept")
        self.assertEqual(actions[0]["node_id"], "requirements")
        self.assertEqual(
            [task["task_id"] for task in actions[0]["tasks"]], ["req-ch2"])

    def test_empty_gate_object_still_resolves_named_code_gates(self):
        from services.controller import node_policy

        class Ports:
            GATE_DEFAULTS = {
                "test": {"retry_node": "implementation"},
                "review": {"retry_node": "implementation"},
                "wrapup": {"retry_node": "implementation"},
            }
            FIX_LOOP_MAX = 3
            get_stage_policy = staticmethod(lambda node_id: {})

        self.assertIsNone(node_policy.resolve_gate_config(
            Ports, {"id": "requirements", "node_type": "agent", "gate": {}},
            "requirements"))
        self.assertIsNotNone(node_policy.resolve_gate_config(
            Ports, {"id": "test", "node_type": "agent", "gate": {}}, "test"))
        self.assertIsNotNone(node_policy.resolve_gate_config(
            Ports, {"id": "custom_gate", "node_type": "agent", "gate": {}},
            "custom_gate"))
        explicit = node_policy.resolve_gate_config(
            Ports, {"id": "requirements", "gate": {"retry_node": "plan"}},
            "requirements")
        self.assertEqual(explicit["retry_node"], "plan")

    def test_code_gate_is_not_redone_here(self):
        actions = self._actions([
            _task("rev", "cleaned", "review", stage_verdict="blocked",
                  stage_verdict_note="P0"),
            _task("tested", "cleaned", "test", stage_verdict="blocked",
                  stage_verdict_note="失败"),
        ])
        self.assertEqual(actions, [])

    def test_stage_policy_gate_id_is_not_redone(self):
        actions = self._actions(
            [_task("qa", "cleaned", "qa", stage_verdict="blocked",
                   stage_verdict_note="P0")],
            nodes={"nodes": [{"id": "qa"}]},
            gate_ids={"qa"},
        )
        self.assertEqual(actions, [])

    def test_already_accepted_residual_is_not_rewritten(self):
        actions = self._actions([
            _task("req-ch", "superseded", "requirements", stage_verdict="blocked"),
            _task("req-ch2", "cleaned", "requirements", stage_verdict="blocked",
                  accepted_with_limits=True, supersedes="req-ch"),
        ])
        self.assertEqual(actions, [])

    def test_pass_after_a_redo_does_not_accept_or_rework(self):
        actions = self._actions([
            _task("req-ch", "superseded", "requirements", stage_verdict="blocked"),
            _task("req-ch2", "cleaned", "requirements", stage_verdict="pass",
                  supersedes="req-ch"),
        ])
        self.assertEqual(actions, [])


class AcceptanceEffectTest(unittest.TestCase):
    def test_known_limit_does_not_keep_the_workflow_blocked(self):
        workflow = {"workflow_id": "wf", "status": "running", "execution_id": "e1"}
        config = {"nodes": [{"id": "requirements"}]}
        blocked = _task(
            "req-ch2", "cleaned", "requirements", stage_verdict="blocked",
            accepted_with_limits=True, execution_id="e1",
        )
        result = assess_workflow(workflow, config, [
            _task("req-exec", "cleaned", "requirements", stage_verdict="pass",
                  execution_id="e1"),
            blocked,
        ])
        self.assertTrue(result["can_advance"])
        self.assertEqual(result["obligations"], [])
        self.assertEqual(blocked["stage_verdict"], "blocked")

    def test_unaccepted_block_still_stops_advance(self):
        workflow = {"workflow_id": "wf", "status": "running", "execution_id": "e1"}
        config = {"nodes": [{"id": "requirements"}]}
        result = assess_workflow(workflow, config, [
            _task("req-ch", "cleaned", "requirements", stage_verdict="blocked",
                  execution_id="e1", stage_verdict_note="B1"),
        ])
        self.assertFalse(result["can_advance"])

    def test_node_verdict_ignores_known_limit_and_keeps_a_real_block(self):
        self.assertIsNone(node_verdict([
            _task("a", "cleaned", "requirements", stage_verdict="blocked",
                  accepted_with_limits=True),
        ]))
        self.assertEqual(node_verdict([
            _task("a", "cleaned", "requirements", stage_verdict="pass"),
            _task("b", "cleaned", "requirements", stage_verdict="blocked",
                  accepted_with_limits=True),
        ]), "pass")
        self.assertEqual(node_verdict([
            _task("a", "cleaned", "requirements", stage_verdict="blocked"),
        ]), "blocked")

    def test_redispatch_prompt_carries_the_known_limit_note(self):
        node = {
            "id": "requirements",
            "label": "需求",
            "purpose": "写清需求",
            "required_outputs": ["需求说明"],
            "rules": [],
            "default_task_type": "docs",
            "default_integration_mode": "none",
        }
        plan = plan_stage_dispatch("wf", node, [
            _task("req-ch", "superseded", "requirements",
                  stage_verdict="blocked",
                  stage_verdict_note="原文",
                  same_node_redo_note="请写成已知限制\nreq-ch: B1 端口绑错",
                  goal="重写需求"),
        ], "需求")
        self.assertEqual(plan["mode"], "dispatch")
        prompt = plan["specs"][0]["prompt"]
        self.assertIn("已知限制", prompt)
        self.assertIn("B1 端口绑错", prompt)


if __name__ == "__main__":
    unittest.main()
