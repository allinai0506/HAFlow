"""Regression tests for P1 orchestration reliability fixes:
1. PR target base branch resolution (anchor_branch vs pr_base_branch / note base / origin HEAD)
   and executing git push from clone_path when it contains the certified candidate SHA.
2. wrapup_delivery enablement and verifier retirement for code-craft-v1.
3. Auto-superseding stale waiting_human (dispatch_replan_exhausted / generation_changed)
   when the node already has a completed non-superseded task in the current execution.
4. Closing the predecessor back-pointer (superseded_by) on baseline_stale fail-fast refusal.
"""
import subprocess
import time
from types import SimpleNamespace

import pytest

from herdr import node_dispatch as nd
from herdr import pr_delivery
from herdr import workflow_docs as wd
from herdr import wrapup_delivery
from herdr.state_store import SQLiteStateStore
from herdr.task_cli_launch import record_router_failure_task


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class _FakeProvider:
    def __init__(self, sha):
        self.sha = sha
        self.rows = []
        self.created_args = []

    def list_open(self, head, base):
        return self.rows

    def create(self, head, base, title, body, draft):
        self.created_args.append((head, base, title))
        row = {
            "number": 19,
            "html_url": "https://github.com/test/project/pull/19",
            "state": "open",
            "head": {"sha": self.sha, "ref": head},
            "base": {"ref": base},
        }
        self.rows = [row]
        return row


def _setup_worktree_delivery(tmp_path, monkeypatch, *, note_base="dev", wf_pr_base=None, with_clone=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "dev")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "dev")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/dev")
    _git(repo, "remote", "set-head", "origin", "dev")

    # Local-only worktree anchor branch (never pushed to origin)
    _git(repo, "switch", "-c", "agent/gemini-init")
    _git(repo, "switch", "-c", "herdr/integration-t1")
    (repo / "app.txt").write_text("candidate\n")
    _git(repo, "commit", "-am", "candidate")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/herdr/tasks/t1", sha)
    _git(repo, "switch", "agent/gemini-init")

    clone_dir = None
    if with_clone:
        clone_dir = tmp_path / "clone-t1"
        subprocess.run(["git", "clone", str(repo), str(clone_dir)], check=True, capture_output=True)
        _git(clone_dir, "checkout", sha)

    push_cwds = []
    real_git = pr_delivery._git

    def wrapped_git(target_repo, *args):
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/test/project.git"
        if args and args[0] == "push":
            push_cwds.append(str(target_repo))
        if args and args[0] in {"ls-remote", "push"}:
            args = tuple(str(remote) if v == "https://github.com/test/project.git" else v for v in args)
        return real_git(target_repo, *args)

    monkeypatch.setattr(pr_delivery, "_git", wrapped_git)

    store = SQLiteStateStore(tmp_path / "state.db")
    monkeypatch.setenv("HERDR_STATE_DB", str(store.db_path))
    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "docs"))

    wf = {"workflow_id": "wf-p1", "status": "running", "base_branch": "agent/gemini-init"}
    if wf_pr_base is not None:
        wf["pr_base_branch"] = wf_pr_base
    store.save_workflow(wf)
    task = {
        "task_id": "t1",
        "workflow_id": "wf-p1",
        "status": "integrated",
        "source_repo": str(repo),
        "integration_branch": "herdr/integration-t1",
        "integration_ref": "refs/herdr/tasks/t1",
        "integrated_commit": sha,
        "base_branch": "agent/gemini-init",
        "run_id": "run-1",
    }
    if clone_dir is not None:
        task["clone_path"] = str(clone_dir)
    store.save_task(task)
    for tid in ("review", "test"):
        store.save_task({
            "task_id": tid,
            "workflow_id": "wf-p1",
            "status": "completed",
            "stage_verdict": "pass",
            "candidate_sha": sha,
            "verified_candidate_sha": sha,
            "node": tid,
        })
    fields = {
        "delivery_id": f"candidate-{sha}",
        "delivery_branch": "herdr/integration-t1",
        "candidate_sha": sha,
        "review_task": "review",
        "test_gate": "test",
    }
    if note_base is not None:
        fields["base"] = note_base
    wd.append_note("wf-p1", kind="delivery", title="candidate", body="", task_id="review", fields=fields)
    return store, repo, clone_dir, remote, sha, push_cwds


def test_pr_delivery_uses_verified_note_base_for_local_anchor_and_pushes_from_trusted_source_repo(tmp_path, monkeypatch):
    store, repo, clone_dir, remote, sha, push_cwds = _setup_worktree_delivery(
        tmp_path, monkeypatch, note_base="dev", with_clone=True
    )
    provider = _FakeProvider(sha)
    receipt = pr_delivery.create_task_pr(store, "t1", title="P1 fix", client=provider)
    assert receipt["base"] == "dev"
    assert provider.created_args == [("herdr/integration-t1", "dev", "P1 fix")]
    # Security boundary: push MUST run from trusted source_repo, never untrusted agent clone_dir
    assert push_cwds == [str(repo)]
    assert _git(remote, "rev-parse", "refs/heads/herdr/integration-t1") == sha


