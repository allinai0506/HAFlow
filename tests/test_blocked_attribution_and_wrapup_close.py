"""Regression tests for P1: self-attributed verifier retry, wrapup awaiting publication, and close-workflow."""

import importlib
import subprocess
import sys
import time
from types import SimpleNamespace

from herdr import recovery_store as rs
from herdr.state_store import SQLiteStateStore
from herdr.workflow_progress import assess_workflow
from herdr.workflow_recovery import drive_recovery


def test_wrapup_blocked_for_pr_authorization_never_spawns_craft_fix_loop(tmp_path):
    """wrapup blocked without blaming implementation must not emit a fix_loop targeting craft."""
    sha = "a" * 40
    config = {
        "workflow_template": "code-craft-v1",
        "nodes": [
            {"id": "craft", "role": "worker"},
            {"id": "cleanroom_test", "gate": {"retry_node": "craft"}},
            {"id": "cleanroom_review", "gate": {"retry_node": "craft"}},
            {"id": "wrapup", "role": "wrapup"},
        ],
    }
    workflow = {
        "workflow_id": "wf-wrap",
        "execution_id": "exec-wrap",
        "status": "running",
        "startup_ready": True,
        "candidate_sha": sha,
    }
    tasks = [
        {
            "task_id": "wf-wrap-craft-auto-r2",
            "workflow_id": "wf-wrap",
            "node": "craft",
            "stage": "craft",
            "status": "cleaned",
            "run_id": "run-craft",
            "execution_id": "exec-wrap",
            "commit": sha,
            "integrated_commit": sha,
        },
        {
            "task_id": "wf-wrap-wrapup-auto",
            "workflow_id": "wf-wrap",
            "node": "wrapup",
            "stage": "wrapup",
            "status": "cleaned",
            "run_id": "run-wrap",
            "execution_id": "exec-wrap",
            "candidate_sha": sha,
            "verified_candidate_sha": sha,
            "stage_verdict": "blocked",
            "stage_verdict_note": "AC-4 blocked: read-only worker cannot push PR",
            "affected_task_ids": [],
        },
    ]
    res = assess_workflow(workflow, config, tasks)
    assert res["can_advance"] is False
    assert [op for op in res["obligations"] if op["kind"] == "fix_loop"] == []

    # Generic default note without publication-specific marker must still emit fix_loop
    tasks[1]["stage_verdict_note"] = "business acceptance recorded: blocked"
    res_generic = assess_workflow(workflow, config, tasks)
    assert len([op for op in res_generic["obligations"] if op["kind"] == "fix_loop"]) == 1


