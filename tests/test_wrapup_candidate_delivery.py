"""Real Git/CLI/state regression for post-certification wrapup commits.

X history is a legal fixture; Y integration, freeze and all new evidence must
be produced by platform entries. Agent and GitHub transport are hermetic.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import yaml

from herdr import scheduler_facts, workflow_docs as wd
from herdr.state_store import SQLiteStateStore
from herdr.task_cli_evidence import record_delivery_note

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def cli(scene, *args, ok=True):
    result = subprocess.run(
        [sys.executable, str(ROOT / "bin/herdr-task"), *args],
        cwd=ROOT,
        env=scene.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if ok:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(params=["software-development-v1", "spec-development-v1"])
def scene(tmp_path, monkeypatch, request):
    repo, clone, remote = (
        tmp_path / "repo",
        tmp_path / ".herdr-controller/clones/wrap",
        tmp_path / "remote.git",
    )
    clone.parent.mkdir(parents=True)
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.test")
    (repo / "code.py").write_text(
        "answer = 42\n" + "# unchanged context\n" * 30 + "wrapup_note = 'X'\n"
    )
    original = (repo / "code.py").read_text()
    (repo / "code.py").write_text(
        original.replace("wrapup_note = 'X'", "wrapup_note = 'base'")
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "common baseline")
    (repo / "code.py").write_text(original)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "certified implementation X")
    x = git(repo, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    subprocess.run(
        ["git", "clone", "-b", "main", str(remote), str(clone)],
        check=True,
        capture_output=True,
    )
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", "test@example.test")
    git(clone, "switch", "-c", "agent/wrapup")
    from herdr.task_resources import write_worker_launch_identity

    write_worker_launch_identity(
        clone,
        {"task_id": "wrap", "run_id": "r-wrap", "intent_id": "wrap-fixture"},
        initial=True,
    )
    store = SQLiteStateStore(tmp_path / "state.db")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "HERDR_STATE_DB": str(store.db_path),
        "HERDR_WORKFLOW_DOCS_DIR": str(tmp_path / "shared-docs"),
        "HERDR_CONTROLLER_DIR": str(tmp_path / "controller"),
        "TASKS_FILE": str(tmp_path / "tasks.json"),
        "WORKFLOWS_FILE": str(tmp_path / "workflows.json"),
    }
    for key, value in env.items():
        if key.startswith("HERDR_") or key in {"TASKS_FILE", "WORKFLOWS_FILE"}:
            monkeypatch.setenv(key, value)
    cfg = yaml.safe_load(
        (ROOT / "workflow_templates" / (request.param + ".yaml")).read_text()
    )
    store.save_workflow(
        {
            "workflow_id": "wf",
            "execution_id": "exec",
            "status": "running",
            "config": cfg,
            "project_root": str(repo),
            "base_branch": "main",
        }
    )
    store.save_task(
        {
            "task_id": "impl",
            "workflow_id": "wf",
            "execution_id": "exec",
            "run_id": "r-impl",
            "node": "implementation",
            "status": "integrated",
            "integration_mode": "git",
            "commit": x,
            "integrated_commit": x,
            "integration_ref": "refs/herdr/tasks/impl",
            "integration_branch": "herdr/integration-impl",
            "source_repo": str(repo),
            "base_branch": "main",
        }
    )
    git(repo, "update-ref", "refs/herdr/tasks/impl", x)
    git(repo, "update-ref", "refs/heads/herdr/integration-impl", x)
    scheduler_facts.record_candidate_frozen(
        "wf", x, source_node="implementation", db_path=store.db_path
    )
    wf = store.get_workflow("wf")
    gate_nodes = ["test", "review"] + (
        ["acceptance"] if any(n["id"] == "acceptance" for n in cfg["nodes"]) else []
    )
    for node in gate_nodes:
        store.save_task(
            {
                "task_id": "old-" + node,
                "workflow_id": "wf",
                "execution_id": "exec",
                "run_id": "old-r-" + node,
                "node": node,
                "status": "cleaned",
                "stage_verdict": "pass",
                "candidate_sha": x,
                "verified_candidate_sha": x,
                "candidate_episode_id": wf["candidate_episode_id"],
                "agent": "test-agent" if node == "test" else "review-agent",
                "integration_mode": "none",
            }
        )
    record_delivery_note(
        "wf", "herdr/integration-impl", x, "old-review", "old-test", base="main"
    )
    store.save_task(
        {
            "task_id": "wrap",
            "workflow_id": "wf",
            "execution_id": "exec",
            "run_id": "r-wrap",
            "node": "wrapup",
            "stage": "wrapup",
            "status": "working",
            "agent": "wrap-agent",
            "integration_mode": "none",
            "execution_mode": "git",
            "source_repo": str(repo),
            "clone_path": str(clone),
            "branch": "agent/wrapup",
            "base_branch": "main",
            "baseline_commit": x,
            "candidate_sha": x,
            "candidate_episode_id": wf["candidate_episode_id"],
            "started_at": time.time() - 120,
            "baseline_fingerprint": {"tracked": {}, "untracked": {}},
            "baseline_untracked": [],
        }
    )
    from herdr.completion_receipt import issue_completion_contract

    issue_completion_contract("wrap", store)
    return SimpleNamespace(
        store=store,
        repo=repo,
        clone=clone,
        remote=remote,
        x=x,
        env=env,
        cfg=cfg,
        root=tmp_path,
    )


def test_wrapup_added_commit_can_register_and_integrate_before_completion(scene):
    (scene.clone / "knowledge.md").write_text("Knowledge collected during wrapup.\n")
    git(scene.clone, "add", "knowledge.md")
    git(scene.clone, "commit", "-m", "wrapup knowledge Y")
    y = git(scene.clone, "rev-parse", "HEAD")
    cli(scene, "commit", "wrap")
    assert scene.store.get_task("wrap")["status"] == "working"
    assert scene.store.get_task("wrap")["commit"] == y
    cli(scene, "integrate", "wrap")
    task = scene.store.get_task("wrap")
    assert task["status"] == "working"
    assert task["integrated_commit"] == git(scene.clone, "rev-parse", "HEAD")
    assert (
        git(scene.repo, "show", task["integration_ref"] + ":knowledge.md")
        == "Knowledge collected during wrapup."
    )
    assert scene.store.list_events(workflow_id="wf", event_type="candidate_published")


@pytest.fixture
def controller(scene, monkeypatch):
    import importlib.util
    import queue

    spec = importlib.util.spec_from_file_location(
        "wrapup_candidate_controller", ROOT / "services/herdr-controller.py"
    )
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    for node in scene.cfg["nodes"]:
        if node["id"] in {"requirements", "specify", "plan"}:
            scene.store.save_task(
                {
                    "task_id": node["id"],
                    "workflow_id": "wf",
                    "node": node["id"],
                    "status": "cleaned",
                    "integration_mode": "none",
                    "run_id": "r-" + node["id"],
                    "execution_id": "exec",
                }
            )
    overrides = {
        "_get_store": lambda: scene.store,
        "get_task": scene.store.get_task,
        "load_tasks": lambda: scene.store.list_tasks(workflow_id="wf"),
        "workflow_config_for": lambda wid: scene.cfg,
        "project_for_workflow": lambda wid: dict(
            scene.store.get_workflow(wid),
            project_root=str(scene.repo),
            base_branch="main",
            coordinator_pane_id="test-only-coordinator",
            requirement="certify fixture code and wrapup knowledge",
            startup_ready=True,
        ),
        "coordinator_pane_for_workflow": lambda wid: "test-only-coordinator",
        "STAGE_STATE_FILE": str(scene.root / "stage-state.json"),
        "STAGE_POLICIES_FILE": str(scene.root / "stage-policies.json"),
        "coordinator_queue": queue.Queue(),
        "workflow_closed": lambda wid: False,
        "_workflow_entry": scene.store.get_workflow,
        "notify_attention": lambda *a, **kw: None,
        "attention_get": lambda *a: None,
        "attention_note": lambda *a, **kw: None,
        "attention_blocks_retry": lambda *a: False,
    }
    for name, value in overrides.items():
        monkeypatch.setattr(ctl, name, value)
    (scene.root / "stage-policies.json").write_text("{}")
    scene.controller = ctl
    return ctl


def add_output(scene, text="Knowledge collected during wrapup."):
    (scene.clone / "knowledge.md").write_text(text + "\n")
    cli(scene, "commit", "wrap", "--message", "wrapup output")
    source = git(scene.clone, "rev-parse", "HEAD")
    cli(scene, "integrate", "wrap")
    return source, scene.store.get_task("wrap")["integrated_commit"]


def test_active_wrapup_controller_freezes_and_queues_both_verifiers(scene, controller):
    y, s = add_output(scene)
    controller.check_workflow_stage_advance("wf")
    frozen = scheduler_facts.list_candidate_frozen_events(
        "wf", db_path=scene.store.db_path
    )
    assert frozen[-1]["payload"]["candidate_sha"] == s
    assert frozen[-1]["payload"]["source_node"] == "wrapup"
    assert scene.store.get_task("old-test")["status"] == "superseded"
    assert scene.store.get_task("old-review")["status"] == "superseded"
    queued = list(controller.coordinator_queue.queue)
    assert {event["node_id"] for event in queued} == {"test", "review"}, queued
    assert scene.store.get_task("wrap")["status"] == "working"
    assert not any(
        (event.get("payload") or {}).get("decision") == "reuse"
        for event in scheduler_facts.list_reverification_decisions(
            "wf", db_path=scene.store.db_path
        )
    )
    controller.check_workflow_stage_advance("wf")
    assert len(list(controller.coordinator_queue.queue)) == 2
    assert (
        len(
            scheduler_facts.list_candidate_frozen_events(
                "wf", db_path=scene.store.db_path
            )
        )
        == 2
    )


@pytest.fixture
def agents(scene, controller, monkeypatch):
    """Only the external launch/Agent is substituted; dispatch receipts are real."""
    import time
    from herdr import completion_receipt
    from herdr.task_resources import begin_launch_intent, finish_launch_intent
    from herdr.task_cli_launch_preflight import (
        preflight_delivery_identity,
        validate_test_review_baseline,
        _resolve_expected_candidate,
    )

    launched = []
    real_run = subprocess.run

    def run(command, **kwargs):
        if len(command) < 2 or command[1] != "launch":
            return real_run(command, **kwargs)

        def arg(key, default=None):
            return command[command.index(key) + 1] if key in command else default

        tid, node, sha = arg("--task-id"), arg("--node"), arg("--candidate-sha")
        run_id = "run-" + tid
        op_id = int(arg("--dispatch-operation-id"))
        claim = begin_launch_intent(
            scene.store,
            workflow_id="wf",
            node_id=node,
            task_id=tid,
            candidate_sha=sha,
            dispatch_round=int(arg("--dispatch-round")),
            supersedes=arg("--supersedes"),
            dispatch_operation_id=op_id,
            dispatch_attempt=int(arg("--dispatch-attempt")),
            execution_id="exec",
            run_id=run_id,
        )
        assert "intent" in claim, claim
        if node in {"test", "review"}:
            preflight = preflight_delivery_identity(
                "wf", node, tid, sha, get_store=lambda: scene.store
            )
            assert preflight["candidate_sha"] == sha
        clone = scene.root / ".herdr-controller/clones" / tid
        real_run(
            ["git", "clone", str(scene.repo), str(clone)],
            check=True,
            capture_output=True,
        )
        git(clone, "checkout", "--detach", sha)
        from herdr.task_resources import write_worker_launch_identity

        write_worker_launch_identity(
            clone,
            {
                "task_id": tid,
                "run_id": run_id,
                "intent_id": claim["intent"]["intent_id"],
            },
            initial=True,
        )
        if node in {"test", "review"}:
            validate_test_review_baseline(
                SimpleNamespace(
                    workflow_id="wf", task_id=tid, node=node, candidate_sha=sha
                ),
                git(clone, "rev-parse", "HEAD"),
                str(scene.repo),
                get_store=lambda: scene.store,
                resolve_expected_candidate=_resolve_expected_candidate,
            )
        criteria = [
            command[i + 1]
            for i, value in enumerate(command[:-1])
            if value == "--acceptance"
        ]
        wf = scene.store.get_workflow("wf")
        scene.store.save_task(
            {
                "task_id": tid,
                "workflow_id": "wf",
                "node": node,
                "stage": node,
                "execution_id": "exec",
                "run_id": run_id,
                "status": "working",
                "agent": node + "-agent",
                "clone_path": str(clone),
                "source_repo": str(scene.repo),
                "execution_mode": "git",
                "integration_mode": "none",
                "candidate_sha": sha,
                "candidate_episode_id": wf["candidate_episode_id"],
                "baseline_commit": sha,
                "started_at": time.time() - 120,
                "acceptance_criteria": criteria,
                "dispatch_role": arg("--dispatch-role"),
                "dispatch_round": int(arg("--dispatch-round")),
                "dispatch_operation_id": op_id,
                "launch_intent_id": claim["intent"]["intent_id"],
                "supersedes": arg("--supersedes"),
            }
        )
        finish_launch_intent(scene.store, claim["intent"])
        scene.store.transition_task(
            arg("--supersedes"),
            "superseded",
            "replacement Agent launched",
            metadata={"superseded_by": tid, "replacement_pending": True},
        )
        completion_receipt.issue_completion_contract(tid, scene.store)
        launched.append(tid)
        return subprocess.CompletedProcess(command, 0, "Task dispatched: " + tid, "")

    class Transport:
        def __getattr__(self, key):
            return run if key == "run" else getattr(subprocess, key)

    monkeypatch.setattr(controller, "subprocess", Transport())
    monkeypatch.setattr(controller, "maybe_dispatch_node_handoffs", lambda **kw: [])
    monkeypatch.setattr(controller, "maybe_compact_coordinator", lambda *a, **kw: None)
    return launched


def dispatch_verifiers(scene, controller, agents):
    controller.check_workflow_stage_advance("wf")
    while not controller.coordinator_queue.empty():
        event = controller.coordinator_queue.get_nowait()
        assert event["node_id"] in {"test", "review"}, event
        controller._handle_stage_advance_item(event)
    assert {scene.store.get_task(t)["node"] for t in agents[-2:]} == {"test", "review"}
    return agents[-2:]


def certify(scene, tid, *, verdict="pass", acceptance=True):
    """Fake independent Agent executes work, publishes actual evidence via CLI."""
    from herdr import completion_receipt

    task = scene.store.get_task(tid)
    # Test and review workers each inspect the actual pinned Git tree. This
    # fixture substitutes their external intelligence, never platform evidence.
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            'from code import answer; assert answer == 42; print("fixture check passed")',
        ],
        cwd=task["clone_path"],
        text=True,
        capture_output=True,
        check=True,
    )
    report = scene.root / (tid + "-report.txt")
    report.write_text(
        task["node"]
        + " independent fixture execution at "
        + task["candidate_sha"]
        + "\n"
        + result.stdout
    )
    identity = [
        "--task-id",
        tid,
        "--run-id",
        task["run_id"],
        "--epoch",
        task["completion_epoch"],
    ]
    pub = cli(
        scene,
        "checkpoint-publish",
        *identity,
        "--segment-file",
        str(report),
        "--step",
        "1",
    )
    receipt = json.loads(pub.stdout)
    artifact = receipt["observation_id"] + ":" + receipt["sha256"]
    if acceptance:
        criteria = sum(
            (
                ["--criterion", f"AC-{i + 1}={verdict}"]
                for i in range(len(task["acceptance_criteria"]))
            ),
            [],
        )
        cli(
            scene,
            "acceptance-record",
            *identity,
            "--candidate-sha",
            task["candidate_sha"],
            "--verdict",
            verdict,
            "--artifact",
            artifact,
            *criteria,
        )
    cli(
        scene,
        "report-completion",
        tid,
        "--identity-file",
        task["completion_identity_path"],
        "--artifact",
        artifact,
    )
    consumed = completion_receipt.consume_completion_receipt(tid, scene.store)
    assert consumed["accepted"], consumed
    return cli(
        scene,
        "set",
        tid,
        "completed",
        "--verdict",
        verdict,
        "--note",
        "Independent fixture evidence checked",
        ok=acceptance,
    )


class FakeForge:
    def __init__(self, scene):
        self.scene, self.rows, self.creates = scene, [], 0
        self.fail_after_create = False

    def list_open(self, head, base):
        return [
            row
            for row in self.rows
            if row["head"]["ref"] == head and row["base"]["ref"] == base
        ]

    def create(self, head, base, title, body, draft):
        self.creates += 1
        sha = git(self.scene.remote, "rev-parse", "refs/heads/" + head)
        row = {
            "number": self.creates,
            "html_url": f"https://github.com/owner/repo/pull/{self.creates}",
            "state": "open",
            "head": {"ref": head, "sha": sha},
            "base": {"ref": base},
        }
        self.rows.append(row)
        if self.fail_after_create:
            self.fail_after_create = False
            raise RuntimeError("simulated external reply lost after creation")
        return row


@pytest.fixture
def forge(scene, monkeypatch):
    from herdr import pr_delivery
    import runpy

    FAKE_GH_SCRIPT = runpy.run_path(str(ROOT / "tests/test_wrapup_review_closure.py"))[
        "FAKE_GH_SCRIPT"
    ]
    provider = FakeForge(scene)
    monkeypatch.setattr(pr_delivery, "PullRequestClient", lambda *a, **kw: provider)
    real_git = pr_delivery._git

    def transport(repo, *args):
        if args[:2] == ("remote", "get-url"):
            return "https://github.com/owner/repo.git"
        if args and args[0] in {"push", "ls-remote"}:
            args = tuple(
                str(scene.remote)
                if value == "https://github.com/owner/repo.git"
                else value
                for value in args
            )
        return real_git(repo, *args)

    monkeypatch.setattr(pr_delivery, "_git", transport)
    # Real CLI closure uses an external fake gh, with actual live-state reads.
    fakebin = scene.root / "fakebin"
    fakebin.mkdir()
    (fakebin / "gh").write_text(FAKE_GH_SCRIPT)
    (fakebin / "gh").chmod(0o755)
    (fakebin / "head.txt").write_text(scene.x)
    scene.env["PATH"] = str(fakebin) + os.pathsep + scene.env.get("PATH", "")
    scene.fakebin = fakebin
    return provider


def publish(scene, task_id):
    from herdr.task_cli_delivery import create_pull_request

    create_pull_request(
        SimpleNamespace(
            task_id=task_id, title="Fixture delivery", body_file=None, draft=False
        ),
        get_store=lambda: scene.store,
    )
    return scene.store.list_events(
        workflow_id="wf", event_type="pull_request_created", desc=True, limit=1
    )[0]["payload"]


def delivery(scene, tasks, *, acceptance=True):
    from herdr.wrapup_delivery import publication_branch
    from herdr.delivery_record import candidate_identity, select_effective_delivery

    sha = scene.store.get_task("wrap")["integrated_commit"]
    ids = {scene.store.get_task(t)["node"]: t for t in tasks}
    prior = select_effective_delivery(wd.annotate_notes(wd.load_notes("wf")))
    cli(
        scene,
        "record-delivery",
        "--workflow-id",
        "wf",
        "--candidate-sha",
        sha,
        "--delivery-branch",
        publication_branch(scene.store.get_task("wrap"), sha),
        "--review-task",
        ids["review"],
        "--test-gate",
        ids["test"],
        "--base",
        "main",
        "--supersedes",
        candidate_identity(prior),
    )
    if acceptance:
        accept_join(scene)
    return sha


def accept_join(scene, *, verdict="pass"):
    ctl = getattr(scene, "controller", None)
    if not ctl or not any(n["id"] == "acceptance" for n in scene.cfg["nodes"]):
        return
    ctl.check_workflow_stage_advance("wf")
    while not ctl.coordinator_queue.empty():
        event = ctl.coordinator_queue.get_nowait()
        assert event["node_id"] == "acceptance", event
        ctl._handle_stage_advance_item(event)
    active = [
        t
        for t in scene.store.list_tasks(workflow_id="wf")
        if t.get("node") == "acceptance" and t["status"] != "superseded"
    ]
    for task in active:
        if task["status"] == "working":
            certify(scene, task["task_id"], verdict=verdict)


def report_wrapup(scene):
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
    assert consume_completion_receipt("wrap", scene.store)["accepted"]


def _workspace_delivery(scene):
    ceiling = str(
        scene.cfg.get("delivery_ceiling")
        or scene.cfg.get("delivery_ceiling_default")
        or ""
    ).strip()
    return ceiling.lower() in {"workspace", "working_tree", "worktree"} or ceiling == "工作区"


def closure(scene, pr, head, *, ok=True):
    if ok:
        accept_join(scene)
    return cli(
        scene,
        "record-review-closure",
        "wrap",
        "--pr-url",
        pr["url"],
        "--head-sha",
        head,
        "--ci-status",
        "pass",
        "--unresolved-p1",
        "0",
        "--unresolved-p2",
        "0",
        "--findings",
        "All findings dispositioned; independent fixture checks pass",
        ok=ok,
    )


@pytest.mark.parametrize("existing_pr", [False, True])
def test_certified_x_wrapup_y_to_pr_same_head_completion(
    scene, controller, agents, forge, existing_pr
):
    if existing_pr:
        old = publish(scene, "impl")
    y, s = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        certify(scene, tid)
    assert delivery(scene, tasks) == s
    pr = publish(scene, "wrap")
    assert pr["candidate_sha"] == s == git(scene.clone, "rev-parse", "HEAD")
    assert git(scene.remote, "rev-parse", pr["branch"]) == s
    if existing_pr:
        assert pr["url"] != old["url"] and pr["replaces_pr_url"] == old["url"]
    (scene.fakebin / "head.txt").write_text(s)
    closure(scene, pr, s)
    report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")
    assert scene.store.get_task("wrap")["status"] == "completed"
    assert (
        scene.store.list_events(workflow_id="wf", event_type="review_closure_recorded")[
            -1
        ]["payload"]["head_sha"]
        == s
    )
    assert len(
        scene.store.list_events(
            workflow_id="wf", event_type="business_acceptance_recorded"
        )
    ) == (3 if any(n["id"] == "acceptance" for n in scene.cfg["nodes"]) else 2)
    assert all(scene.store.get_task(t)["verified_candidate_sha"] == s for t in tasks)
    from herdr.workflow import is_workflow_completed

    completed = {
        node["id"]
        for node in scene.cfg["nodes"]
        if controller.is_node_complete("wf", node["id"])
    }
    assert is_workflow_completed(scene.cfg, completed)
    from herdr.delivery_record import select_effective_delivery, _body_value

    effective = select_effective_delivery(wd.annotate_notes(wd.load_notes("wf")))
    assert _body_value(effective, "candidate_sha") == s
    trace = {
        "workflow_id": "wf",
        "execution_id": "exec",
        "X": scene.x,
        "Y": y,
        "S": s,
        "frozen_sha": scene.store.get_workflow("wf")["candidate_sha"],
        "completion_head": git(scene.clone, "rev-parse", "HEAD"),
        "delivery_identity": {
            key: _body_value(effective, key)
            for key in ("candidate_sha", "delivery_branch", "test_gate", "review_task")
        },
        "acceptance": [
            {
                key: task.get(key)
                for key in (
                    "task_id",
                    "run_id",
                    "candidate_sha",
                    "verified_candidate_sha",
                    "candidate_episode_id",
                    "stage_verdict",
                )
            }
            for task in scene.store.list_tasks(workflow_id="wf")
            if task.get("node") == "acceptance" and task.get("status") != "superseded"
        ],
        "gates": [
            {
                key: scene.store.get_task(t).get(key)
                for key in (
                    "task_id",
                    "run_id",
                    "candidate_sha",
                    "candidate_episode_id",
                    "verified_candidate_sha",
                    "stage_verdict",
                )
            }
            for t in tasks
        ],
        "publication": pr,
        "receipt": scene.store.list_events(
            workflow_id="wf", event_type="review_closure_recorded"
        )[-1]["payload"],
        "wrapup_status": scene.store.get_task("wrap")["status"],
    }
    cli(scene, "close-workflow", "wf")
    trace["workflow_status"] = scene.store.get_workflow("wf")["status"]
    evidence_dir = os.environ.get("HERDR_TEST_EVIDENCE_DIR")
    if evidence_dir:
        output = Path(evidence_dir)
        output.mkdir(parents=True, exist_ok=True)
        (
            output
            / (
                scene.cfg["name"]
                + ("-existing-pr" if existing_pr else "-no-pr")
                + ".json"
            )
        ).write_text(json.dumps(trace, indent=2))
    assert scene.store.get_workflow("wf")["status"] == "completed"


def test_no_additional_commit_existing_candidate_path_remains_valid(scene, forge):
    cli(scene, "commit", "wrap")
    assert not scene.store.get_task("wrap").get("wrapup_delivery")
    pr = publish(scene, "impl")
    closure(scene, pr, scene.x)
    report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")
    assert scene.store.get_task("wrap")["status"] == "completed"
    assert git(scene.clone, "rev-parse", "HEAD") == scene.x


@pytest.mark.parametrize(
    "failure",
    [
        "test",
        "review",
        "business",
        "ci-pending",
        "ci-red",
        "p1",
        "wrong-pr",
        "old-head",
    ],
)
def test_uncertified_or_forge_blocked_candidate_cannot_complete(
    scene, controller, agents, forge, failure
):
    y, s = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        node = scene.store.get_task(tid)["node"]
        certify(
            scene,
            tid,
            verdict="blocked" if failure == node else "pass",
            acceptance=not (failure == "business" and node == "test"),
        )
    delivery(scene, tasks)
    if failure in {"test", "review", "business"}:
        with pytest.raises(ValueError, match="gate"):
            publish(scene, "wrap")
    else:
        pr = publish(scene, "wrap")
        (scene.fakebin / "head.txt").write_text(s)
        if failure.startswith("ci-"):
            (scene.fakebin / "checks_rc").write_text(
                "8" if failure == "ci-pending" else "1"
            )
        if failure == "p1":
            (scene.fakebin / "threads.json").write_text(
                json.dumps(
                    [
                        {
                            "isResolved": False,
                            "comments": {
                                "nodes": [
                                    {
                                        "author": {
                                            "login": "chatgpt-codex-connector[bot]"
                                        },
                                        "body": "**P1 Badge** fixture blocker",
                                    }
                                ]
                            },
                        }
                    ]
                )
            )
        if failure == "wrong-pr":
            pr = dict(pr, url="https://github.com/owner/repo/pull/999")
        assert (
            closure(
                scene, pr, scene.x if failure == "old-head" else s, ok=False
            ).returncode
            != 0
        )
    report_wrapup(scene)
    result = cli(scene, "set", "wrap", "completed", ok=False)
    if _workspace_delivery(scene):
        assert result.returncode == 0
        assert scene.store.get_task("wrap")["status"] == "completed"
    else:
        assert result.returncode != 0
        assert scene.store.get_task("wrap")["status"] == "agent_done"


def test_review_fix_z_reenters_same_bounded_path_and_rejects_old_results(
    scene, controller, agents, forge
):
    y, s = add_output(scene)
    first = dispatch_verifiers(scene, controller, agents)
    for tid in first:
        certify(scene, tid)
    delivery(scene, first)
    old = publish(scene, "wrap")
    (scene.fakebin / "head.txt").write_text(s)
    closure(scene, old, s)
    z, final = add_output(scene, "Knowledge and review fix Z.")
    assert final != s
    second = dispatch_verifiers(scene, controller, agents)
    assert set(second).isdisjoint(first)
    for tid in first:
        assert scene.store.get_task(tid)["status"] == "superseded"
        assert (
            cli(
                scene, "set", tid, "completed", "--verdict", "pass", ok=False
            ).returncode
            != 0
        )
    assert closure(scene, old, s, ok=False).returncode != 0
    for tid in second:
        certify(scene, tid)
    delivery(scene, second)
    pr = publish(scene, "wrap")
    assert pr["candidate_sha"] == final and pr["replaces_pr_url"] == old["url"]
    (scene.fakebin / "head.txt").write_text(final)
    closure(scene, pr, final)
    report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")
    assert scene.store.get_task("wrap")["wrapup_delivery"]["round"] == 2


def test_freeze_interruption_and_external_publication_reply_loss_recover_once(
    scene, controller, agents, forge, monkeypatch
):
    from herdr import wrapup_delivery

    y, s = add_output(scene)
    retire = wrapup_delivery.reconcile_verifiers
    monkeypatch.setattr(
        wrapup_delivery,
        "reconcile_verifiers",
        lambda *a: (_ for _ in ()).throw(OSError("crash after freeze")),
    )
    controller.check_workflow_stage_advance("wf")
    assert scene.store.get_workflow("wf")["candidate_sha"] == s
    assert scene.store.get_task("old-test")["status"] == "cleaned"
    monkeypatch.setattr(wrapup_delivery, "reconcile_verifiers", retire)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        certify(scene, tid)
    delivery(scene, tasks)
    forge.fail_after_create = True
    with pytest.raises(RuntimeError, match="reply lost"):
        publish(scene, "wrap")
    assert not scene.store.list_events(
        workflow_id="wf", event_type="pull_request_created"
    )
    pr = publish(scene, "wrap")
    assert publish(scene, "wrap") == pr
    assert forge.creates == 1
    assert (
        len(scene.store.list_events(workflow_id="wf", event_type="candidate_frozen"))
        == 2
    )
    assert (
        len(
            scene.store.list_events(workflow_id="wf", event_type="pull_request_created")
        )
        == 1
    )
    (scene.fakebin / "head.txt").write_text(s)
    closure(scene, pr, s)
    report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")


def test_rebased_candidate_certifies_final_s_including_y(
    scene, controller, agents, forge
):
    # Disjoint edits in the same file must survive normal rebase together.
    source = (scene.clone / "code.py").read_text()
    (scene.clone / "code.py").write_text(
        source.replace("wrapup_note = 'X'", "wrapup_note = 'Y'")
    )
    (scene.repo / "code.py").write_text(
        source.replace("answer = 42", "answer = 42  # main advance")
    )
    (scene.repo / "base-update.txt").write_text("New main content\n")
    git(scene.repo, "add", ".")
    git(scene.repo, "commit", "-m", "advance main independently")
    git(scene.repo, "push", "origin", "main")
    y, s = add_output(scene)
    assert y != s
    assert (
        git(scene.clone, "show", "HEAD:knowledge.md")
        == "Knowledge collected during wrapup."
    )
    assert git(scene.clone, "show", "HEAD:base-update.txt") == "New main content"
    content = git(scene.clone, "show", "HEAD:code.py")
    assert "answer = 42  # main advance" in content
    assert "wrapup_note = 'Y'" in content
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        certify(scene, tid)
    delivery(scene, tasks)
    pr = publish(scene, "wrap")
    (scene.fakebin / "head.txt").write_text(s)
    closure(scene, pr, s)
    report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")
    assert scene.store.get_task("wrap")["wrapup_delivery"]["source_sha"] == y
    assert pr["candidate_sha"] == s


def test_integrated_before_publication_interruption_replays_without_new_commit(scene):
    (scene.clone / "knowledge.md").write_text("durable source Y\n")
    cli(scene, "commit", "wrap")
    source = git(scene.clone, "rev-parse", "HEAD")
    program = """import runpy, sys
