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
        from herdr.task_cli_launch_preflight import validate_supersedes_for_launch
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
            validate_supersedes_for_launch(args_no_supersedes, "review", mock_store)
        self.assertIn("pending replacement obligation", str(ctx.exception))
        self.assertIn("--supersedes", str(ctx.exception))

        # With --supersedes -> should pass cleanly
        args_with_supersedes = MagicMock(
            workflow_id="wf-1",
            supersedes="task-old",
            dispatch_role="worker",
        )
        # Should not raise
        validate_supersedes_for_launch(args_with_supersedes, "review", mock_store)

    def test_begin_launch_intent_candidate_rotation_resets_round_to_one(self):
        """When rotating to a fresh candidate SHA, begin_launch_intent permits dispatch_round=1."""
        from herdr.task_resources import begin_launch_intent
        mock_store = MagicMock()
        mock_store.read_workflow_snapshot.return_value = ({}, {}, [])
        mock_store.get_task.return_value = {
            "task_id": "task-old",
            "workflow_id": "wf-1",
            "node": "review",
            "stage": "review",
            "status": "superseded",
            "dispatch_role": "worker",
            "dispatch_round": 2,
            "candidate_sha": "a" * 40,
        }
        mock_store.list_tasks.return_value = []
        mock_store.list_events.return_value = []
        mock_conn = MagicMock()
        with patch("herdr.state_db.get_readonly_db_connection", return_value=mock_conn), \
             patch("herdr.task_resources.find_dispatch_task", return_value=None), \
             patch("herdr.task_resources._latest_intent", return_value=None), \
             patch("herdr.task_resources._launch_event"), \
             patch("herdr.dispatch_recovery.require_authorization"):
            # dispatch_round=1 on fresh candidate "b"*40 superseding task-old with "a"*40 candidate
            claim = begin_launch_intent(
                mock_store,
                workflow_id="wf-1",
                node_id="review",
                role="worker",
                candidate_sha="b" * 40,
                dispatch_round=1,
                task_id="task-new",
                supersedes="task-old",
            )
            self.assertEqual(claim["status"], "claimed")
            self.assertEqual(claim["intent"]["dispatch_round"], 1)

    def test_begin_launch_intent_non_candidate_stage_requires_round_advancement(self):
        """Non-candidate stage (empty candidate_sha on both old and new) must enforce round increment."""
        from herdr.task_resources import begin_launch_intent
        mock_store = MagicMock()
        mock_store.read_workflow_snapshot.return_value = ({}, {}, [])
        mock_store.get_task.return_value = {
            "task_id": "task-old",
            "workflow_id": "wf-1",
            "node": "craft",
            "stage": "craft",
            "status": "failed",
            "dispatch_role": "worker",
            "dispatch_round": 1,
            "candidate_sha": "",
        }
        mock_conn = MagicMock()
        with patch("herdr.state_db.get_readonly_db_connection", return_value=mock_conn), \
             patch("herdr.dispatch_recovery.require_authorization"), \
             self.assertRaises(ValueError) as ctx:
            begin_launch_intent(
                mock_store,
                workflow_id="wf-1",
                node_id="craft",
                role="worker",
                candidate_sha="",
                dispatch_round=1,  # Attempting round 1 when predecessor was round 1
                task_id="task-new",
                supersedes="task-old",
            )
        self.assertIn("supersedes must bind previous round", str(ctx.exception))

    def test_begin_launch_intent_fresh_candidate_rotation_rejects_round_greater_than_one(self):
        """Fresh candidate rotation must strictly require dispatch_round=1."""
        from herdr.task_resources import begin_launch_intent
        mock_store = MagicMock()
        mock_store.read_workflow_snapshot.return_value = ({}, {}, [])
        mock_store.get_task.return_value = {
            "task_id": "task-old",
            "workflow_id": "wf-1",
            "node": "review",
            "stage": "review",
            "status": "superseded",
            "dispatch_role": "worker",
            "dispatch_round": 2,
            "candidate_sha": "a" * 40,
        }
        mock_store.list_tasks.return_value = []
        mock_conn = MagicMock()
        with patch("herdr.state_db.get_readonly_db_connection", return_value=mock_conn), \
             patch("herdr.dispatch_recovery.require_authorization"), \
             self.assertRaises(ValueError) as ctx:
            begin_launch_intent(
                mock_store,
                workflow_id="wf-1",
                node_id="review",
                role="worker",
                candidate_sha="b" * 40,
                dispatch_round=2,  # Fresh candidate must be round 1, not 2
                task_id="task-new",
                supersedes="task-old",
            )
        self.assertIn("fresh candidate round requires dispatch_round=1", str(ctx.exception))

    def test_orchestrator_candidate_rotation_with_sparse_predecessor_resets_round_to_one(self):
        """When predecessor has sparse candidate field, fresh candidate resets dispatch_round to 1."""
        from collections import defaultdict
        from types import SimpleNamespace
        from herdr.task_cli_launch_orchestrator import launch_task_orchestrated
        args = SimpleNamespace(
            workflow_id="wf-1",
            node="review",
            task_id="task-new",
            supersedes="task-old",
            candidate_sha="b" * 40,
            dispatch_round=None,
            dispatch_role="worker",
            agent="auto",
            task_type="review",
            integration_mode="none",
            onto=None,
            source=None,
            goal="review",
            acceptance=[],
            prompt="review",
            agent_policy=None,
            dry_run=True,
        )
        mock_store = MagicMock()
        mock_store.read_workflow_snapshot.return_value = ({}, {}, [])
        mock_store.get_task.return_value = {
            "task_id": "task-old",
            "workflow_id": "wf-1",
            "node": "review",
            "dispatch_round": 2,
            "candidate_sha": "",  # sparse predecessor candidate
        }
        deps = defaultdict(MagicMock)
        deps["BranchOwnershipError"] = Exception
        deps["_get_store"] = lambda: mock_store
        deps["get_store"] = lambda: mock_store
        deps["_is_isolation_rejection"] = lambda exc: False
        deps["_is_gate_task"] = lambda *a: False
        deps["execution_fields_from_workflow"] = lambda *a: {"execution_mode": "none"}
        deps["project_for_workflow"] = lambda *a: {"project_id": "p", "project_root": "/tmp", "execution": {"mode": "none"}}
        deps["workflow_config_for"] = lambda *a: {"nodes": [{"id": "review"}]}
        deps["find_dispatch_task"] = lambda *a, **k: None
        deps["find_task"] = lambda *a, **k: None
        mock_conn = MagicMock()
        with patch("herdr.state_db.get_readonly_db_connection", return_value=mock_conn), \
             patch("herdr.dispatch_recovery.require_authorization"), \
             patch("herdr.task_resources.find_dispatch_task", return_value=None), \
             patch("herdr.task_resources._latest_intent", return_value=None), \
             patch("herdr.task_resources._launch_event"), \
             patch("herdr.task_resources.record_launch_resources", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                launch_task_orchestrated(args, deps=deps)
        self.assertEqual(args.dispatch_round, 1)


if __name__ == "__main__":
    unittest.main()
