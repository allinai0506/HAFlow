from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
import pytest

from herdr import scheduler_facts, workflow_docs as wd
from herdr.delivery_record import select_effective_delivery, _body_value
from herdr.workflow import is_workflow_completed
from herdr.workflow_progress import assess_workflow
from tests.test_wrapup_candidate_delivery import (
    ROOT,
    scene,
    controller,
    agents,
    forge,
    add_output,
    dispatch_verifiers,
    certify,
    delivery,
    accept_join,
    publish,
    closure,
    _workspace_delivery,
    report_wrapup,
    cli,
    git,
)


def _report_wrapup(scene, *, now=None):
    from herdr.completion_receipt import consume_completion_receipt

    task = scene.store.get_task("wrap")
    report = scene.root / "final-wrapup.txt"
    report.write_text(
        "Final runtime report for " + git(scene.clone, "rev-parse", "HEAD")
    )
    pub = cli(
        scene,
        "checkpoint-publish",
        "--task-id",
        "wrap",
        "--run-id",
        task["run_id"],
        "--epoch",
        task["completion_epoch"],
        "--segment-file",
        str(report),
        "--step",
        "1",
    )
    receipt = json.loads(pub.stdout)
    cli(
        scene,
        "report-completion",
        "wrap",
        "--identity-file",
        task["completion_identity_path"],
        "--artifact",
        receipt["observation_id"] + ":" + receipt["sha256"],
    )
    consumed = consume_completion_receipt(
        "wrap", scene.store, now=now or (time.time() + 120)
    )
    assert consumed["accepted"], consumed


def _install_transport_mock(controller, scene):
    real_transport = controller.subprocess.run

    def mock_run(command, **kwargs):
        try:
            if len(command) >= 3 and command[1] == "rework":
                tid = command[2]

                def arg(key, default=None):
                    return command[command.index(key) + 1] if key in command else default

                req_id = arg("--request-id")
                reason = arg("--reason")
                prompt = arg("--prompt")
                task = scene.store.get_task(tid)
                from herdr.completion_receipt import issue_completion_contract
                from herdr import kernel

                kernel.transition_task(
                    tid,
                    "rework",
                    reason=reason or "recovery",
                    source="herdr-task",
                    store=scene.store,
                    metadata={"request_id": req_id},
                )
                issue_completion_contract(tid, scene.store)
                task = scene.store.get_task(tid)
                scene.store.update_task_metadata(
                    tid,
                    {
                        "rework_delivery": "delivered",
                        "rework_request_id": req_id,
                        "rework_prompt": prompt,
                        "stage_verdict": None,
                        "stage_verdict_note": None,
                    },
                )
                scene.store.record_event(
                    "rework_dispatched",
                    {
                        "delivery_phase": "dispatched",
                        "intervention_id": req_id,
                        "action": "REWORK",
                        "completion_epoch": task.get("completion_epoch"),
                        "identity_path": task.get("completion_identity_path"),
                    },
                    workflow_id=task.get("workflow_id"),
                    task_id=tid,
                    node_id=task.get("node") or task.get("stage"),
                    run_id=task.get("run_id"),
                    source="herdr-task",
                )
                kernel.transition_task(
                    tid,
                    "working",
                    reason="agent_working",
                    source="agent",
                    store=scene.store,
                )
                import subprocess
                return subprocess.CompletedProcess(command, 0, f"[REWORK] {tid} delivered", "")
            return real_transport(command, **kwargs)
        except Exception:
            import traceback
            traceback.print_exc()
            raise

    controller.subprocess.run = mock_run