def test_verifier_self_blocked_reruns_same_verifier_node_on_frozen_sha(tmp_path, monkeypatch):
    """When a verifier gate is blocked with affected_task_ids pointing to itself (missing E2E/regression),
    fix_loop must target that verifier node on the same frozen candidate_sha instead of craft."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    config = {
        "workflow_template": "code-craft-v1",
        "nodes": [
            {"id": "craft", "role": "worker"},
            {"id": "cleanroom_test", "gate": {"retry_node": "craft"}},
        ],
    }
    store.save_workflow(
        {
            "workflow_id": "wf-self",
            "project_root": str(repo),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-self",
            "candidate_sha": sha,
            "candidate_episode_id": 1,
            "created_at": 1.0,
            "config": config,
        }
    )
    store.record_event(
        "candidate_frozen",
        {"candidate_sha": sha},
        workflow_id="wf-self",
        source="critical-path-scheduler",
        timestamp=2.0,
    )
    store.save_task(
        {
            "task_id": "wf-self-craft-auto",
            "workflow_id": "wf-self",
            "node": "craft",
            "stage": "craft",
            "status": "cleaned",
            "run_id": "run-craft",
            "execution_id": "exec-self",
            "commit": sha,
            "integrated_commit": sha,
        }
    )
    store.save_task(
        {
            "task_id": "wf-self-cleanroom_test-auto",
            "workflow_id": "wf-self",
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "status": "cleaned",
            "run_id": "run-test-1",
            "execution_id": "exec-self",
            "integration_mode": "none",
            "candidate_sha": sha,
            "verified_candidate_sha": sha,
            "dispatch_role": "worker",
            "dispatch_round": 1,
            "stage_verdict": "blocked",
            "stage_verdict_note": "Missing E2E and full backend suite evidence",
            "affected_task_ids": ["wf-self-cleanroom_test-auto"],
        }
    )

    ctl = importlib.import_module("services.herdr-controller")
    monkeypatch.setattr(ctl, "_get_store", lambda: store)
    monkeypatch.setattr(ctl, "load_tasks", store.list_tasks)
    monkeypatch.setattr(ctl, "clear_stage_advance", lambda *_a, **_k: None)

    launched = []
    real_run = subprocess.run

    def fake_run(cmd, **_kwargs):
        if "launch" not in cmd:
            return real_run(cmd, **_kwargs)
        launched.append(cmd)
        next_id = cmd[cmd.index("--task-id") + 1]
        cand_sha = cmd[cmd.index("--candidate-sha") + 1]
        store.save_task(
            {
                "task_id": next_id,
                "workflow_id": "wf-self",
                "node": "cleanroom_test",
                "stage": "cleanroom_test",
                "status": "working",
                "run_id": "run-test-2",
                "execution_id": "exec-self",
                "candidate_sha": cand_sha,
                "supersedes": "wf-self-cleanroom_test-auto",
                "completion_protocol": "receipt-v1",
                "completion_epoch": "ep-2",
                "completion_identity_path": str(tmp_path / "id-2.json"),
            }
        )
        old = store.get_task("wf-self-cleanroom_test-auto")
        store.save_task({**old, "status": "superseded", "superseded_by": next_id})
        store.record_event(
            "initial_dispatched",
            {
                "delivery_phase": "dispatched",
                "completion_epoch": "ep-2",
                "identity_path": str(tmp_path / "id-2.json"),
                "intervention_id": "intv-2",
            },
            task_id=next_id,
            run_id="run-test-2",
            workflow_id="wf-self",
            node_id="cleanroom_test",
            source="herdr-task",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ctl.subprocess, "run", fake_run)
    now = time.time()
    rs.reconcile(db_path, "wf-self", now=now)
    drive_recovery(store, "wf-self", lambda op, owner: ctl.execute_workflow_recovery(op, owner), now=now)

    ops = rs.list_operations(db_path, "wf-self")
    assert len(launched) == 1, [(o["status"], o["payload"].get("reason"), o["detail"]) for o in ops]
    assert "--node" in launched[0] and launched[0][launched[0].index("--node") + 1] == "cleanroom_test"
    r2 = store.get_task("wf-self-cleanroom_test-auto-r2")
    assert r2 is not None

    # Now r2 passes on the same frozen candidate SHA -> result_status must settle to 'resolved'
    from herdr.workflow_recovery import result_status
    store.save_task({**r2, "status": "cleaned", "stage_verdict": "pass", "verified_candidate_sha": sha})
    wf_now = store.get_workflow("wf-self")
    ops_now = rs.list_operations(db_path, "wf-self")
    fix_op = next(o for o in ops_now if o["payload"]["kind"] == "fix_loop")
    settled = result_status(fix_op, wf_now, store.list_tasks(workflow_id="wf-self"), acceptance={r2["task_id"]: True})
    assert settled is not None and settled[0] == "resolved"


def test_close_workflow_allows_blocked_wrapup_when_pr_created(tmp_path):
    """When PR publication is already recorded in events, close-workflow must not abort on
    wrapup's awaiting-PR blocked verdict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    store.save_workflow(
        {
            "workflow_id": "wf-close-pr",
            "project_root": str(repo),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-cp",
            "candidate_sha": sha,
            "candidate_episode_id": 1,
            "config": {"nodes": [{"id": "craft"}, {"id": "wrapup"}]},
        }
    )
    store.save_task(
        {
            "task_id": "wf-close-pr-craft",
            "workflow_id": "wf-close-pr",
            "node": "craft",
            "stage": "craft",
            "status": "cleaned",
            "run_id": "run-c",
            "execution_id": "exec-cp",
            "commit": sha,
            "integrated_commit": sha,
        }
    )
    store.save_task(
        {
            "task_id": "wf-close-pr-wrapup",
            "workflow_id": "wf-close-pr",
            "node": "wrapup",
            "stage": "wrapup",
            "status": "cleaned",
            "run_id": "run-w",
            "execution_id": "exec-cp",
            "candidate_sha": sha,
            "verified_candidate_sha": sha,
            "stage_verdict": "blocked",
            "stage_verdict_note": "AC-4 blocked: cannot push PR",
            "awaiting_publication": True,
        }
    )
    # Mismatched PR candidate SHA must NOT waive blocked wrapup
    store.record_event(
        "pull_request_created",
        {"candidate_sha": "0" * 40, "execution_id": "exec-cp", "number": 1926, "url": "https://gitee.com/org/repo/pulls/1926"},
        workflow_id="wf-close-pr",
        task_id="wf-close-pr-craft",
        source="herdr-task",
    )
    env = {
        **dict(__import__("os").environ),
        "HERDR_STATE_DB": str(db_path),
        "HERDR_TASKS_FILE": str(tmp_path / "tasks.json"),
        "HERDR_WORKFLOWS_FILE": str(tmp_path / "workflows.json"),
    }
    res_mismatch = subprocess.run(
        [sys.executable, "bin/herdr-task", "close-workflow", "wf-close-pr"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert res_mismatch.returncode == 2
    assert "[CLOSE ABORT] workflow has blocked gate verdicts" in (res_mismatch.stdout + res_mismatch.stderr)

    # Matching candidate SHA & execution_id waives the publication-only wrapup block
    store.record_event(
        "pull_request_created",
        {"candidate_sha": sha, "execution_id": "exec-cp", "number": 1927, "url": "https://gitee.com/org/repo/pulls/1927"},
        workflow_id="wf-close-pr",
        task_id="wf-close-pr-craft",
        source="herdr-task",
    )
    res = subprocess.run(
        [sys.executable, "bin/herdr-task", "close-workflow", "wf-close-pr"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert "[CLOSE ABORT] workflow has blocked gate verdicts" not in (res.stdout + res.stderr)
    assert res.returncode == 0, res.stdout + res.stderr