from herdr import scheduler_facts
def interrupted(*args, **kwargs):
    raise ValueError('simulated process interruption before publication')
scheduler_facts.publish_integrated_candidate = interrupted
sys.argv = [sys.argv[1], 'integrate', 'wrap']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(ROOT / "bin/herdr-task")],
        cwd=ROOT,
        env=scene.env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert scene.store.get_task("wrap")["wrapup_delivery"]["phase"] == "integrated"
    assert not scene.store.list_events(
        workflow_id="wf", event_type="candidate_published"
    )
    cli(scene, "commit", "wrap")
    cli(scene, "integrate", "wrap")
    cli(scene, "integrate", "wrap")
    assert git(scene.clone, "rev-parse", "HEAD") == source
    assert (
        len(scene.store.list_events(workflow_id="wf", event_type="candidate_published"))
        == 1
    )


@pytest.mark.parametrize(
    "field,value",
    [("execution_id", "foreign-execution"), ("workflow_id", "foreign-workflow")],
)
def test_wrong_execution_or_workflow_cannot_register_wrapup_output(scene, field, value):
    if field == "workflow_id":
        with pytest.raises(ValueError, match="protected task fields"):
            scene.store.update_task_metadata("wrap", {field: value})
        return
    scene.store.update_task_metadata("wrap", {field: value})
    (scene.clone / "knowledge.md").write_text("must not register\n")
    before = git(scene.clone, "rev-parse", "HEAD")
    result = cli(scene, "commit", "wrap", ok=False)
    assert result.returncode != 0
    assert git(scene.clone, "rev-parse", "HEAD") == before
    assert not scene.store.get_task("wrap").get("wrapup_delivery")
    assert not scene.store.list_events(event_type="candidate_published")


