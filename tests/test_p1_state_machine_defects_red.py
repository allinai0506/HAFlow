"""Regression tests for P1 state machine defects: F-01 and F-03.

F-01: finalize_escalated task must NOT be considered complete by node_is_complete
      or stage_state.is_node_complete, preventing premature node advancement and
      spurious [WORKFLOW COMPLETE] close loops.
F-03: Direct dispatch in causal wait mode (mode == "wait") must NOT write the
      'notified' lock into stage-state.json, preventing permanent dispatch deadlock
      when waiting for prerequisite deliverables or before physical tasks register.
"""

import json
from pathlib import Path
import tempfile
import pytest

from herdr.scheduler import node_is_complete
from herdr.state_db import init_db, save_workflow
from herdr.state_store import get_state_store
from herdr import node_dispatch_store as nds


# ---------------------------------------------------------------------------
# F-01: finalize_escalated Node Completion Alignment
# ---------------------------------------------------------------------------

class TestF01FinalizeEscalatedCompletion:
    """Verify that finalize_escalated tasks never satisfy node completion."""

    def test_single_task_node_is_complete_refuses_finalize_escalated(self):
        node = {"id": "impl", "node_type": "agent"}
        task = {
            "task_id": "wf-1:impl:t1",
            "workflow_id": "wf-1",
            "node": "impl",
            "status": "integrated",
            "integration_mode": "git",
            "finalize_escalated": True,
        }
        # RED assertion: node_is_complete must return False when finalize_escalated is True
        assert node_is_complete([task], node=node) is False, (
            "node_is_complete must not treat finalize_escalated tasks as complete"
        )

        # When escalation is cleared, completion is restored
        task["finalize_escalated"] = False
        assert node_is_complete([task], node=node) is True

    def test_multi_role_node_is_complete_refuses_finalize_escalated_role(self):
        node = {
            "id": "plan",
            "node_type": "agent",
            "roles": [
                {"name": "executor"},
                {"name": "challenger", "depends_on_role": "executor"},
            ],
        }
        t_exec = {
            "task_id": "wf-1-plan-executor",
            "workflow_id": "wf-1",
            "node": "plan",
            "role": "executor",
            "status": "integrated",
            "integration_mode": "git",
            "finalize_escalated": True,
        }
        t_challenger = {
            "task_id": "wf-1-plan-challenger",
            "workflow_id": "wf-1",
            "node": "plan",
            "role": "challenger",
            "status": "completed",
            "integration_mode": "none",
        }
        tasks = [t_exec, t_challenger]

        assert node_is_complete(tasks, node=node) is False, (
            "Multi-role node must not be complete when any role task has finalize_escalated=True"
        )

        # Resolving escalation allows multi-role completion
        t_exec["finalize_escalated"] = False
        assert node_is_complete(tasks, node=node) is True

    def test_stage_state_fallback_refuses_finalize_escalated(self):
        from services.controller.stage_state import is_node_complete as stage_is_node_complete

        class DummyPorts:
            scheduler_core = None
            _get_store = None
            def load_tasks(self):
                return []
            def _reverification_satisfies_node(self, wid, nid):
                return False
            def workflow_config_for(self, wid):
                return {"nodes": [{"id": "impl"}]}

        ports = DummyPorts()
        task = {
            "task_id": "wf-1:impl:t1",
            "workflow_id": "wf-1",
            "node": "impl",
            "status": "integrated",
            "integration_mode": "git",
            "finalize_escalated": True,
        }

        # Call stage_state fallback when scheduler_core is None
        ports.load_tasks = lambda: [task]
        assert stage_is_node_complete(ports, "wf-1", "impl") is False, (
            "stage_state.is_node_complete fallback must not treat finalize_escalated as complete"
        )

        task["finalize_escalated"] = False
        assert stage_is_node_complete(ports, "wf-1", "impl") is True

    def test_superseded_escalated_task_does_not_block_replacement(self):
        node = {"id": "impl", "node_type": "agent"}
        old_task = {
            "task_id": "wf-1:impl:t1",
            "workflow_id": "wf-1",
            "node": "impl",
            "status": "superseded",
            "integration_mode": "git",
            "finalize_escalated": True,
            "superseded_by": "wf-1:impl:t1-r2",
        }
        new_task = {
            "task_id": "wf-1:impl:t1-r2",
            "workflow_id": "wf-1",
            "node": "impl",
            "status": "integrated",
            "integration_mode": "git",
            "finalize_escalated": False,
        }
        # The superseded task had escalation, but the replacement succeeded
        assert node_is_complete([old_task, new_task], node=node) is True

    def test_workflow_close_defer_loop_prevented_by_node_completion_alignment(self):
        """When a task has finalize_escalated, is_node_complete returns False,
        so is_workflow_completed returns False and controller never enters
        spurious [WORKFLOW COMPLETE] close-deferral loop.
        """
        from herdr.workflow import is_workflow_completed
        from services.controller.stage_state import is_node_complete

        class DummyPorts:
            import herdr.scheduler as scheduler_core
            def load_tasks(self):
                return [{
                    "task_id": "wf-close:impl:t1",
                    "workflow_id": "wf-close",
                    "node": "impl",
                    "status": "integrated",
                    "integration_mode": "git",
                    "finalize_escalated": True,
                }]
            _get_store = None
            def _reverification_satisfies_node(self, wid, nid):
                return False
            def workflow_config_for(self, wid):
                return {"nodes": [{"id": "impl"}]}

        ports = DummyPorts()
        wf_cfg = {"nodes": [{"id": "impl"}]}

        # Node is NOT complete because of finalize_escalated
        assert is_node_complete(ports, "wf-close", "impl") is False

        completed_nodes = {
            n["id"] for n in wf_cfg["nodes"]
            if is_node_complete(ports, "wf-close", n["id"])
        }
        assert "impl" not in completed_nodes
        assert is_workflow_completed(wf_cfg, completed_nodes) is False

        # When human resolves escalation
        ports.load_tasks = lambda: [{
            "task_id": "wf-close:impl:t1",
            "workflow_id": "wf-close",
            "node": "impl",
            "status": "integrated",
            "integration_mode": "git",
            "finalize_escalated": False,
        }]
        assert is_node_complete(ports, "wf-close", "impl") is True
        completed_nodes_resolved = {
            n["id"] for n in wf_cfg["nodes"]
            if is_node_complete(ports, "wf-close", n["id"])
        }
        assert is_workflow_completed(wf_cfg, completed_nodes_resolved) is True


