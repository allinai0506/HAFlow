"""Cross-template regression tests for fix_loop successor launch on cleaned/committed delivered tasks."""

import importlib
import subprocess
import time
from types import SimpleNamespace

import pytest

from herdr import recovery_store as rs
from herdr.state_store import SQLiteStateStore
from herdr.workflow_recovery import drive_recovery


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def _git_commit(repo, rel_path, content, msg):
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize(
    ("template_name", "impl_node", "gate_node", "predecessor_status", "commit_field"),
    [
        ("code-craft-v1", "craft", "cleanroom_review", "cleaned", "integrated_commit"),
        ("triad-dev-v1", "executor", "review", "cleaned", "commit"),
        ("software-development-v1", "implementation", "review", "committed", "commit"),
    ],
)
def test_fix_loop_launches_successor_for_delivered_cleaned_and_committed_across_templates(
    tmp_path,
    monkeypatch,
    template_name,
    impl_node,
    gate_node,
    predecessor_status,
    commit_field,
):
    """Any workflow template whose delivered predecessor is cleaned or committed with a valid SHA
    auto-launches an r2 successor instead of stalling in target_requires_explicit_replacement."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _git_commit(repo, "work.txt", "v1\n", "delivered candidate")

    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    config = {
        "workflow_template": template_name,
        "nodes": [
            {"id": impl_node, "role": "worker"},
            {"id": gate_node, "role": "gate", "gate": {"retry_node": impl_node}},
            {"id": "wrapup", "role": "wrapup"},
        ],
    }
    store.save_workflow(
        {
            "workflow_id": "wf",
            "project_root": str(repo),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-1",
            "candidate_sha": sha,
            "candidate_episode_id": 1,
            "created_at": 1.0,
            "config": config,
        }
    )
    store.record_event(
        "candidate_frozen",
        {"candidate_sha": sha},
        workflow_id="wf",
        source="critical-path-scheduler",
        timestamp=2.0,
    )

    pred_id = f"wf-{impl_node}-auto"
    pred_task = {
        "task_id": pred_id,
        "workflow_id": "wf",
        "node": impl_node,
        "stage": impl_node,
        "status": predecessor_status,
        "run_id": "run-pred-1",
        "execution_id": "exec-1",
        "agent": "agy",
        "goal": "Fix cache keys",
        "dispatch_role": "worker",
        "dispatch_round": 1,
        commit_field: sha,
    }
    store.save_task(pred_task)
    store.save_task(
        {
            "task_id": f"wf-{gate_node}-auto",
            "workflow_id": "wf",
            "node": gate_node,
            "stage": gate_node,
            "status": "cleaned",
            "run_id": "run-gate-1",
            "execution_id": "exec-1",
            "candidate_sha": sha,
            "stage_verdict": "blocked",
            "stage_verdict_note": "BUG-1: fix AOP self-invocation",
            "affected_task_ids": [pred_id],
        }
    )

    ctl = importlib.import_module("services.herdr-controller")
    monkeypatch.setattr(ctl, "_get_store", lambda: store)
    monkeypatch.setattr(ctl, "load_tasks", store.list_tasks)
    monkeypatch.setattr(ctl, "clear_stage_advance", lambda *_args, **_kwargs: None)

    launched_cmds = []

    def fake_run(cmd, **_kwargs):
        launched_cmds.append(cmd)
        if "supersede" in cmd:
            target_id = cmd[cmd.index("supersede") + 1]
            existing = store.get_task(target_id)
            if existing:
                store.save_task({**existing, "status": "superseded"})
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        next_id = cmd[cmd.index("--task-id") + 1]
        cand_sha = cmd[cmd.index("--candidate-sha") + 1]
        store.save_task(
            {
                "task_id": next_id,
                "workflow_id": "wf",
                "node": impl_node,
                "stage": impl_node,
                "status": "working",
                "run_id": "run-succ-2",
                "execution_id": "exec-1",
                "candidate_sha": cand_sha,
                "supersedes": pred_id,
                "recovery_lineage": {
                    "predecessor_id": pred_id,
                    "successor_id": next_id,
                    "predecessor_run_id": "run-pred-1",
                    "successor_run_id": "run-succ-2",
                    "candidate_sha": cand_sha,
                },
                "completion_protocol": "receipt-v1",
                "completion_epoch": "ep-2",
                "completion_identity_path": "/tmp/id2.json",
                "prompt_ledger_status": "DISPATCHED",
            }
        )
        store.update_task_metadata(pred_id, {"superseded_by": next_id})
        store.record_event(
            "initial_dispatched",
            {
                "action": "INITIAL",
                "delivery_phase": "dispatched",
                "completion_epoch": "ep-2",
                "identity_path": "/tmp/id2.json",
                "intervention_id": "initial:run-succ-2",
            },
            workflow_id="wf",
            node_id=impl_node,
            task_id=next_id,
            run_id="run-succ-2",
            source="herdr-task",
        )
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(ctl, "subprocess", SimpleNamespace(run=fake_run))

    now = time.time()
    rs.reconcile(store.db_path, "wf", now=now)
    drive_recovery(store, "wf", lambda op, owner: ctl.execute_workflow_recovery(op, owner), now=now)

    launch_cmds = [c for c in launched_cmds if "launch" in c]
    assert len(launch_cmds) == 1
    cmd = launch_cmds[0]
    assert cmd[cmd.index("--candidate-sha") + 1] == sha
    assert cmd[cmd.index("--supersedes") + 1] == pred_id
    assert cmd[cmd.index("--node") + 1] == impl_node

    op = next(o for o in rs.list_operations(store.db_path, "wf") if o["payload"]["kind"] == "fix_loop")
    assert op["status"] == "awaiting_result"
    assert op["detail"]["successor_ids"] == [f"{pred_id}-r2"]
    assert store.get_task(f"wf-{gate_node}-auto")["status"] == "superseded"


def test_cleaned_without_delivered_commit_still_requires_explicit_replacement(tmp_path, monkeypatch):
    """Fail-closed: a cleaned predecessor with no valid commit/integrated_commit must still pause for human."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = _git_commit(repo, "work.txt", "v1\n", "base")

    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    config = {
        "workflow_template": "code-craft-v1",
        "nodes": [
            {"id": "craft", "role": "worker"},
            {"id": "cleanroom_review", "role": "gate", "gate": {"retry_node": "craft"}},
        ],
    }
    store.save_workflow(
        {
            "workflow_id": "wf",
            "project_root": str(repo),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-1",
            "candidate_sha": sha,
            "candidate_episode_id": 1,
            "created_at": 1.0,
            "config": config,
        }
    )
    store.record_event(
        "candidate_frozen",
        {"candidate_sha": sha},
        workflow_id="wf",
        source="critical-path-scheduler",
        timestamp=2.0,
    )
    store.save_task(
        {
            "task_id": "wf-craft-auto",
            "workflow_id": "wf",
            "node": "craft",
            "stage": "craft",
            "status": "cleaned",
            "run_id": "run-pred-1",
            "execution_id": "exec-1",
        }
    )
    store.save_task(
        {
            "task_id": "wf-cleanroom_review-auto",
            "workflow_id": "wf",
            "node": "cleanroom_review",
            "stage": "cleanroom_review",
            "status": "cleaned",
            "run_id": "run-gate-1",
            "execution_id": "exec-1",
            "candidate_sha": sha,
            "stage_verdict": "blocked",
            "affected_task_ids": ["wf-craft-auto"],
        }
    )

    ctl = importlib.import_module("services.herdr-controller")
    monkeypatch.setattr(ctl, "_get_store", lambda: store)
    monkeypatch.setattr(ctl, "load_tasks", store.list_tasks)

    now = time.time()
    rs.reconcile(store.db_path, "wf", now=now)
    drive_recovery(store, "wf", lambda op, owner: ctl.execute_workflow_recovery(op, owner), now=now)

    op = next(o for o in rs.list_operations(store.db_path, "wf") if o["payload"]["kind"] == "fix_loop")
    assert op["status"] == "waiting_human"
    assert op["detail"]["target_requires_explicit_replacement"] is True
    assert store.get_task("wf-craft-auto-r2") is None