def test_full_wrapup_failure_recovery_closed_loop(scene, controller, agents, forge):
    """
    E2E failure recovery closed loop for both software-development-v1 and spec-development-v1:
    1. implementation produces certified X (in fixture scene).
    2. wrapup produces new candidate Y.
    3. review blocks candidate Y.
    4. controller automatic recovery identifies wrapup as retry target, delivers rework.
    5. verifiers are invalidated while wrapup is preserved.
    6. wrapup produces repair candidate Z under verified recovery authorization.
    7. controller freezes Z and dispatches fresh verifiers.
    8. verifiers pass on Z; recovery operation resolves.
    9. delivery note recorded for Z (superseding X).
    10. PR published for Z, review closure recorded on Z head.
    11. wrapup and workflow complete.
    """
    # 1. wrapup produces candidate Y
    y, s_y = add_output(scene, "Knowledge Y containing defect")

    # 2. verifiers dispatched for Y
    first_tasks = dispatch_verifiers(scene, controller, agents)
    for tid in first_tasks:
        node = scene.store.get_task(tid)["node"]
        if node == "review":
            certify(scene, tid, verdict="blocked")
        else:
            certify(scene, tid, verdict="pass")

    # intercept rework in transport to simulate worker receiving prompt
    _install_transport_mock(controller, scene)

    # 3. controller automated recovery
    ops = controller.check_workflow_recovery("wf")
    assert ops, "Recovery operations should not be empty"
    op = next(o for o in ops if (o.get("payload") or {}).get("kind") == "fix_loop")
    assert op["status"] == "awaiting_result"
    assert op["payload"]["retry_node"] == "wrapup"
    assert op["payload"]["affected_task_ids"] == ["wrap"]
    assert "wrap" in op["detail"].get("rework_ids", [])

    # Verify wrap task is preserved and has confirmed rework delivery
    wrap = scene.store.get_task("wrap")
    assert wrap["status"] in ("working", "agent_done")
    assert wrap.get("rework_delivery") == "delivered"
    rework_prompt = wrap.get("rework_prompt") or ""
    assert "old-review-r2: Independent fixture evidence checked" in rework_prompt

    # Verify blocked gate task is superseded
    assert scene.store.get_task("old-review-r2")["status"] == "superseded"

    # 4. wrapup produces repair candidate Z
    z, s_z = add_output(scene, "Knowledge Z fixing the defect")
    assert s_z != s_y

    # 5. controller freezes candidate Z and dispatches fresh verifiers
    second_tasks = dispatch_verifiers(scene, controller, agents)
    assert set(second_tasks).isdisjoint(first_tasks)
    for tid in first_tasks:
        assert scene.store.get_task(tid)["status"] == "superseded"

    # 6. verifiers certify candidate Z
    for tid in second_tasks:
        certify(scene, tid, verdict="pass")
    accept_join(scene)

    # 7. settlement of recovery operation
    ops = controller.check_workflow_recovery("wf", now=time.time() + 60)
    op = next(o for o in ops if o["id"] == op["id"])
    assert op["status"] == "resolved"
    assert op["detail"].get("reason") == "replacement_reverified"

    # 8. delivery note recorded for Z
    assert delivery(scene, second_tasks) == s_z

    # 9. PR published for Z
    pr = publish(scene, "wrap")
    assert pr["candidate_sha"] == s_z
    assert git(scene.remote, "rev-parse", pr["branch"]) == s_z

    # 10. review closure on Z head
    (scene.fakebin / "head.txt").write_text(s_z)
    closure(scene, pr, s_z)

    # 11. wrapup completes
    _report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")
    assert scene.store.get_task("wrap")["status"] == "completed"

    # 12. workflow completes and close-workflow records receipt
    cli(scene, "close-workflow", "wf")
    assert scene.store.get_workflow("wf")["status"] == "completed"

    import sqlite3
    conn = sqlite3.connect(scene.store.db_path)
    close_row = conn.execute(
        "SELECT state, receipt FROM workflow_close_operations WHERE workflow_id=?",
        ("wf",),
    ).fetchone()
    assert close_row is not None
    assert close_row[0] == "completed"
    receipt = json.loads(close_row[1])
    assert receipt.get("workflow_id") == "wf"

    effective = select_effective_delivery(wd.annotate_notes(wd.load_notes("wf")))
    assert _body_value(effective, "candidate_sha") == s_z