def test_pr_delivery_note_base_cannot_override_operator_remote_base_branch(tmp_path, monkeypatch):
    store, repo, _, _, sha, _ = _setup_worktree_delivery(
        tmp_path, monkeypatch, note_base="main", with_clone=False
    )
    # Operator configured a real remote base_branch ('dev' exists at refs/remotes/origin/dev)
    resolved = pr_delivery._resolve_pr_base(
        repo, note_base="main", task_base="dev", workflow_base="dev"
    )
    assert resolved == "dev"
    # Explicit operator pr_base_branch still wins over task_base
    resolved_explicit = pr_delivery._resolve_pr_base(
        repo, pr_base="release/2.0", note_base="main", task_base="dev"
    )
    assert resolved_explicit == "release/2.0"


def test_pr_delivery_falls_back_to_origin_head_for_local_anchor(tmp_path, monkeypatch):
    store, repo, _, _, sha, _ = _setup_worktree_delivery(
        tmp_path, monkeypatch, note_base=None, with_clone=False
    )
    provider = _FakeProvider(sha)
    receipt = pr_delivery.create_task_pr(store, "t1", title="Anchor fallback", client=provider)
    assert receipt["base"] == "dev"


def test_wrapup_delivery_enabled_for_code_craft_v1_and_retires_cleanroom_verifiers(tmp_path):
    from pathlib import Path
    from herdr.workflow import load_template

    cfg = load_template(str(Path(__file__).resolve().parents[1] / "workflow_templates" / "code-craft-v1.yaml"))
    cfg["workflow_template"] = "code-craft-v1"
    wrap_task = {
        "task_id": "wf-cc-wrapup",
        "node": "wrapup",
        "integration_mode": "none",
        "execution_mode": "git",
    }
    assert wrapup_delivery.enabled(wrap_task, cfg) is True

    store = SQLiteStateStore(tmp_path / "cc.db")
    old_sha = "a" * 40
    new_sha = "b" * 40
    store.save_workflow({
        "workflow_id": "wf-cc",
        "status": "running",
        "execution_id": "exec-cc",
        "config": cfg,
    })
    ev = store.record_event(
        "candidate_frozen",
        {"candidate_sha": new_sha, "source_node": "wrapup"},
        workflow_id="wf-cc",
        source="critical-path-scheduler",
    )
    ep_id = ev["id"]
    for nid in ("cleanroom_test", "cleanroom_review"):
        store.save_task({
            "task_id": f"wf-cc-{nid}",
            "workflow_id": "wf-cc",
            "node": nid,
            "stage": nid,
            "execution_id": "exec-cc",
            "status": "cleaned",
            "integration_mode": "none",
            "candidate_sha": old_sha,
            "candidate_episode_id": ep_id - 1,
            "stage_verdict": "pass",
        })

    assert wrapup_delivery.reconcile_verifiers(store, "wf-cc") is True
    for nid in ("cleanroom_test", "cleanroom_review"):
        t = store.get_task(f"wf-cc-{nid}")
        assert t["status"] == "superseded"
        assert t["replacement_pending"] is True