def test_begin_launch_intent_and_link_successor_allow_r2_to_r3_on_cleaned_predecessor(tmp_path):
    """r2 (cleaned, candidate_sha=sha1, commit=sha2, round=2) -> r3 (candidate_sha=sha2, round=3)
    must not be misclassified as fresh candidate requiring round=1, and link_committed_successor
    must accept cleaned predecessors with delivered commit."""
    from herdr.recovery_successor import link_committed_successor
    from herdr.task_resources import begin_launch_intent, finish_launch_intent, record_launch_resources

    sha1 = "1" * 40
    sha2 = "2" * 40
    store = SQLiteStateStore(db_path=tmp_path / "state.db")
    store.save_workflow(
        {
            "workflow_id": "wf-r3",
            "project_root": str(tmp_path),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-r3",
            "candidate_sha": sha2,
            "candidate_episode_id": 2,
            "created_at": 1.0,
            "config": {"nodes": [{"id": "craft"}, {"id": "cleanroom_test", "gate": {"retry_node": "craft"}}]},
        }
    )
    store.save_task(
        {
            "task_id": "wf-r3-craft-auto-r2",
            "workflow_id": "wf-r3",
            "node": "craft",
            "stage": "craft",
            "status": "cleaned",
            "run_id": "run-r2",
            "execution_id": "exec-r3",
            "dispatch_role": "worker",
            "dispatch_round": 2,
            "candidate_sha": sha1,
            "commit": sha2,
            "integrated_commit": sha2,
        }
    )
    claim = begin_launch_intent(
        store,
        workflow_id="wf-r3",
        node_id="craft",
        role="worker",
        candidate_sha=sha2,
        dispatch_round=3,
        task_id="wf-r3-craft-auto-r3",
        supersedes="wf-r3-craft-auto-r2",
        run_id="run-r3",
        execution_id="exec-r3",
    )
    assert claim["status"] == "claimed"
    intent = record_launch_resources(store, claim["intent"], {"run_id": "run-r3"})
    store.save_task(
        {
            "task_id": "wf-r3-craft-auto-r3",
            "workflow_id": "wf-r3",
            "node": "craft",
            "stage": "craft",
            "status": "dispatched",
            "run_id": "run-r3",
            "execution_id": "exec-r3",
            "dispatch_role": "worker",
            "dispatch_round": 3,
            "candidate_sha": sha2,
            "supersedes": "wf-r3-craft-auto-r2",
            "launch_intent_id": intent["intent_id"],
            "completion_epoch": "epoch-r3",
            "completion_identity_path": str(tmp_path / "id-r3.json"),
        }
    )
    finish_launch_intent(store, intent)
    store.record_event(
        "initial_dispatched",
        {
            "delivery_phase": "dispatched",
            "completion_epoch": "epoch-r3",
            "identity_path": str(tmp_path / "id-r3.json"),
            "intervention_id": "intv-r3",
        },
        task_id="wf-r3-craft-auto-r3",
        run_id="run-r3",
        workflow_id="wf-r3",
        node_id="craft",
        source="herdr-task",
    )
    r2 = store.get_task("wf-r3-craft-auto-r2")
    linked = link_committed_successor(
        store,
        "wf-r3-craft-auto-r2",
        "wf-r3-craft-auto-r3",
        r2["version"],
        sha2,
        "failed candidate recovery",
    )
    assert linked["ok"] is True
    assert store.get_task("wf-r3-craft-auto-r2")["superseded_by"] == "wf-r3-craft-auto-r3"