# ---------------------------------------------------------------------------
# F-03: Direct Dispatch Causal Wait Must Not Latch 'notified'
# ---------------------------------------------------------------------------

class TestF03DirectDispatchCausalWaitLatch:
    """Verify that causal wait mode in direct dispatch does not latch 'notified'."""

    def test_try_direct_stage_advance_wait_mode_clears_latch_and_defers_operation(self, tmp_path):
        from services.controller.direct_dispatch import try_direct_stage_advance
        from services.controller.stage_state import (
            mark_stage_advance_queued,
            load_stage_state,
            save_stage_state,
            clear_stage_advance,
            stage_advance_key,
        )

        db_path = tmp_path / "state.db"
        init_db(db_path)
        import time
        now = time.time()
        wf_cfg = {
            "nodes": [{"id": "plan", "node_type": "agent"}],
        }
        save_workflow({"workflow_id": "wf-test", "status": "running", "config": wf_cfg}, db_path=db_path)

        state_file = tmp_path / "stage-state.json"
        state_file.write_text("{}")

        import threading
        state_lock = threading.RLock()

        real_store = get_state_store(db_path)
        class MockPlanner:
            def merge_node_policy(self, node, policy):
                return node
            def candidate_branch_for_node(self, *a, **k):
                return "agent/main"
            def plan_stage_dispatch(self, *a, **k):
                # Simulate causal wait: prerequisite deliverable not ready
                return {
                    "mode": "wait",
                    "reason": "waiting for prerequisite deliverables before redispatching next role",
                    "specs": [],
                }

        # Create a durable operation in DB
        ops = nds.reconcile_workflow(db_path, "wf-test", now=now)
        assert len(ops) >= 1
        op_id = ops[0]["id"]

        # Claim the operation (as coordinator admission does)
        owner_id = "test-owner-uuid"
        claimed_op = nds.claim(db_path, op_id, owner_id, now=now)
        assert claimed_op is not None
        assert claimed_op["status"] == "running"

        class Ports:
            STAGE_STATE_FILE = state_file
            lock = state_lock
            direct_dispatch_planner = MockPlanner()
            def direct_stage_dispatch_enabled(self):
                return True
            def project_for_workflow(self, wid):
                return {
                    "project_root": str(tmp_path),
                    "base_branch": "main",
                    "startup_ready": True,
                    "requirement": "build feature",
                }
            def workflow_config_for(self, wid):
                return wf_cfg
            def find_node(self, cfg, nid):
                return {"id": nid, "node_type": "agent"}
            def get_stage_policy(self, nid):
                return {}
            def load_tasks(self):
                return []
            def get_task(self, tid):
                return None
            def node_is_gate(self, wid, nid):
                return False
            def shared_docs_block(self, *a, **k):
                return ""
            def _scheduler_join_gate_allows(self, *a, **k):
                return True
            def _scheduler_expected_candidate_sha(self, *a, **k):
                return "abc1234"
            def _all_selective_facts(self, *a, **k):
                return []
            def _selective_redispatch_blocker_notes(self, *a, **k):
                return None
            selective_replan_core = None
            def blocked_verdict_dep(self, *a, **k):
                return None
            def _reverification_reused_node(self, *a, **k):
                return False
            def _get_store(self):
                return real_store
            # Latch ports
            def stage_advance_key(self, wid, stage):
                return stage_advance_key(self, wid, stage)
            def load_stage_state(self):
                with self.lock:
                    return json.loads(self.STAGE_STATE_FILE.read_text())
            def save_stage_state(self, s):
                with self.lock:
                    self.STAGE_STATE_FILE.write_text(json.dumps(s))
            def clear_stage_advance(self, wid, stage):
                clear_stage_advance(self, wid, stage)
            def mark_stage_advance_notified(self, wid, stage, **kwargs):
                # Old buggy behavior wrote notified
                with self.lock:
                    s = self.load_stage_state()
                    s[self.stage_advance_key(wid, stage)] = "notified"
                    self.save_stage_state(s)

        ports = Ports()

        # Simulate advancement marking the stage as 'queued'
        assert mark_stage_advance_queued(ports, "wf-test", "plan") is True
        assert ports.load_stage_state()["wf-test:plan"] == "queued"

        item = {
            "kind": "stage_advance",
            "workflow_id": "wf-test",
            "node_id": "plan",
            "node": {"id": "plan", "node_type": "agent"},
            "dispatch_operation_id": op_id,
            "dispatch_owner": owner_id,
        }

        # Execute direct stage advance
        result = try_direct_stage_advance(ports, item)
        assert result is True

        # RED Assertion: 'notified' MUST NOT be in stage-state.json!
        current_state = ports.load_stage_state()
        assert current_state.get("wf-test:plan") != "notified", (
            "Causal wait must not write 'notified' latch into stage-state.json!"
        )
        assert "wf-test:plan" not in current_state, (
            "Causal wait must clear the queued stage advance latch so subsequent sweeps can re-evaluate"
        )

        # Subsequent sweep can queue the stage again without being blocked
        assert mark_stage_advance_queued(ports, "wf-test", "plan") is True

        # And verify durable operation was deferred safely, not stranded in 'running'
        with nds.rs._transaction(db_path) as conn:
            op_after = nds.rs._get(conn, op_id)
            assert op_after["status"] == "pending", (
                f"Operation should be deferred back to pending on causal wait, got {op_after['status']}"
            )
            # Held defer does not count as failed attempt
            assert op_after["attempts"] == 1

    def test_causal_wait_transitions_to_dispatch_when_prerequisite_ready(self, tmp_path):
        from services.controller.direct_dispatch import try_direct_stage_advance
        from services.controller.stage_state import (
            mark_stage_advance_queued,
            load_stage_state,
            save_stage_state,
            clear_stage_advance,
            stage_advance_key,
        )

        db_path = tmp_path / "state2.db"
        init_db(db_path)
        wf_cfg = {
            "nodes": [{"id": "plan", "node_type": "agent"}],
        }
        save_workflow({"workflow_id": "wf-causal", "status": "running", "config": wf_cfg}, db_path=db_path)

        state_file = tmp_path / "stage-state2.json"
        state_file.write_text("{}")
        import threading
        state_lock = threading.RLock()
        real_store = get_state_store(db_path)

        class StatefulPlanner:
            def __init__(self):
                self.prerequisite_met = False
            def merge_node_policy(self, node, policy):
                return node
            def candidate_branch_for_node(self, *a, **k):
                return "agent/main"
            def plan_stage_dispatch(self, *a, **k):
                if not self.prerequisite_met:
                    return {
                        "mode": "wait",
                        "reason": "waiting for executor deliverables",
                        "specs": [],
                    }
                return {
                    "mode": "dispatch",
                    "specs": [{
                        "task_id": "wf-causal-plan-challenger",
                        "workflow_id": "wf-causal",
                        "role": "challenger",
                        "task_type": "coding",
                        "integration_mode": "none",
                        "goal": "challenge plan",
                        "prompt": "verify plan",
                        "acceptance": ["verify plan"],
                    }],
                }

        planner = StatefulPlanner()
        dispatched_cmds = []

        class Ports:
            STAGE_STATE_FILE = state_file
            lock = state_lock
            direct_dispatch_planner = planner
            DIRECT_DISPATCH_LAUNCH_TIMEOUT = 10
            TASK_MANAGER = "herdr-task"
            def direct_stage_dispatch_enabled(self):
                return True
            def project_for_workflow(self, wid):
                return {
                    "project_root": str(tmp_path),
                    "base_branch": "main",
                    "startup_ready": True,
                    "requirement": "build feature",
                }
            def workflow_config_for(self, wid):
                return wf_cfg
            def find_node(self, cfg, nid):
                return {"id": nid, "node_type": "agent"}
            def get_stage_policy(self, nid):
                return {}
            def load_tasks(self):
                return []
            def get_task(self, tid):
                return None
            def node_is_gate(self, wid, nid):
                return False
            def shared_docs_block(self, *a, **k):
                return ""
            def _scheduler_join_gate_allows(self, *a, **k):
                return True
            def _scheduler_expected_candidate_sha(self, *a, **k):
                return "abc1234"
            def _dispatch_candidate_ready(self, *a, **k):
                return True
            def _all_selective_facts(self, *a, **k):
                return []
            def _selective_redispatch_blocker_notes(self, *a, **k):
                return None
            selective_replan_core = None
            def blocked_verdict_dep(self, *a, **k):
                return None
            def _reverification_reused_node(self, *a, **k):
                return False
            def _get_store(self):
                return real_store
            def stage_advance_key(self, wid, stage):
                return stage_advance_key(self, wid, stage)
            def load_stage_state(self):
                with self.lock:
                    return json.loads(self.STAGE_STATE_FILE.read_text())
            def save_stage_state(self, s):
                with self.lock:
                    self.STAGE_STATE_FILE.write_text(json.dumps(s))
            def clear_stage_advance(self, wid, stage):
                clear_stage_advance(self, wid, stage)
            def mark_stage_advance_notified(self, wid, stage, **kwargs):
                with self.lock:
                    s = self.load_stage_state()
                    s[self.stage_advance_key(wid, stage)] = "notified"
                    self.save_stage_state(s)
            def maybe_compact_coordinator(self, *a, **k):
                pass
            def maybe_dispatch_node_handoffs(self, *a, **k):
                return []
            class subprocess:
                class SubprocessError(Exception):
                    pass
                class TimeoutExpired(Exception):
                    pass
                @staticmethod
                def run(cmd, **kwargs):
                    dispatched_cmds.append(cmd)
                    class Res:
                        returncode = 0
                        stdout = ""
                        stderr = ""
                    return Res()

        ports = Ports()
        item = {
            "kind": "stage_advance",
            "workflow_id": "wf-causal",
            "node_id": "plan",
            "node": {"id": "plan", "node_type": "agent"},
        }

        # Sweep 1: Prerequisite NOT met -> wait mode
        mark_stage_advance_queued(ports, "wf-causal", "plan")
        assert ports.load_stage_state().get("wf-causal:plan") == "queued"
        res1 = try_direct_stage_advance(ports, item)
        assert res1 is True
        # Latch is cleared, not 'notified'
        assert "wf-causal:plan" not in ports.load_stage_state()
        assert len(dispatched_cmds) == 0

        # Sweep 2: Prerequisite STILL not met -> wait mode again (idempotent, no stuck latch)
        assert mark_stage_advance_queued(ports, "wf-causal", "plan") is True
        res2 = try_direct_stage_advance(ports, item)
        assert res2 is True
        assert "wf-causal:plan" not in ports.load_stage_state()
        assert len(dispatched_cmds) == 0

        # Sweep 3: Prerequisite NOW met -> dispatch mode!
        planner.prerequisite_met = True
        assert mark_stage_advance_queued(ports, "wf-causal", "plan") is True
        res3 = try_direct_stage_advance(ports, item)
        assert res3 is True
        # Now task was launched and latch is 'notified'
        assert len(dispatched_cmds) == 1
        assert ports.load_stage_state().get("wf-causal:plan") == "notified"

        # Sweep 4: Latch is 'notified', queued advance is rejected (no duplicate dispatch)
        assert mark_stage_advance_queued(ports, "wf-causal", "plan") is False
        assert len(dispatched_cmds) == 1