def test_node_dispatch_supersedes_replan_exhausted_when_node_already_completed():
    wid, eid = "wf-zombie", "exec-z"
    workflow = {"workflow_id": wid, "execution_id": eid, "status": "running"}
    config = {"nodes": [{"id": "craft"}]}
    predecessors = [{"task_id": "craft-r3", "run_id": "run-3"}]
    payload = nd.payload(workflow, config, config["nodes"][0], predecessors)
    operation = {
        "id": 417442,
        "workflow_id": wid,
        "status": "waiting_human",
        "started": False,
        "identity_key": payload["identity_key"],
        "payload": payload,
        "detail": {"replan_count": 2, "reason": "dispatch_replan_exhausted"},
    }
    tasks = [
        {
            "task_id": "craft-r3",
            "workflow_id": wid,
            "node": "craft",
            "stage": "craft",
            "execution_id": eid,
            "run_id": "run-3",
            "status": "superseded",
            "superseded_by": "craft-r7",
            "replacement_pending": False,
        },
        {
            "task_id": "craft-r7",
            "workflow_id": wid,
            "node": "craft",
            "stage": "craft",
            "execution_id": eid,
            "run_id": "run-7",
            "status": "cleaned",
            "supersedes": "craft-r3",
        },
    ]
    outcome = nd.result(operation, workflow, config, tasks, time.time())
    assert outcome == (
        "superseded",
        {"reason": "dispatch_existing_tasks", "responsibility_transferred_to": "registered_tasks"},
    )

    # Negative guard 1: if craft-r7 is still working (not complete), keep waiting_human
    tasks[1]["status"] = "working"
    outcome_working = nd.result(operation, workflow, config, tasks, time.time())
    assert outcome_working[0] == "waiting_human"
    assert outcome_working[1]["reason"] == "dispatch_replan_exhausted"

    # Negative guard 2: active task with non-empty blocker must NOT auto-supersede
    tasks[1]["status"] = "cleaned"
    tasks[1]["blocker"] = "unresolved review finding"
    outcome_blocked = nd.result(operation, workflow, config, tasks, time.time())
    assert outcome_blocked[0] == "waiting_human"
    assert outcome_blocked[1]["reason"] == "dispatch_replan_exhausted"

    # Negative guard 3: sibling task in same node still working or mismatched candidate_episode_id
    tasks[1]["blocker"] = ""
    wf_ep = {
        "workflow_id": wid,
        "execution_id": eid,
        "status": "running",
        "candidate_sha": "a" * 40,
        "candidate_episode_id": 2,
    }
    cfg_gate = {"nodes": [{"id": "craft"}, {"id": "cleanroom_test", "depends_on": ["craft"]}]}
    gate_payload = nd.payload(wf_ep, cfg_gate, cfg_gate["nodes"][1], [{"task_id": "t-old", "run_id": "r1"}])
    gate_op = {
        "id": 999,
        "workflow_id": wid,
        "status": "waiting_human",
        "started": False,
        "identity_key": gate_payload["identity_key"],
        "payload": gate_payload,
        "detail": {"replan_count": 2},
    }
    gate_tasks = [
        {
            "task_id": "t-old",
            "workflow_id": wid,
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "execution_id": eid,
            "run_id": "r1",
            "status": "superseded",
            "superseded_by": "t-ep1",
            "replacement_pending": False,
        },
        {
            "task_id": "t-ep1",
            "workflow_id": wid,
            "node": "cleanroom_test",
            "stage": "cleanroom_test",
            "execution_id": eid,
            "run_id": "r2",
            "status": "cleaned",
            "candidate_sha": "a" * 40,
            "candidate_episode_id": 1,  # Stale episode != 2
            "stage_verdict": "pass",
        },
    ]
    assert nd._node_already_completed(gate_op, wf_ep, cfg_gate, gate_tasks) is False
    # Sibling task with different/empty candidate_sha still working must also block completion
    gate_tasks[1]["candidate_episode_id"] = 2
    gate_tasks.append({
        "task_id": "t-sibling",
        "workflow_id": wid,
        "node": "cleanroom_test",
        "stage": "cleanroom_test",
        "execution_id": eid,
        "run_id": "r3",
        "status": "working",
        "candidate_sha": "",
    })
    assert nd._node_already_completed(gate_op, wf_ep, cfg_gate, gate_tasks) is False


def test_baseline_stale_refusal_closes_predecessor_superseded_by_link(tmp_path):
    store = SQLiteStateStore(tmp_path / "stale.db")
    store.save_workflow({"workflow_id": "wf-stale", "status": "running", "execution_id": "exec-s"})
    store.save_task({
        "task_id": "craft-r3",
        "workflow_id": "wf-stale",
        "node": "craft",
        "stage": "craft",
        "execution_id": "exec-s",
        "run_id": "run-3",
        "dispatch_round": 3,
        "status": "superseded",
        "replacement_pending": True,
    })
    from herdr.task_resources import begin_launch_intent

    claim = begin_launch_intent(
        store,
        workflow_id="wf-stale",
        node_id="craft",
        role="worker",
        candidate_sha="",
        dispatch_round=4,
        task_id="craft-r4",
        supersedes="craft-r3",
        run_id="run-4",
        execution_id="exec-s",
    )
    args = SimpleNamespace(
        task_id="craft-r4",
        workflow_id="wf-stale",
        run_id="run-4",
        agent="codex",
        execution_id="exec-s",
        integration_mode="git",
        goal="fix",
        acceptance=[],
        artifact_mode=None,
        _launch_intent=claim["intent"],
    )
    record_router_failure_task(
        args,
        "craft",
        "[BASELINE STALE] behind dev by 2 commits",
        get_store=lambda: store,
        new_run_id=lambda: "run-4",
        now=lambda: time.time(),
        release_agent_reservation=lambda tid: None,
        reason="baseline_stale",
        failure_code="baseline_stale",
    )
    pred = store.get_task("craft-r3")
    r4 = store.get_task("craft-r4")
    assert pred["superseded_by"] == "craft-r4"
    assert pred["replacement_pending"] is False
    assert r4["supersedes"] == "craft-r3"
    assert r4["launch_not_started"] is True