def test_both_default_templates_expose_actual_platform_reentry_commands(scene):
    from herdr.workflow import normalize_workflow

    normalized = normalize_workflow(scene.cfg)
    wrapup = next(n for n in normalized["nodes"] if n["id"] == "wrapup")
    assert wrapup["default_integration_mode"] == "none"
    assert wrapup["review_closure_required"] is True
    rules = "\n".join(wrapup["rules"])
    for command in (
        "herdr-task commit",
        "herdr-task integrate",
        "record-delivery",
        "herdr-task create-pr",
        "checkpoint-publish",
    ):
        assert command in rules


def test_restart_after_one_verifier_delivery_does_not_duplicate_that_branch(
    scene, controller, agents, monkeypatch
):
    from herdr import node_dispatch_store

    add_output(scene)
    controller.check_workflow_stage_advance("wf")
    first = controller.coordinator_queue.get_nowait()
    controller._handle_stage_advance_item(first)
    assert len(agents) == 1
    # Lose the queued sibling event while its durable reservation has no send.
    lost = controller.coordinator_queue.get_nowait()
    operation = node_dispatch_store.operation_for_node(
        scene.store.db_path, "wf", lost["node_id"]
    )
    recovered_at = operation["lease_until"] + 1
    monkeypatch.setattr(time, "time", lambda: recovered_at)
    node_dispatch_store.reconcile_workflow(scene.store.db_path, "wf", now=recovered_at)
    controller.check_workflow_stage_advance("wf")
    while not controller.coordinator_queue.empty():
        controller._handle_stage_advance_item(controller.coordinator_queue.get_nowait())
    assert len(agents) == 2
    assert len(set(agents)) == 2
    assert {scene.store.get_task(t)["node"] for t in agents} == {"test", "review"}