def test_auto_attribution_matches_explicit_affected_task(scene, controller, agents):
    """Auto-attribution on failed wrapup candidate matches explicit affected_task_ids=['wrap']."""
    y, s_y = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    wf = scene.store.get_workflow("wf")
    current_tasks = scene.store.list_tasks(workflow_id="wf")

    # Assess without explicit affected_task_ids (auto attribution)
    auto_assessment = assess_workflow(wf, scene.cfg, current_tasks)
    auto_obligation = next(
        o for o in auto_assessment["obligations"] if o.get("kind") == "fix_loop"
    )
    assert auto_obligation["status"] == "pending"
    assert auto_obligation["retry_node"] == "wrapup"
    assert auto_obligation["affected_task_ids"] == ["wrap"]

    # Now simulate verifier explicitly outputting affected_task_ids=['wrap']
    review_task = next(
        t for t in current_tasks if t.get("node") == "review" and t.get("stage_verdict") == "blocked"
    )
    scene.store.update_task_metadata(
        review_task["task_id"], {"stage_verdict_affected_task_ids": ["wrap"]}
    )
    current_tasks = scene.store.list_tasks(workflow_id="wf")

    explicit_assessment = assess_workflow(wf, scene.cfg, current_tasks)
    explicit_obligation = next(
        o for o in explicit_assessment["obligations"] if o.get("kind") == "fix_loop"
    )
    assert explicit_obligation["status"] == "pending"
    assert explicit_obligation["retry_node"] == "wrapup"
    assert explicit_obligation["affected_task_ids"] == ["wrap"]


def test_implementation_candidate_rework_preserved(scene, controller, agents):
    """Ordinary implementation candidate rework loop is not disrupted."""
    # Reset workflow candidate back to implementation X and block review on X
    wf = scene.store.get_workflow("wf")
    scene.store.save_workflow({**wf, "candidate_sha": scene.x, "candidate_episode_id": 1})
    scene.store.save_task(
        {
            "task_id": "test-impl-block",
            "workflow_id": "wf",
            "execution_id": "exec",
            "run_id": "r-test-ib",
            "node": "review",
            "status": "completed",
            "stage_verdict": "blocked",
            "stage_verdict_note": "implementation defect",
            "candidate_sha": scene.x,
            "candidate_episode_id": 1,
        }
    )
    current_tasks = scene.store.list_tasks(workflow_id="wf")
    assessment = assess_workflow(scene.store.get_workflow("wf"), scene.cfg, current_tasks)
    obligation = next(
        o for o in assessment["obligations"] if o.get("kind") == "fix_loop"
    )
    assert obligation["retry_node"] == "implementation"
    assert obligation["affected_task_ids"] == ["impl"]


def test_illegal_or_mismatched_affected_task_rejected(scene, controller, agents):
    """Specifying wrong affected task for candidate Y is rejected."""
    y, s_y = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    # Verifier attributes failure to 'impl' (candidate mismatch)
    current_tasks = scene.store.list_tasks(workflow_id="wf")
    review_task = next(
        t for t in current_tasks if t.get("node") == "review" and t.get("stage_verdict") == "blocked"
    )
    scene.store.update_task_metadata(
        review_task["task_id"], {"stage_verdict_affected_task_ids": ["impl"]}
    )
    current_tasks = scene.store.list_tasks(workflow_id="wf")

    wf = scene.store.get_workflow("wf")
    assessment = assess_workflow(wf, scene.cfg, current_tasks)
    obligation = next(
        o for o in assessment["obligations"] if o.get("kind") == "fix_loop"
    )
    assert obligation["status"] == "waiting_human"
    assert obligation["reason"] == "candidate_mismatch"


def test_unauthorized_repair_submission_rejected(scene, controller, agents):
    """Wrapup repair submission without legitimate recovery authorization is rejected."""
    y, s_y = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    # Without controller recovery authorization, wrapup commit is rejected
    (scene.clone / "knowledge.md").write_text("Unauthorized attempt to bypass recovery\n")
    res = cli(scene, "commit", "wrap", ok=False)
    assert res.returncode != 0
    err = (res.stderr + res.stdout).lower()
    assert "certified" in err or "recovery" in err or "effective" in err


