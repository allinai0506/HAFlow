import importlib.machinery
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import direct_dispatch as dd


def _import_herdr_task():
    task_bin = HERDR_ROOT / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader(
        "herdr_task_bin_test",
        importlib.machinery.SourceFileLoader("herdr_task_bin_test", str(task_bin)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDispatchSupersedeEnforcement(unittest.TestCase):
    def test_plan_stage_dispatch_redispatch_populates_supersedes(self):
        """When planning redispatch for awaiting tasks, 'supersedes' must be populated."""
        node = {
            "id": "review",
            "label": "6评审",
            "purpose": "评审代码",
            "task_type": "review",
            "integration_mode": "none",
            "required_outputs": ["评审意见"],
        }
        old_task = {
            "task_id": "wf-1-review-auto",
            "workflow_id": "wf-1",
            "node": "review",
            "stage": "review",
            "status": "superseded",
            "replacement_pending": True,
            "superseded_by": None,
            "dispatch_role": "reviewer",
            "dispatch_round": 1,
            "created_at": 1000.0,
        }
        plan = dd.plan_stage_dispatch(
            "wf-1",
            node,
            [old_task],
            "审核修改",
        )
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(len(plan["specs"]), 1)
        spec = plan["specs"][0]
        self.assertEqual(spec["redispatch_of"], "wf-1-review-auto")
        self.assertEqual(spec["supersedes"], "wf-1-review-auto")
        self.assertEqual(spec["dispatch_round"], 2)

    def test_controller_redispatch_cmd_includes_supersedes(self):
        """Controller's direct dispatch must pass --supersedes when redispatching."""
        # Load controller module dynamically
        spec = importlib.util.spec_from_loader(
            "ctrl_test",
            importlib.machinery.SourceFileLoader(
                "ctrl_test", str(HERDR_ROOT / "services" / "herdr-controller.py")
            ),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        mock_plan = {
            "mode": "dispatch",
            "reason": "redispatch",
            "specs": [{
                "task_id": "wf-1-review-auto-2",
                "goal": "review goal",
                "prompt": "review prompt",
                "agent": "claude",
                "task_type": "review",
                "integration_mode": "none",
                "acceptance": ["pass"],
                "redispatch_of": "wf-1-review-auto",
                "supersedes": "wf-1-review-auto",
            }],
        }
        with patch.object(mod.direct_dispatch_planner, "plan_stage_dispatch", return_value=mock_plan), \
             patch("subprocess.run") as mock_run, \
             patch.object(mod, "get_task", return_value={"status": "completed"}), \
             patch.object(mod, "project_for_workflow", return_value={
                 "startup_ready": True,
                 "project_root": "/tmp/test",
                 "coordinator_pane_id": "p1",
                 "requirement": "req"
             }), \
             patch.object(mod, "_dispatch_candidate_ready", return_value=True):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            item = {
                "workflow_id": "wf-1",
                "node_id": "review",
                "node": {
                    "id": "review",
                    "label": "6评审",
                    "purpose": "评审代码",
                    "task_type": "review",
                    "integration_mode": "none",
                    "required_outputs": ["评审意见"],
                }
            }
            handled = mod.try_direct_stage_advance(item)
            self.assertTrue(handled)
            self.assertTrue(mock_run.called)
            launch_calls = [c[0][0] for c in mock_run.call_args_list if "launch" in c[0][0]]
            self.assertTrue(len(launch_calls) >= 1)
            called_cmd = launch_calls[0]
            self.assertIn("--supersedes", called_cmd)
            idx = called_cmd.index("--supersedes")
            self.assertEqual(called_cmd[idx + 1], "wf-1-review-auto")

    def test_validate_supersedes_for_launch_rejection(self):
        """Launching in a node with an unreplaced obligation or failed task must reject without --supersedes."""
        ht = _import_herdr_task()
        mock_store = MagicMock()
        mock_store.list_tasks.return_value = [{
            "task_id": "task-old",
            "workflow_id": "wf-1",
            "node": "review",
            "status": "superseded",
            "replacement_pending": True,
            "superseded_by": None,
            "dispatch_role": "worker",
        }]

        # Without --supersedes -> should raise ValueError
        args_no_supersedes = MagicMock(
            workflow_id="wf-1",
            supersedes=None,
            dispatch_role="worker",
        )
        with self.assertRaises(ValueError) as ctx:
            ht._validate_supersedes_for_launch(args_no_supersedes, "review", mock_store)
        self.assertIn("pending replacement obligation", str(ctx.exception))
        self.assertIn("--supersedes", str(ctx.exception))

        # With --supersedes -> should pass cleanly
        args_with_supersedes = MagicMock(
            workflow_id="wf-1",
            supersedes="task-old",
            dispatch_role="worker",
        )
        # Should not raise
        ht._validate_supersedes_for_launch(args_with_supersedes, "review", mock_store)


if __name__ == "__main__":
    unittest.main()