def test_old_publication_response_cannot_record_after_wrapup_advances(
    scene, controller, agents, forge, monkeypatch
):
    add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        certify(scene, tid)
    delivery(scene, tasks)
    create = forge.create

    def advance_after_remote_effect(*args, **kwargs):
        row = create(*args, **kwargs)
        add_output(scene, "Z produced while publication response is in flight.")
        return row

    monkeypatch.setattr(forge, "create", advance_after_remote_effect)
    with pytest.raises(ValueError, match="changed during publication"):
        publish(scene, "wrap")
    assert not scene.store.list_events(
        workflow_id="wf", event_type="pull_request_created"
    )
    assert forge.creates == 1


@pytest.mark.parametrize("acceptance_state", ["missing", "blocked"])
def test_spec_publication_requires_new_acceptance(
    scene, controller, agents, forge, acceptance_state
):
    if scene.cfg["name"] != "spec-development-v1":
        pytest.skip("SPEC has the acceptance join")
    add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        certify(scene, tid)
    delivery(scene, tasks, acceptance=False)
    if acceptance_state == "blocked":
        accept_join(scene, verdict="blocked")
    with pytest.raises(ValueError, match="acceptance"):
        publish(scene, "wrap")
    if acceptance_state == "blocked":
        return
    accept_join(scene)
    assert (
        publish(scene, "wrap")["candidate_sha"]
        == scene.store.get_workflow("wf")["candidate_sha"]
    )