def test_old_candidate_evidence_cannot_pass_new_candidate(scene, controller, agents, forge):
    """Old verification evidence from X or Y cannot verify candidate Z."""
    y, s_y = add_output(scene)
    first_tasks = dispatch_verifiers(scene, controller, agents)
    for tid in first_tasks:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    _install_transport_mock(controller, scene)
    controller.check_workflow_recovery("wf")

    z, s_z = add_output(scene, "Knowledge Z fix")
    # Verify that attempting to publish without passing gates on Z raises ValueError
    with pytest.raises(ValueError, match="gate|bind"):
        publish(scene, "wrap")

    # Verify that review closure using old candidate SHA fails
    pr = {"url": "https://github.com/owner/repo/pull/1"}
    (scene.fakebin / "head.txt").write_text(s_z)
    closure_res = closure(scene, pr, s_y, ok=False)
    assert closure_res.returncode != 0

    # A workspace ceiling does not require the PR receipt. Other templates still refuse.
    _report_wrapup(scene)
    set_res = cli(scene, "set", "wrap", "completed", ok=False)
    if _workspace_delivery(scene):
        assert set_res.returncode == 0
    else:
        assert set_res.returncode != 0


def test_budget_exhaustion_stops_with_waiting_human(scene, controller, agents):
    """Exhausting retry / rotation budget transitions to waiting_human."""
    y, s_y = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    # Artificially set wrapup delivery round to 3 (max cap)
    wrap = scene.store.get_task("wrap")
    delivery_meta = dict(wrap.get("wrapup_delivery") or {})
    delivery_meta["round"] = 3
    scene.store.update_task_metadata("wrap", {"wrapup_delivery": delivery_meta})

    _install_transport_mock(controller, scene)
    controller.check_workflow_recovery("wf")

    # Next attempt to produce new candidate reaches round 4 > 3, which must fail
    (scene.clone / "knowledge.md").write_text("Knowledge beyond budget\n")
    res = cli(scene, "commit", "wrap", ok=False)
    assert res.returncode != 0
    err = (res.stderr + res.stdout).lower()
    assert "budget" in err or "exhausted" in err or "round" in err or "exceeds" in err


def test_idempotent_sweeps_do_not_duplicate_reworks(scene, controller, agents):
    """Repeated controller sweeps do not duplicate reworks or reset budgets."""
    y, s_y = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    _install_transport_mock(controller, scene)

    # First recovery sweep
    ops1 = controller.check_workflow_recovery("wf")
    op1 = next(o for o in ops1 if (o.get("payload") or {}).get("kind") == "fix_loop")
    req_id1 = op1["detail"]["rework_requests"]["wrap"]

    # Second recovery sweep without state changes
    ops2 = controller.check_workflow_recovery("wf")
    op2 = next(o for o in ops2 if (o.get("payload") or {}).get("kind") == "fix_loop")
    req_id2 = op2["detail"]["rework_requests"]["wrap"]

    # Same operation, same rework request, no duplicate dispatch
    assert op1["id"] == op2["id"]
    assert req_id1 == req_id2
    events = scene.store.list_events(task_id="wrap", event_type="rework_dispatched")
    assert len(events) == 1


def test_gate_dispatch_inventory_lists_wrapup_not_impl(scene, controller, agents):
    """
    P1 verification: Selective gate inventory block via real controller port proxy
    identifies wrapup as retry_node and lists task_id: wrap, not the predecessor implementation.
    """
    y, s_y = add_output(scene, "Knowledge Y")
    # Dispatched verifiers freeze candidate Y and receive this inventory block
    tasks = dispatch_verifiers(scene, controller, agents)
    assert len(tasks) == 2

    # Verify controller dependency wiring on ports
    inventory_block = controller._selective_gate_inventory_block("wf", scene.cfg)
    assert inventory_block is not None, "Gate inventory block should be generated"
    assert "task_id: wrap" in inventory_block
    assert "task_id: impl" not in inventory_block