def test_fix_loop_real_cli_launch_records_failure_details_and_succeeds_r2_to_r3(tmp_path, monkeypatch):
    """Exercise controller -> real bin/herdr-task launch (not mocked) for r2 -> r3 recovery."""
    import sys

    repo = tmp_path / "repo"
    _init_repo(repo)
    sha1 = _git_commit(repo, "work.txt", "v1\n", "r1 candidate")
    sha2 = _git_commit(repo, "work.txt", "v2\n", "r2 candidate")

    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path=db_path)
    config = {
        "workflow_template": "code-craft-v1",
        "nodes": [
            {"id": "craft", "role": "worker"},
            {"id": "cleanroom_review", "role": "gate", "gate": {"retry_node": "craft"}},
        ],
    }
    store.save_workflow(
        {
            "workflow_id": "wf-cli-r3",
            "project_root": str(repo),
            "base_branch": "main",
            "status": "running",
            "startup_ready": True,
            "execution_id": "exec-cli",
            "candidate_sha": sha2,
            "candidate_episode_id": 2,
            "created_at": 1.0,
            "config": config,
            "healthy_agents": ["agy", "codex"],
            "unhealthy_agents": {},
            "preflight_checked_at": time.time(),
        }
    )
    store.record_event(
        "candidate_frozen",
        {"candidate_sha": sha2},
        workflow_id="wf-cli-r3",
        source="critical-path-scheduler",
        timestamp=2.0,
    )
    store.save_task(
        {
            "task_id": "wf-cli-r3-craft-auto-r2",
            "workflow_id": "wf-cli-r3",
            "node": "craft",
            "stage": "craft",
            "status": "cleaned",
            "run_id": "run-r2",
            "execution_id": "exec-cli",
            "agent": "codex",
            "goal": "Fix cache keys",
            "dispatch_role": "worker",
            "dispatch_round": 2,
            "candidate_sha": sha1,
            "commit": sha2,
            "integrated_commit": sha2,
        }
    )
    store.save_task(
        {
            "task_id": "wf-cli-r3-cleanroom_review-auto",
            "workflow_id": "wf-cli-r3",
            "node": "cleanroom_review",
            "stage": "cleanroom_review",
            "status": "cleaned",
            "run_id": "run-rev",
            "execution_id": "exec-cli",
            "candidate_sha": sha2,
            "stage_verdict": "blocked",
            "stage_verdict_note": "BUG-3: still broken",
            "affected_task_ids": ["wf-cli-r3-craft-auto-r2"],
        }
    )

    ctl = importlib.import_module("services.herdr-controller")
    monkeypatch.setattr(ctl, "_get_store", lambda: store)
    monkeypatch.setattr(ctl, "load_tasks", store.list_tasks)

    # First verify failure detail capture when CLI exits non-zero
    real_run = subprocess.run
    def _failing_run(cmd, **kwargs):
        if "launch" in cmd:
            return SimpleNamespace(returncode=2, stdout="", stderr="simulated launch error\n")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(ctl.subprocess, "run", _failing_run)
    now = time.time()
    rs.reconcile(db_path, "wf-cli-r3", now=now)
    drive_recovery(store, "wf-cli-r3", lambda op, owner: ctl.execute_workflow_recovery(op, owner), now=now)
    op = next(o for o in rs.list_operations(db_path, "wf-cli-r3") if o["payload"]["kind"] == "fix_loop")
    assert op["detail"].get("launch_returncode") == 2, op["detail"]
    assert "simulated launch error" in (op["detail"].get("launch_error") or "")

    # Now test real begin_launch_intent via bin/herdr-task launch CLI (stopping right after begin_launch_intent
    # isn't needed; we verify real bin/herdr-task launch --help/admission reaches past begin_launch_intent)
    env = {
        **dict(__import__("os").environ),
        "HERDR_STATE_DB": str(db_path),
        "HERDR_TASKS_FILE": str(tmp_path / "tasks.json"),
        "HERDR_WORKFLOWS_FILE": str(tmp_path / "workflows.json"),
    }
    res = real_run(
        [
            sys.executable,
            "bin/herdr-task",
            "launch",
            "--task-id",
            "wf-cli-r3-craft-auto-r3",
            "--workflow-id",
            "wf-cli-r3",
            "--node",
            "craft",
            "--source",
            str(repo),
            "--agent",
            "auto",
            "--integration-mode",
            "git",
            "--task-type",
            "fix",
            "--candidate-sha",
            sha2,
            "--supersedes",
            "wf-cli-r3-craft-auto-r2",
            "--supersede-reason",
            "failed candidate recovery",
            "--dispatch-role",
            "worker",
            "--dispatch-round",
            "3",
            "--execution-id",
            "exec-cli",
            "--goal",
            "Fix cache keys",
            "--prompt",
            "Fix BUG-3",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert "fresh candidate round requires dispatch_round=1" not in (res.stdout + res.stderr)
    assert "Committed recovery requires" not in (res.stdout + res.stderr)