def test_wrapup_integrate_rejects_unregistered_head_rollback(scene):
    (scene.clone / "knowledge.md").write_text("registered output Y\n")
    cli(scene, "commit", "wrap")
    git(scene.clone, "reset", "--hard", scene.x)
    result = cli(scene, "integrate", "wrap", ok=False)
    assert result.returncode != 0
    assert "HEAD differs from registered commit" in result.stdout + result.stderr
    assert scene.store.get_task("wrap")["wrapup_delivery"]["phase"] == "committed"


def test_rebase_accepts_equivalent_x_on_independent_main(scene):
    # Main independently incorporates X's patch under a different commit.
    source = (scene.clone / "code.py").read_text()
    git(scene.repo, "reset", "--hard", scene.x + "^")
    (scene.repo / "code.py").write_text(source)
    (scene.repo / "main.txt").write_text("independent main work\n")
    git(scene.repo, "add", ".")
    git(scene.repo, "commit", "-m", "independently incorporate implementation")
    git(scene.repo, "push", "--force", "origin", "main")
    (scene.clone / "code.py").write_text(
        source.replace("wrapup_note = 'X'", "wrapup_note = 'Y'")
    )
    y, s = add_output(scene)
    assert y != s
    assert "wrapup_note = 'Y'" in git(scene.clone, "show", "HEAD:code.py")
    assert git(scene.clone, "show", "HEAD:main.txt") == "independent main work"
    task = scene.store.get_task("wrap")
    assert task["integration_head_before_rebase"] == y
    assert task["integration_base_sha"] == git(scene.repo, "rev-parse", "HEAD")


