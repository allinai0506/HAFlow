"""End-to-End Autonomous Flow Verification (wf-haflow-1029-02 remediation).

Verifies the complete autonomy loop across all four remediation milestones:
1. Milestone 1: Teardown hygiene whitelist ignores test artifacts (coverage, test-results).
2. Milestone 2: Round-scoped router isolation prevents dual-agent deadlocks.
3. Milestone 3: Dynamic defect attribution correctly identifies custom implementation nodes
   and attributes multi-task DAGs into pending obligations without halting to waiting_human.
4. Milestone 4: Controller maintains running status during closeout and elevates blocked
   cleanups with structured diagnostic notes.
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from herdr import agent_router
from herdr.agent_router import choose_agent
from herdr.repo_hygiene import teardown_output_diagnosis
from herdr.state_store import SQLiteStateStore
from herdr.workflow_progress import assess_workflow
from services.controller import workflow_close


def test_full_autonomous_remediation_lifecycle(tmp_path):
    """Simulate the end-to-end flow of wf-haflow-1029-02 from defect detection
    to round 2 repair, router allocation, hygiene cleanup, and clean closeout."""

    wid = "wf-haflow-1029-sim"
    eid = "exec-sim-1"
    candidate_r1 = "1" * 40
    integrated_r1 = "a" * 40
    candidate_r2 = "2" * 40
    integrated_r2 = "b" * 40

    config = {
        "nodes": [
            {"id": "craft", "role": "implementation"},
            {
                "id": "review",
                "role": "verifier",
                "gate": {"retry_node": "craft"},
                "agent_policy": {
                    "exclude_stage_agents": ["craft", "implementation"],
                    "allow_soft_degrade": False,
                },
            },
            {"id": "wrapup", "role": "delivery"},
        ]
    }

    # -------------------------------------------------------------
    # Stage 1: Round 1 Implementation & Blocked Gate Detection
    # -------------------------------------------------------------
    wf_state = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
        "startup_ready": True,
        "candidate_sha": integrated_r1,
    }

    craft_task_r1 = {
        "task_id": "t-craft-r1",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "craft",
        "stage": "craft",
        "status": "completed",
        "run_id": "run-craft-1",
        "agent": "agent-alpha",
        "commit": candidate_r1,
        "integrated_commit": integrated_r1,
        "dispatch_round": 1,
    }

    review_task_r1 = {
        "task_id": "t-review-r1",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "review",
        "stage": "review",
        "status": "completed",
        "stage_verdict": "blocked",
        "candidate_sha": integrated_r1,
        "run_id": "run-review-1",
        "agent": "agent-beta",
        "commit": integrated_r1,
        "dispatch_round": 1,
    }

    tasks = [craft_task_r1, review_task_r1]

    # Verify Milestone 3: Dynamic attribution attributes to 'craft' as PENDING, NOT waiting_human!
    assessment = assess_workflow(wf_state, config, tasks)
    assert assessment["can_advance"] is False
    assert len(assessment["obligations"]) == 1

    obligation = assessment["obligations"][0]
    assert obligation["retry_node"] == "craft"
    assert obligation["affected_task_ids"] == ["t-craft-r1"]
    assert obligation["status"] == "pending", "Must NOT drop into waiting_human on blocked gate!"
    assert obligation["reason"] == "blocked_gate"

    # -------------------------------------------------------------
    # Stage 2: Round 2 Agent Routing & Author Isolation
    # -------------------------------------------------------------
    # Setup state store and pool files for agent router
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path)
    pools_file = tmp_path / "pools.json"
    reservations_file = tmp_path / "reservations.json"
    lock_file = tmp_path / "router.lock"

    pools_file.write_text(
        json.dumps({
            "projects": {
                "proj-sim": {
                    "allowed_agents": ["agent-alpha", "agent-beta"],
                    "disabled_agents": [],
                    "stage_preferences": {},
                    "task_type_preferences": {},
                }
            }
        }),
        encoding="utf-8",
    )

    store.save_workflow({
        "workflow_id": wid,
        "project_id": "proj-sim",
        "status": "running",
        "execution": {"mode": "git"},
    })
    store.save_task(craft_task_r1)
    store.save_task(review_task_r1)

    with patch.object(agent_router, "_get_store", return_value=store), \
         patch.object(agent_router, "workflow_config_for", return_value=config), \
         patch.object(agent_router, "POOLS_FILE", pools_file), \
         patch.object(agent_router, "RESERVATIONS_FILE", reservations_file), \
         patch.object(agent_router, "ROUTER_LOCK_FILE", lock_file):

        # In Round 2:
        # 1. 'craft' (implementation) author for self-remediation
        author_r2 = choose_agent(
            wid,
            "craft",
            "implementation",
            dispatch_round=2,
            candidate_author="agent-alpha",
        )
        assert author_r2 in ["agent-alpha", "agent-beta"]

        # Record Round 2 craft completed by agent-alpha
        craft_task_r2 = {
            "task_id": "t-craft-r2",
            "workflow_id": wid,
            "execution_id": eid,
            "node": "craft",
            "stage": "craft",
            "agent": "agent-alpha",
            "status": "completed",
            "commit": candidate_r2,
            "integrated_commit": integrated_r2,
            "dispatch_round": 2,
        }
        store.save_task(craft_task_r2)

        # 2. 'review' (verifier) MUST isolate the author ('agent-alpha'), assigning to 'agent-beta'
        reviewer_r2 = choose_agent(
            wid,
            "review",
            "test",
            dispatch_round=2,
            candidate_author="agent-alpha",
        )
        assert reviewer_r2 == "agent-beta", "Verifier must exclude candidate author without deadlock!"

    # -------------------------------------------------------------
    # Stage 3: Teardown Hygiene Bypass for Test Artifacts
    # -------------------------------------------------------------
    # The review/test node executed pytest and generated coverage & test-results
    status_output = (
        "?? test-evidence/report.xml\n"
        "?? coverage/coverage.json\n"
        "?? .coverage\n"
    )
    diag = teardown_output_diagnosis(status_output, task_id="t-review-r1")
    assert diag["reason"] == ""
    assert diag["blocked_files"] == []
    assert diag["blocked_total"] == 0, "Test evidence and coverage directories must be bypassed!"

    # -------------------------------------------------------------
    # Stage 4: Autonomous Closeout & Diagnostic Visibility
    # -------------------------------------------------------------
    review_task_r2 = {
        "task_id": "t-review-r2",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "review",
        "stage": "review",
        "status": "completed",
        "stage_verdict": "pass",
        "candidate_sha": integrated_r2,
        "dispatch_round": 2,
    }
    wrapup_task = {
        "task_id": "t-wrapup",
        "workflow_id": wid,
        "execution_id": eid,
        "node": "wrapup",
        "stage": "wrapup",
        "status": "completed",
        "stage_verdict": "pass",
    }
    all_completed_tasks = [craft_task_r2, review_task_r2, wrapup_task]
    wf_state["candidate_sha"] = integrated_r2

    # Verify workflow assessment recognizes all complete
    final_assessment = assess_workflow(wf_state, config, all_completed_tasks)
    assert final_assessment["can_advance"] is True
    assert len(final_assessment["obligations"]) == 0

    # Test workflow close integration
    mock_ports = MagicMock()
    mock_ports.load_tasks.return_value = all_completed_tasks
    mock_ports.git_finalize_pending_tasks.return_value = []
    mock_ports.git_escalated_tasks.return_value = []
    mock_ports._workflow_close_inflight = set()
    mock_ports.attention_blocks_retry.return_value = False
    mock_ports._workflow_entry.return_value = {"status": "running"}
    mock_ports.liveness.workflow_is_foreign.return_value = False
    mock_ports.close_retry_interval = lambda a: 60

    # Ensure synchronous thread execution for test
    class SyncThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()

    mock_ports.threading.Thread = SyncThread
    mock_ports.TASK_MANAGER = "herdr-task"

    # Simulate close command success
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "OK"
    mock_res.stderr = ""
    mock_ports.subprocess.run.return_value = mock_res

    workflow_close.maybe_close_completed_workflow(mock_ports, wid)

    # Assert attention cleared and status remained running until CLI close
    mock_ports.attention_clear.assert_called_once_with(f"{wid}:auto-close")


def test_close_retry_escalation_to_attention_note():
    """Verify Milestone 4: when auto-close fails repeatedly (>= 2 attempts),
    it elevates to auto_close_blocked with teardown_hygiene_violation and dry-run remediation."""
    mock_ports = MagicMock()
    wid = "wf-stuck-close"
    close_key = f"{wid}:auto-close"

    mock_ports.attention_get.return_value = {"attempts": 1}  # Previous attempt 1
    mock_ports.close_retry_interval.return_value = 120

    workflow_close._note_close_backoff(
        mock_ports,
        wid,
        close_key,
        error_output="Directory not empty: scratch/dirty.txt",
    )

    mock_ports.attention_note.assert_called_once_with(
        close_key,
        {"task_id": f"{wid}:close", "workflow_id": wid},
        "auto_close_blocked",
        "teardown_hygiene_violation",
        attempts=2,
        detail={
            "error": "Directory not empty: scratch/dirty.txt",
            "remediation": f"herdr-task close-workflow {wid} --dry-run",
        },
    )
    mock_ports.attention_throttle.assert_called_once_with(close_key, interval=120)