def test_consecutive_failures_round3_recovery(scene, controller, agents, forge):
    """
    P1 verification: Multi-round failure recovery where candidate Y fails (round 1),
    repair Z fails again (round 2), and repair W succeeds (round 3) within the budget <= 3.
    Verifies ancestral lineage to certified commit X is preserved across rounds.
    """
    # Round 1: wrapup produces candidate Y
    y, s_y = add_output(scene, "Candidate Y (round 1 defect)")
    tasks1 = dispatch_verifiers(scene, controller, agents)
    for tid in tasks1:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    _install_transport_mock(controller, scene)

    # Controller recovery round 1 -> delivers rework to wrap
    ops1 = controller.check_workflow_recovery("wf")
    op1 = next(o for o in ops1 if (o.get("payload") or {}).get("kind") == "fix_loop")
    assert op1["status"] == "awaiting_result"
    assert op1["payload"]["retry_node"] == "wrapup"

    # Round 2: wrapup produces repair candidate Z (still flawed)
    z, s_z = add_output(scene, "Candidate Z (round 2 defect)")
    tasks2 = dispatch_verifiers(scene, controller, agents)
    assert set(tasks2).isdisjoint(tasks1)
    for tid in tasks2:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    # Controller recovery round 2 -> delivers second rework to wrap
    ops2 = controller.check_workflow_recovery("wf")
    op2 = next(
        o
        for o in ops2
        if (o.get("payload") or {}).get("candidate_sha") == s_z
        and (o.get("payload") or {}).get("kind") == "fix_loop"
    )
    assert op2["status"] == "awaiting_result"
    assert op2["payload"]["retry_node"] == "wrapup"
    assert op2["payload"]["candidate_sha"] == s_z

    # Round 3: wrapup produces repair candidate W (clean fix)
    w, s_w = add_output(scene, "Candidate W (round 3 clean fix)")
    tasks3 = dispatch_verifiers(scene, controller, agents)
    assert set(tasks3).isdisjoint(tasks2)
    for tid in tasks3:
        certify(scene, tid, verdict="pass")
    accept_join(scene)

    # Recovery settles
    ops3 = controller.check_workflow_recovery("wf", now=time.time() + 60)
    op3 = next(o for o in ops3 if o["id"] == op2["id"])
    assert op3["status"] == "resolved"
    assert op3["detail"].get("reason") == "replacement_reverified"

    # Delivery, PR, review closure and close-workflow on candidate W
    assert delivery(scene, tasks3) == s_w
    pr = publish(scene, "wrap")
    assert pr["candidate_sha"] == s_w
    (scene.fakebin / "head.txt").write_text(s_w)
    closure(scene, pr, s_w)

    _report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")
    assert scene.store.get_task("wrap")["status"] == "completed"

    cli(scene, "close-workflow", "wf")
    assert scene.store.get_workflow("wf")["status"] == "completed"


def test_unconfirmed_rework_blocks_repair_submission(scene, controller, agents):
    """
    P2 verification: An unconfirmed rework (e.g. pending delivery or missing confirmed receipt)
    blocks candidate submission, preventing premature admission before rework has been delivered.
    """
    y, s_y = add_output(scene, "Candidate Y")
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        node = scene.store.get_task(tid)["node"]
        certify(scene, tid, verdict="blocked" if node == "review" else "pass")

    _install_transport_mock(controller, scene)
    ops = controller.check_workflow_recovery("wf")
    op = next(o for o in ops if (o.get("payload") or {}).get("kind") == "fix_loop")
    assert op["status"] == "awaiting_result"

    # Temporarily set rework_delivery to pending (unconfirmed)
    scene.store.update_task_metadata("wrap", {"rework_delivery": "pending"})

    # Attempt to commit repair candidate before delivery is confirmed
    (scene.clone / "knowledge.md").write_text("Premature repair attempt\n")
    res = cli(scene, "commit", "wrap", ok=False)
    assert res.returncode != 0
    err = (res.stderr + res.stdout).lower()
    assert "certified" in err or "recovery" in err or "effective" in err

    # Now restore confirmed delivery
    scene.store.update_task_metadata("wrap", {"rework_delivery": "delivered"})

    # Now commit repair candidate succeeds
    res_ok = cli(scene, "commit", "wrap")
    assert res_ok.returncode == 0