def test_dirty_output_after_receipt_cannot_complete(scene, controller, agents, forge):
    _, s = add_output(scene)
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        certify(scene, tid)
    delivery(scene, tasks)
    pr = publish(scene, "wrap")
    (scene.fakebin / "head.txt").write_text(s)
    closure(scene, pr, s)
    (scene.clone / "knowledge.md").write_text("unregistered output after receipt\n")
    report_wrapup(scene)
    result = cli(scene, "set", "wrap", "completed", ok=False)
    if _workspace_delivery(scene):
        assert result.returncode == 0
        assert scene.store.get_task("wrap")["status"] == "completed"
    else:
        assert result.returncode != 0
        assert "worktree" in result.stdout + result.stderr
        assert scene.store.get_task("wrap")["status"] != "completed"


def interrupt_after_rebase(scene):
    (scene.clone / "knowledge.md").write_text("registered Y survives interruption\n")
    cli(scene, "commit", "wrap")
    y = scene.store.get_task("wrap")["commit"]
    (scene.repo / "main.txt").write_text("main before interruption\n")
    git(scene.repo, "add", ".")
    git(scene.repo, "commit", "-m", "advance main before integration")
    git(scene.repo, "push", "origin", "main")
    program = """import runpy, sys
from herdr import kernel
original = kernel.update_task_metadata
def interrupted(task_id, updates, **kwargs):
    if 'integration_head_before_rebase' in updates and 'integrated_commit' not in updates:
        raise ValueError('interrupted after rebase before S persistence')
    return original(task_id, updates, **kwargs)
kernel.update_task_metadata = interrupted
sys.argv = [sys.argv[1], 'integrate', 'wrap']
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(ROOT / "bin/herdr-task")],
        cwd=ROOT,
        env=scene.env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "interrupted after rebase" in result.stderr
    assert scene.store.get_task("wrap")["commit"] == y
    s = git(scene.clone, "rev-parse", "HEAD")
    assert s != y
    return y, s


@pytest.mark.parametrize("restart_entry", ["integrate", "commit"])
def test_rebase_before_persistence_recovers_to_delivery(
    scene, controller, agents, forge, restart_entry
):
    y, interrupted_s = interrupt_after_rebase(scene)
    if restart_entry == "commit":
        # Recovery must verify against the pinned old base before consuming a newer main.
        (scene.repo / "main-later.txt").write_text("later main advance\n")
        git(scene.repo, "add", ".")
        git(scene.repo, "commit", "-m", "advance main during interruption")
        git(scene.repo, "push", "origin", "main")
    cli(scene, restart_entry, "wrap")
    cli(scene, "commit", "wrap")
    cli(scene, "integrate", "wrap")
    cli(scene, "integrate", "wrap")
    task = scene.store.get_task("wrap")
    s = task["integrated_commit"]
    assert task["wrapup_delivery"]["source_sha"] == y
    assert task["wrapup_delivery"]["round"] == 1
    assert (
        git(scene.clone, "show", "HEAD:knowledge.md")
        == "registered Y survives interruption"
    )
    assert git(scene.clone, "show", "HEAD:main.txt") == "main before interruption"
    if restart_entry == "commit":
        assert git(scene.clone, "show", "HEAD:main-later.txt") == "later main advance"
    else:
        assert s == interrupted_s
    tasks = dispatch_verifiers(scene, controller, agents)
    for tid in tasks:
        certify(scene, tid)
    delivery(scene, tasks)
    pr = publish(scene, "wrap")
    (scene.fakebin / "head.txt").write_text(s)
    closure(scene, pr, s)
    report_wrapup(scene)
    cli(scene, "set", "wrap", "completed")
    assert scene.store.get_task("wrap")["status"] == "completed"
    assert (
        len(scene.store.list_events(workflow_id="wf", event_type="candidate_published"))
        == 1
    )
    assert (
        len(
            scene.store.list_events(workflow_id="wf", event_type="pull_request_created")
        )
        == 1
    )


def test_rebase_recovery_rejects_unregistered_result(scene):
    interrupt_after_rebase(scene)
    (scene.clone / "knowledge.md").write_text("unregistered replacement\n")
    git(scene.clone, "add", ".")
    git(scene.clone, "commit", "-m", "unregistered replacement after crash")
    for entry in ("integrate", "commit"):
        result = cli(scene, entry, "wrap", ok=False)
        assert result.returncode != 0
    assert scene.store.get_task("wrap")["wrapup_delivery"]["phase"] == "committed"


@pytest.mark.parametrize("requested", ["auto", "wrap-agent"])
@pytest.mark.parametrize("independent_available", [False, True])
@pytest.mark.parametrize("opt_out", [False, True])
@pytest.mark.parametrize("legacy_pin", [False, True], ids=["current-pin", "legacy-pin"])
def test_wrapup_verifier_router_refuses_degradation_before_launch(
    scene,
    controller,
    monkeypatch,
    requested,
    independent_available,
    opt_out,
    legacy_pin,
):
    from herdr import agent_router as router

    add_output(scene)
    controller.check_workflow_stage_advance("wf")
    pool = ["wrap-agent"] + (["independent-agent"] if independent_available else [])
    record = dict(
        scene.store.get_workflow("wf"),
        project_id="fixture-project",
        healthy_agents=pool,
    )
    import copy

    config = copy.deepcopy(scene.cfg)
    if legacy_pin:
        for node in config["nodes"]:
            if node["id"] in {"test", "review"}:
                node["agent_policy"].pop("exclude_stage_agents", None)
    if opt_out:
        for node in config["nodes"]:
            if node["id"] in {"test", "review"}:
                node["agent_policy"].update(
                    allow_reuse_implementation_agents=True,
                    reuse_reason="must not waive fresh wrapup independence",
                )
    monkeypatch.setattr(router, "_get_store", lambda: scene.store)
    monkeypatch.setattr(router, "workflow_record", lambda _: record)
    monkeypatch.setattr(router, "workflow_config_for", lambda _: config)
    monkeypatch.setattr(
        router, "ensure_pool_for_project", lambda _: {"allowed_agents": pool}
    )
    monkeypatch.setattr(router, "_candidate_order", lambda *args: pool)
    monkeypatch.setattr(router, "preflight_snapshot_fresh", lambda _: True)
    monkeypatch.setattr(router, "_active_agent_loads", lambda _: {})
    monkeypatch.setattr(router, "_load_canary_module", lambda: None)
    monkeypatch.setattr(router, "_record_shadow_best_effort", lambda *args: None)
    monkeypatch.setattr(
        router, "RESERVATIONS_FILE", scene.root / "agent-reservations.json"
    )
    monkeypatch.setattr(router, "ROUTER_LOCK_FILE", scene.root / "agent-router.lock")
    monkeypatch.setenv("HERDR_ROUTER_SOFT_DEGRADE", "1")
    for node in ("test", "review"):
        if independent_available and requested == "auto":
            selected, context = router._choose_agent_impl(
                "wf", node, "test", requested=requested
            )
            assert selected == "independent-agent"
            assert context["wrapup_candidate_sha"] == record["candidate_sha"]
        else:
            with pytest.raises(router.RouterIsolationRejection):
                router._choose_agent_impl("wf", node, "test", requested=requested)
    assert not scene.store.list_events(
        workflow_id="wf", event_type="router_isolation_degraded"
    )
