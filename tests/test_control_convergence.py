"""Cross-boundary regressions for candidate assembly and dispatch recovery."""

import json
import pytest
import os
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path
from tests.test_node_dispatch_contract import scene as scene
from tests.test_dispatch_idempotency import (
    launch_transport_scene as launch_transport_scene,
)
from tests.test_downstream_dispatch_contract import (
    direct_scene,
    downstream,
    op_for,
    SHA,
)
from herdr import node_dispatch_store as nd, recovery_store
from herdr.task_resources import workflow_launch_lock

REAL_RUN = subprocess.run


def test_real_cli_busy_is_proven_pre_entry_rejection(scene, tmp_path):
    root = Path(__file__).resolve().parents[1]
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = nd.operation_for_node(scene.store.db_path, "wf", "implementation")
    nd.claim(scene.store.db_path, op["id"], "owner", now=1000)
    nd.start(
        scene.store.db_path,
        op["id"],
        "owner",
        now=1000,
        expected_task_ids=["busy-proof"],
    )
    with workflow_launch_lock(scene.store.db_path, "wf"):
        child = subprocess.Popen(
            [
                sys.executable,
                str(root / "bin/herdr-task"),
                "launch",
                "--task-id",
                "busy-proof",
                "--workflow-id",
                "wf",
                "--node",
                "implementation",
                "--source",
                str(tmp_path),
                "--agent",
                "auto",
                "--task-type",
                "feat",
                "--goal",
                "diagnostic no execution",
                "--prompt",
                "must not execute",
                "--dispatch-operation-id",
                str(op["id"]),
                "--dispatch-attempt",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        stdout, stderr = child.communicate(timeout=35)
    assert child.returncode == 75
    assert "[LAUNCH BUSY]" in stdout
    assert scene.store.get_task("busy-proof") is None
    with sqlite3.connect(scene.store.db_path) as c:
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE event_type='launch_intent'"
            ).fetchone()[0]
            == 0
        )
        receipt = json.loads(
            c.execute(
                "SELECT payload_json FROM events WHERE event_type='launch_rejected'"
            ).fetchone()[0]
        )
        assert receipt["resources_absent"] is True
        assert receipt["dispatch_attempt"] == 1
    result = nd.direct_finished(
        scene.store.db_path, op["id"], "owner", ["busy-proof"], now=1001
    )
    assert result["status"] == "pending" and (not result["started"])


def test_controller_known_busy_should_retain_bounded_retry(scene, monkeypatch):
    item = direct_scene(scene, monkeypatch)

    def external(command, **kwargs):
        arg = lambda name: command[command.index(name) + 1]
        scene.store.record_event(
            "launch_rejected",
            {
                "dispatch_operation_id": int(arg("--dispatch-operation-id")),
                "dispatch_attempt": int(arg("--dispatch-attempt")),
                "task_id": arg("--task-id"),
                "resources_absent": True,
            },
            workflow_id="wf",
            task_id=arg("--task-id"),
            source="launch",
        )
        return SimpleNamespace(returncode=75, stdout="[LAUNCH BUSY]", stderr="")

    monkeypatch.setattr(scene.ctrl.subprocess, "run", external)
    scene.ctrl._handle_coordinator_item(item)
    op = op_for(scene)
    assert op["status"] == "pending" and (not op["started"]), (
        "known pre-entry busy became unknown delivery"
    )


def test_claim_of_unchanged_wait_should_not_write_again(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "owner", now=1000)
    nd.start(scene.store.db_path, op["id"], "owner", now=1000)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1900)
    before = op_for(scene)
    for _ in range(100):
        assert nd.claim(scene.store.db_path, op["id"], "new-owner", now=1901) is None
    after = op_for(scene)
    assert before["version"] == after["version"], (
        "claim writes identical waiting event each sweep"
    )


@pytest.mark.parametrize(
    "completed_status",
    ["completed", "committed", "integrated", "cleanup_ready", "cleaned"],
)
def test_waiting_unstarted_operation_should_adopt_matching_completed_manual_task(
    scene, completed_status
):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    with recovery_store._transaction(scene.store.db_path) as c:
        nd._write(
            c,
            op,
            "waiting_human",
            {"reason": "dispatch_prior_delivery_unknown"},
            1000,
            started=0,
        )
    scene.store.save_task(
        {
            "task_id": "manual-test",
            "workflow_id": "wf",
            "node": "test",
            "execution_id": "execution-1",
            "run_id": "manual-run",
            "status": completed_status,
            "candidate_sha": SHA,
            "verified_candidate_sha": SHA,
            "stage_verdict": "pass",
            "candidate_episode_id": op["payload"]["candidate_episode_id"],
        }
    )
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1010)
    result = op_for(scene)
    assert result["status"] in ("resolved", "superseded"), (
        "unstarted human-wait is excluded from existing task adoption"
    )


def test_candidate_rotation_rejection_should_close_old_intent(
    scene, monkeypatch, tmp_path
):
    import importlib.util
    import importlib.machinery
    from herdr.task_resources import begin_launch_intent

    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "notes"))
    item = direct_scene(scene, monkeypatch)
    loader = importlib.machinery.SourceFileLoader(
        "diagnostic_cli", str(Path(__file__).resolve().parents[1] / "bin/herdr-task")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    monkeypatch.setattr(cli, "_get_store", lambda: scene.store)

    def external(command, **kwargs):
        arg = lambda name: command[command.index(name) + 1]

        def entered(args):
            args._launch_intent = begin_launch_intent(
                scene.store,
                workflow_id="wf",
                node_id="test",
                task_id=args.task_id,
                candidate_sha=SHA,
                dispatch_operation_id=args.dispatch_operation_id,
                execution_id="execution-1",
                run_id="old-run",
                now=1000,
            )["intent"]
            scene.store.record_event(
                "candidate_frozen",
                {"candidate_sha": "b" * 40},
                workflow_id="wf",
                source="critical-path-scheduler",
                timestamp=1001,
            )
            cli._preflight_delivery_identity("wf", "test", args.task_id, claim=SHA)

        monkeypatch.setattr(cli, "_launch_task", entered)
        try:
            cli.launch_task(
                SimpleNamespace(
                    workflow_id="wf",
                    node="test",
                    task_id=arg("--task-id"),
                    dispatch_operation_id=int(arg("--dispatch-operation-id")),
                    dispatch_attempt=int(arg("--dispatch-attempt")),
                    source=None,
                )
            )
        except SystemExit as exc:
            return SimpleNamespace(
                returncode=exc.code,
                stdout="baseline rejected after candidate rotation",
                stderr="",
            )
        raise AssertionError("old candidate unexpectedly accepted")

    monkeypatch.setattr(scene.ctrl.subprocess, "run", external)
    scene.ctrl._handle_coordinator_item(item)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1002)
    current = op_for(scene)
    with sqlite3.connect(scene.store.db_path) as c:
        phase = json.loads(
            c.execute(
                "SELECT payload_json FROM events WHERE event_type='launch_intent' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )["phase"]
    assert phase == "resources_absent" and current["status"] == "pending", (
        "rejected old candidate leaves allocating intent and blocks new episode"
    )


@pytest.mark.parametrize(
    "linear,conflict", [(False, False), (True, False), (False, True)]
)
def test_parallel_implementation_freeze_should_cover_all_deliveries(
    scene, monkeypatch, tmp_path, linear, conflict
):
    from herdr.scheduler_facts import publish_integrated_candidate
    from herdr.scheduler import node_is_complete

    monkeypatch.setattr(scene.ctrl.subprocess, "run", REAL_RUN)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return REAL_RUN(
            ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Diagnostic")
    git("config", "user.email", "diagnostic@invalid.test")
    git("config", "commit.gpgsign", "false")
    (repo / "base").write_text("base")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    commits = {}
    for name in ["collector", "hub"]:
        git(
            "checkout",
            "-B",
            name,
            (commits.get("collector") if linear else None) or base,
        )
        (repo / ("shared" if conflict else name)).write_text(name)
        git("add", ".")
        git("commit", "-m", name)
        commits[name] = git("rev-parse", "HEAD")
        scene.store.save_task(
            {
                "task_id": name,
                "workflow_id": "wf",
                "node": "implementation",
                "execution_id": "execution-1",
                "run_id": "run-" + name,
                "status": "cleaned",
                "integration_mode": "git",
                "commit": commits[name],
                "integrated_commit": commits[name],
                "integration_ref": "refs/herdr/tasks/" + name,
                "integration_publication_episode_id": 0,
            }
        )
        git("update-ref", "refs/herdr/tasks/" + name, commits[name])
        publish_integrated_candidate(scene.store, name)
    assert node_is_complete(scene.store.list_tasks(workflow_id="wf"), None)
    (repo / "untracked-work").write_text("user work")
    before = (git("rev-parse", "HEAD"), git("status", "--porcelain"))
    frozen = scene.ctrl._scheduler_freeze_candidate(
        "wf", str(repo), "implementation", ["implementation"]
    )
    assert before == (git("rev-parse", "HEAD"), git("status", "--porcelain"))
    if conflict:
        assert frozen == ""
        with sqlite3.connect(scene.store.db_path) as conn:
            assert not conn.execute(
                "SELECT 1 FROM events WHERE event_type='candidate_frozen'"
            ).fetchone()
        return
    files = git("ls-tree", "--name-only", frozen).splitlines()
    assert {"hub", "collector"}.issubset(files), (
        "latest individual task publication frozen as whole workflow candidate"
    )
    assert (
        scene.ctrl._scheduler_freeze_candidate(
            "wf", str(repo), "implementation", ["implementation"]
        )
        == frozen
    )
    with sqlite3.connect(scene.store.db_path) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE event_type='candidate_frozen'"
        ).fetchall()
        assert len(rows) == 1
        assert len(json.loads(rows[0][0])["delivery_inventory"]) == 2


@pytest.mark.parametrize("attempt", [None, 0, 2])
def test_missing_or_stale_absence_receipt_never_authorizes_retry(
    scene, monkeypatch, attempt
):
    item = direct_scene(scene, monkeypatch)

    def external(command, **kwargs):
        if attempt is not None:
            scene.store.record_event(
                "launch_rejected",
                {
                    "dispatch_operation_id": op_for(scene)["id"],
                    "dispatch_attempt": attempt,
                    "task_id": "wf-test-auto",
                    "resources_absent": True,
                },
                workflow_id="wf",
                source="launch",
            )
        return SimpleNamespace(returncode=75, stdout="[LAUNCH BUSY]", stderr="")

    monkeypatch.setattr(scene.ctrl.subprocess, "run", external)
    scene.ctrl._handle_coordinator_item(item)
    assert op_for(scene)["status"] == "awaiting_result"
    assert op_for(scene)["started"]


@pytest.mark.parametrize(
    "change",
    [
        {"execution_id": "foreign"},
        {"candidate_sha": "b" * 40},
        {"candidate_episode_id": 99999},
        {"verified_candidate_sha": "b" * 40},
        {"stage_verdict": "blocked"},
        {"status": "working"},
    ],
)
def test_waiting_prior_requires_current_completed_verification(scene, change):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    with recovery_store._transaction(scene.store.db_path) as conn:
        nd._write(
            conn,
            op,
            "waiting_human",
            {"reason": "dispatch_prior_delivery_unknown"},
            1000,
        )
    scene.store.save_task(
        dict(
            task_id="manual",
            workflow_id="wf",
            node="test",
            execution_id="execution-1",
            run_id="manual-run",
            status="cleaned",
            stage_verdict="pass",
            candidate_sha=SHA,
            verified_candidate_sha=SHA,
            candidate_episode_id=op["payload"]["candidate_episode_id"],
        )
        | change
    )
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1001)
    assert op_for(scene)["status"] == "waiting_human"


def test_partial_allocation_failure_never_emits_absence(scene, monkeypatch):
    import importlib.machinery
    import importlib.util
    from herdr.task_resources import begin_launch_intent, record_launch_resources

    loader = importlib.machinery.SourceFileLoader(
        "partial_cli", str(Path(__file__).resolve().parents[1] / "bin/herdr-task")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    monkeypatch.setattr(cli, "_get_store", lambda: scene.store)
    args = SimpleNamespace(
        workflow_id="wf",
        node="implementation",
        task_id="partial",
        dispatch_operation_id=None,
        source=None,
    )

    def entered(args):
        intent = begin_launch_intent(
            scene.store,
            workflow_id="wf",
            node_id="implementation",
            task_id="partial",
            run_id="partial-run",
        )["intent"]
        args._launch_intent = record_launch_resources(
            scene.store, intent, {"pane_id": "owned-pane"}
        )
        args._launch_resources_started = True
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_launch_task", entered)
    with pytest.raises(SystemExit):
        cli.launch_task(args)
    with sqlite3.connect(scene.store.db_path) as conn:
        phase = json.loads(
            conn.execute(
                "SELECT payload_json FROM events WHERE event_type='launch_intent' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )["phase"]
        assert phase == "allocating"
        assert not conn.execute(
            "SELECT 1 FROM events WHERE event_type='launch_rejected'"
        ).fetchone()


def test_previous_attempts_absence_cannot_certify_new_unknown_attempt(scene):
    from herdr.task_resources import begin_launch_intent, abort_launch_intent

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "first", now=1000)
    nd.start(scene.store.db_path, op["id"], "first", now=1000)
    intent = begin_launch_intent(
        scene.store,
        workflow_id="wf",
        node_id="test",
        task_id="test",
        candidate_sha=SHA,
        dispatch_operation_id=op["id"],
        execution_id="execution-1",
        run_id="test-run",
        now=1000,
    )["intent"]
    abort_launch_intent(scene.store, intent, now=1001)
    nd.direct_finished(scene.store.db_path, op["id"], "first", ["test"], now=1001)
    nd.claim(scene.store.db_path, op["id"], "second", now=1031)
    nd.start(scene.store.db_path, op["id"], "second", now=1031)
    result = nd.direct_finished(
        scene.store.db_path, op["id"], "second", ["test"], now=1032
    )
    assert result["status"] == "awaiting_result"


def test_assembly_publication_rechecks_exact_delivery_runs(scene):
    from herdr.candidate_assembly import delivery_inventory
    from herdr.scheduler_facts import record_candidate_frozen

    wf = scene.store.get_workflow("wf")
    for name in ["one", "two"]:
        scene.store.save_task(
            dict(
                task_id=name,
                workflow_id="wf",
                node="implementation",
                execution_id="execution-1",
                run_id="run-" + name,
                status="cleaned",
                integration_mode="git",
                commit=SHA,
                integrated_commit=SHA,
                integration_ref="refs/herdr/tasks/" + name,
            )
        )
    inventory = delivery_inventory(
        wf, scene.store.list_tasks(workflow_id="wf"), "implementation"
    )
    task = scene.store.get_task("one")
    task["run_id"] = "replacement-run"
    scene.store.save_task(task)
    with pytest.raises(ValueError, match="inventory changed"):
        record_candidate_frozen(
            "wf",
            SHA,
            source_node="implementation",
            db_path=scene.store.db_path,
            expected_episode_id=0,
            delivery_inventory=inventory,
        )
    assert (
        recovery_store.read_snapshot(scene.store.db_path, "wf")[0]["candidate_sha"]
        is None
    )


def test_receipt_survives_controller_restart(scene):
    from tests.test_downstream_dispatch_contract import downstream, op_for
    from herdr import node_dispatch_store as nd

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "dead-owner", now=1000)
    started = nd.start(
        scene.store.db_path,
        op["id"],
        "dead-owner",
        now=1000,
        expected_task_ids=["test"],
    )
    scene.store.record_event(
        "launch_rejected",
        dict(
            dispatch_operation_id=op["id"],
            dispatch_attempt=started["attempts"],
            task_id="test",
            resources_absent=True,
        ),
        workflow_id="wf",
        task_id="test",
        source="launch",
        timestamp=1001,
    )
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1700)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1900)
    after = op_for(scene)
    assert after["status"] == "pending" and (not after["started"])


def test_durable_absence_releases_new_episode_after_restart(scene):
    from tests.test_downstream_dispatch_contract import downstream, op_for

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    old = op_for(scene)
    nd.claim(scene.store.db_path, old["id"], "dead", now=1000)
    nd.start(
        scene.store.db_path, old["id"], "dead", now=1000, expected_task_ids=["test"]
    )
    scene.store.record_event(
        "candidate_frozen",
        {"candidate_sha": "b" * 40},
        workflow_id="wf",
        source="critical-path-scheduler",
        timestamp=1001,
    )
    scene.store.record_event(
        "launch_rejected",
        dict(
            dispatch_operation_id=old["id"],
            dispatch_attempt=1,
            task_id="test",
            resources_absent=True,
        ),
        workflow_id="wf",
        source="launch",
        timestamp=1002,
    )
    for now in [1003, 1004]:
        nd.reconcile_workflow(scene.store.db_path, "wf", now=now)
    current = op_for(scene)
    assert current["id"] != old["id"]
    assert current["status"] == "pending" and (not current["started"])
    assert nd.claim(scene.store.db_path, current["id"], "live", now=1004)


def test_proven_busy_then_registration_makes_forward_progress_once(scene, monkeypatch):
    from herdr.task_resources import begin_launch_intent

    item = direct_scene(scene, monkeypatch)
    calls = []

    def external(command, **kwargs):
        arg = lambda name: command[command.index(name) + 1]
        calls.append(command)
        op_id = int(arg("--dispatch-operation-id"))
        if len(calls) == 1:
            scene.store.record_event(
                "launch_rejected",
                dict(
                    dispatch_operation_id=op_id,
                    dispatch_attempt=1,
                    task_id=arg("--task-id"),
                    resources_absent=True,
                ),
                workflow_id="wf",
                source="launch",
            )
            return SimpleNamespace(returncode=75, stdout="busy", stderr="")
        intent = begin_launch_intent(
            scene.store,
            workflow_id="wf",
            node_id="test",
            task_id=arg("--task-id"),
            candidate_sha=SHA,
            dispatch_operation_id=op_id,
            dispatch_attempt=int(arg("--dispatch-attempt")),
            execution_id="execution-1",
            run_id="only-run",
        )["intent"]
        scene.store.save_task(
            dict(
                task_id=arg("--task-id"),
                workflow_id="wf",
                node="test",
                execution_id="execution-1",
                run_id="only-run",
                status="working",
                candidate_sha=SHA,
                dispatch_operation_id=op_id,
                launch_intent_id=intent["intent_id"],
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scene.ctrl.subprocess, "run", external)
    scene.ctrl._handle_coordinator_item(item)
    assert op_for(scene)["status"] == "pending"
    scene.clock[0] += 30
    scene.ctrl._handle_coordinator_item(item)
    assert op_for(scene)["status"] == "resolved"
    scene.ctrl._handle_coordinator_item(item)
    assert len(calls) == 2
    assert (
        len(
            [
                t
                for t in scene.store.list_tasks(workflow_id="wf")
                if t.get("node") == "test"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("nonlinear", [False, True])
def test_explicit_candidate_is_not_rotated_by_stale_task_publications(
    scene, monkeypatch, tmp_path, nonlinear
):
    from herdr import scheduler_facts

    monkeypatch.setattr(scene.ctrl.subprocess, "run", REAL_RUN)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return REAL_RUN(
            ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Review")
    git("config", "user.email", "review@invalid.test")
    git("config", "commit.gpgsign", "false")
    (repo / "base").write_text("base")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    commits = []
    for name in ["a", "b"]:
        git("checkout", "-B", name, base)
        (repo / name).write_text(name)
        git("add", ".")
        git("commit", "-m", name)
        sha = git("rev-parse", "HEAD")
        scene.store.save_task(
            dict(
                task_id=name,
                workflow_id="wf",
                node="implementation",
                execution_id="execution-1",
                run_id=name,
                status="cleaned",
                integration_mode="git",
                commit=sha,
                integrated_commit=sha,
                integration_ref="refs/herdr/tasks/" + name,
                integration_publication_episode_id=0,
            )
        )
        git("update-ref", "refs/herdr/tasks/" + name, sha)
        scheduler_facts.publish_integrated_candidate(scene.store, name)
        commits.append(sha)
    selected = base
    if nonlinear:
        git("checkout", "main")
        (repo / "new-base").write_text("upstream change")
        git("add", ".")
        git("commit", "-m", "new base")
        git("cherry-pick", *commits)
        selected = git("rev-parse", "HEAD")
    scheduler_facts.record_candidate_frozen("wf", selected, db_path=scene.store.db_path)
    before = recovery_store.read_snapshot(scene.store.db_path, "wf")[0]
    frozen = scene.ctrl._scheduler_freeze_candidate(
        "wf", str(repo), "implementation", ["implementation"]
    )
    after = recovery_store.read_snapshot(scene.store.db_path, "wf")[0]
    assert after["candidate_sha"] == selected
    assert after["candidate_episode_id"] == before["candidate_episode_id"]
    assert frozen == (selected if nonlinear else "")


def test_second_busy_after_first_intent_absent(scene):
    from tests.test_downstream_dispatch_contract import downstream, op_for, SHA
    from herdr import node_dispatch_store as nd
    from herdr.task_resources import begin_launch_intent, abort_launch_intent

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "one", now=1000)
    nd.start(scene.store.db_path, op["id"], "one", now=1000, expected_task_ids=["test"])
    intent = begin_launch_intent(
        scene.store,
        workflow_id="wf",
        node_id="test",
        task_id="test",
        candidate_sha=SHA,
        dispatch_operation_id=op["id"],
        execution_id="execution-1",
        run_id="run",
        now=1000,
    )["intent"]
    abort_launch_intent(scene.store, intent, now=1001)
    first = nd.direct_finished(scene.store.db_path, op["id"], "one", ["test"], now=1001)
    assert first["status"] == "pending"
    nd.claim(scene.store.db_path, op["id"], "two", now=1031)
    second = nd.start(
        scene.store.db_path, op["id"], "two", now=1031, expected_task_ids=["test"]
    )
    scene.store.record_event(
        "launch_rejected",
        dict(
            dispatch_operation_id=op["id"],
            dispatch_attempt=second["attempts"],
            task_id="test",
            resources_absent=True,
        ),
        workflow_id="wf",
        task_id="test",
        source="launch",
        timestamp=1032,
    )
    after = nd.direct_finished(scene.store.db_path, op["id"], "two", ["test"], now=1032)
    print("SECOND_BUSY", after["status"], after["detail"])
    assert after["status"] == "pending" and not after["started"]


def test_current_rejection_cannot_override_allocating_intent(scene):
    from herdr.task_resources import begin_launch_intent

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "owner", now=1000)
    nd.start(
        scene.store.db_path, op["id"], "owner", now=1000, expected_task_ids=["test"]
    )
    begin_launch_intent(
        scene.store,
        workflow_id="wf",
        node_id="test",
        task_id="test",
        candidate_sha=SHA,
        dispatch_operation_id=op["id"],
        execution_id="execution-1",
        run_id="run",
        now=1000,
    )
    scene.store.record_event(
        "launch_rejected",
        dict(
            dispatch_operation_id=op["id"],
            dispatch_attempt=1,
            task_id="test",
            resources_absent=True,
        ),
        workflow_id="wf",
        source="launch",
        timestamp=1001,
    )
    assert (
        nd.direct_finished(scene.store.db_path, op["id"], "owner", ["test"], now=1002)[
            "status"
        ]
        == "awaiting_result"
    )


def test_restart_requires_absence_for_entire_planned_batch(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "dead", now=1000)
    nd.start(
        scene.store.db_path,
        op["id"],
        "dead",
        now=1000,
        expected_task_ids=["one", "two"],
    )
    scene.store.record_event(
        "launch_rejected",
        dict(
            dispatch_operation_id=op["id"],
            dispatch_attempt=1,
            task_id="one",
            resources_absent=True,
        ),
        workflow_id="wf",
        source="launch",
        timestamp=1001,
    )
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1700)
    after = op_for(scene)
    assert after["started"] and after["attempts"] == 1
    assert after["status"] == "awaiting_result"


def test_preallocation_settlement_releases_only_owned_reservation(scene, monkeypatch):
    import importlib.machinery
    import importlib.util
    from herdr.task_resources import begin_launch_intent

    loader = importlib.machinery.SourceFileLoader(
        "reservation_cli", str(Path(__file__).resolve().parents[1] / "bin/herdr-task")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    monkeypatch.setattr(cli, "_get_store", lambda: scene.store)
    released = []
    monkeypatch.setattr(cli, "release_agent_reservation", released.append)

    def entered(args):
        args._launch_intent = begin_launch_intent(
            scene.store,
            workflow_id="wf",
            node_id="implementation",
            task_id=args.task_id,
            run_id="owned-run",
        )["intent"]
        args._launch_reservation_acquired = True
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_launch_task", entered)
    with pytest.raises(SystemExit):
        cli.launch_task(
            SimpleNamespace(
                workflow_id="wf", node="implementation", task_id="owned", source=None
            )
        )
    assert released == ["owned"]


@pytest.mark.parametrize("status", ["running", "awaiting_result", "waiting_human"])
def test_replayed_prior_attempt_cannot_write_launch_intent(scene, status):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "owner", now=1000)
    op = nd.start(scene.store.db_path, op["id"], "owner", now=1000)
    # Model a newer authorised attempt with the same operation/episode.
    with recovery_store._transaction(scene.store.db_path) as conn:
        nd._write(
            conn, op, status, {}, 1010, started=1, attempts=2, owner="owner", lease=1100
        )
    before = scene.store.list_events(workflow_id="wf", event_type="launch_intent")
    with pytest.raises(ValueError, match="attempt"):
        nd.record_intent(
            scene.store.db_path,
            dict(
                key="stale-key",
                intent_id="stale-intent",
                workflow_id="wf",
                node_id="test",
                task_id="stale",
                candidate_sha=SHA,
                dispatch_operation_id=op["id"],
                dispatch_attempt=1,
                execution_id="execution-1",
                resources={"run_id": "stale-run"},
                phase="allocating",
            ),
            1010,
        )
    assert (
        scene.store.list_events(workflow_id="wf", event_type="launch_intent") == before
    )
    assert scene.store.get_task("stale") is None


def test_old_attempt_cannot_register_or_update_resources(scene):
    from herdr.task_resources import begin_launch_intent, record_launch_resources

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "owner", now=1000)
    op = nd.start(scene.store.db_path, op["id"], "owner", now=1000)
    intent = begin_launch_intent(
        scene.store,
        workflow_id="wf",
        node_id="test",
        task_id="old",
        candidate_sha=SHA,
        dispatch_operation_id=op["id"],
        execution_id="execution-1",
        run_id="old-run",
        now=1000,
    )["intent"]
    with recovery_store._transaction(scene.store.db_path) as conn:
        nd._write(
            conn,
            op,
            "running",
            {},
            1010,
            started=1,
            attempts=2,
            owner="owner",
            lease=1100,
        )
    with pytest.raises(ValueError, match="attempt"):
        record_launch_resources(
            scene.store, intent, {"planned_clone_path": "/tmp/old"}, now=1010
        )
    with pytest.raises(ValueError, match="attempt"):
        scene.store.save_task(
            dict(
                task_id="old",
                workflow_id="wf",
                node="test",
                execution_id="execution-1",
                run_id="old-run",
                status="working",
                candidate_sha=SHA,
                dispatch_operation_id=op["id"],
                launch_intent_id=intent["intent_id"],
            )
        )
    assert scene.store.get_task("old") is None


@pytest.mark.parametrize("supplied", [1, None, 0, True, "2"])
def test_begin_launch_never_infers_retry_attempt(scene, supplied):
    from herdr.task_resources import begin_launch_intent

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "owner", now=1000)
    op = nd.start(scene.store.db_path, op["id"], "owner", now=1000)
    with recovery_store._transaction(scene.store.db_path) as conn:
        nd._write(
            conn,
            op,
            "running",
            {},
            1010,
            started=1,
            attempts=2,
            owner="owner",
            lease=1100,
        )
    with pytest.raises(ValueError, match="attempt"):
        begin_launch_intent(
            scene.store,
            workflow_id="wf",
            node_id="test",
            task_id="old",
            candidate_sha=SHA,
            dispatch_operation_id=op["id"],
            dispatch_attempt=supplied,
            execution_id="execution-1",
            run_id="old-run",
            now=1010,
        )
    intent = begin_launch_intent(
        scene.store,
        workflow_id="wf",
        node_id="test",
        task_id="current",
        candidate_sha=SHA,
        dispatch_operation_id=op["id"],
        dispatch_attempt=2,
        execution_id="execution-1",
        run_id="current-run",
        now=1010,
    )["intent"]
    assert intent["dispatch_attempt"] == 2
    scene.store.save_task(
        dict(
            task_id="current",
            workflow_id="wf",
            node="test",
            status="working",
            run_id="current-run",
            execution_id="execution-1",
            candidate_sha=SHA,
            dispatch_operation_id=op["id"],
            launch_intent_id=intent["intent_id"],
        )
    )
    assert scene.store.get_task("current")["run_id"] == "current-run"


def test_coordinator_prompt_preserves_attempt(scene):
    scene.ctrl._handle_coordinator_item(scene.item)
    op = op_for(scene, "implementation")
    prompt = " ".join(scene.sent[0])
    assert f"--dispatch-attempt {op['attempts']}" in prompt
    assert f'"{scene.ctrl.TASK_MANAGER}" launch' in prompt
    assert f'"{scene.ctrl.TASK_MANAGER}" list' in prompt
    assert "~/HAFlow/bin/herdr-task" not in prompt


@pytest.mark.parametrize("attempt", [1, 2])
def test_actual_cli_threads_attempt_before_router(
    launch_transport_scene, monkeypatch, attempt
):
    cli, store, args = launch_transport_scene
    wf = store.get_workflow("wf")
    wf["execution_id"] = "execution-1"
    store.save_workflow(wf)
    nd.reconcile_workflow(store.db_path, "wf")
    op = nd.operation_for_node(store.db_path, "wf", "n")
    nd.claim(store.db_path, op["id"], "owner")
    op = nd.start(store.db_path, op["id"], "owner")
    with recovery_store._transaction(store.db_path) as conn:
        nd._write(
            conn,
            op,
            "running",
            {},
            time.time(),
            started=1,
            attempts=2,
            owner="owner",
            lease=time.time() + 100,
        )
    args.dispatch_operation_id = op["id"]
    args.dispatch_attempt = attempt
    called = []

    class StopRouter(Exception):
        pass

    def router(*a, **kw):
        called.append(True)
        raise StopRouter()

    monkeypatch.setattr(cli, "choose_agent", router)
    if attempt == 1:
        with pytest.raises(SystemExit) as exc:
            cli.launch_task(args)
        assert exc.value.code == 2
        assert not called
        assert not store.list_events(event_type="launch_intent")
    else:
        with pytest.raises(StopRouter):
            cli.launch_task(args)
        assert called
        assert all(
            e["payload"]["dispatch_attempt"] == 2
            for e in store.list_events(event_type="launch_intent")
        )
    assert not store.list_tasks()


@pytest.mark.parametrize("current_attempt", [1, 2])
def test_legacy_intent_missing_attempt_only_authorizes_first_round(
    scene, current_attempt
):
    from herdr.task_resources import begin_launch_intent, record_launch_resources

    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op["id"], "owner", now=1000)
    op = nd.start(scene.store.db_path, op["id"], "owner", now=1000)
    intent = begin_launch_intent(
        scene.store,
        workflow_id="wf",
        node_id="test",
        task_id="legacy",
        candidate_sha=SHA,
        dispatch_operation_id=op["id"],
        execution_id="execution-1",
        run_id="legacy-run",
        now=1000,
    )["intent"]
    del intent["dispatch_attempt"]
    scene.store.record_event(
        "launch_intent",
        intent,
        workflow_id="wf",
        task_id=intent["key"],
        source="launch",
        timestamp=1001,
    )
    if current_attempt == 2:
        with recovery_store._transaction(scene.store.db_path) as conn:
            nd._write(
                conn,
                op,
                "running",
                {},
                1010,
                started=1,
                attempts=2,
                owner="owner",
                lease=1100,
            )
        with pytest.raises(ValueError, match="attempt"):
            record_launch_resources(
                scene.store, intent, {"planned_clone_path": "/tmp/legacy"}, now=1010
            )
    else:
        record_launch_resources(
            scene.store, intent, {"planned_clone_path": "/tmp/legacy"}, now=1010
        )
        latest = scene.store.list_events(
            task_id=intent["key"],
            event_type="launch_intent",
            source="launch",
            limit=1,
            desc=True,
        )[0]["payload"]
        assert latest["dispatch_attempt"] == 1
        scene.store.save_task(
            dict(
                task_id="legacy",
                workflow_id="wf",
                node="test",
                status="working",
                run_id="legacy-run",
                execution_id="execution-1",
                candidate_sha=SHA,
                dispatch_operation_id=op["id"],
                launch_intent_id=intent["intent_id"],
            )
        )
        assert scene.store.get_task("legacy")["run_id"] == "legacy-run"


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_unclaimed_explicit_replacement_only_authorizes_first_attempt(scene, attempt):
    from herdr.task_resources import begin_launch_intent

    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene, "implementation")
    scene.store.save_task(
        dict(
            task_id="old",
            workflow_id="wf",
            node="implementation",
            execution_id="execution-1",
            run_id="old-run",
            status="superseded",
            dispatch_role="worker",
            dispatch_round=1,
        )
    )
    kwargs = dict(
        workflow_id="wf",
        node_id="implementation",
        task_id="replacement",
        dispatch_operation_id=op["id"],
        dispatch_attempt=attempt,
        execution_id="execution-1",
        run_id="replacement-run",
        supersedes="old",
        dispatch_round=2,
        now=1000,
    )
    if attempt != 1:
        with pytest.raises(ValueError, match="attempt"):
            begin_launch_intent(scene.store, **kwargs)
        assert not scene.store.list_events(event_type="launch_intent")
    else:
        intent = begin_launch_intent(scene.store, **kwargs)["intent"]
        assert intent["dispatch_attempt"] == 1
        scene.store.save_task(
            dict(
                task_id="replacement",
                workflow_id="wf",
                node="implementation",
                execution_id="execution-1",
                run_id="replacement-run",
                status="working",
                supersedes="old",
                dispatch_round=2,
                dispatch_role="worker",
                dispatch_operation_id=op["id"],
                launch_intent_id=intent["intent_id"],
            )
        )
        assert scene.store.get_task("replacement")["run_id"] == "replacement-run"


@pytest.mark.parametrize("counter", [0, 1, 2])
@pytest.mark.parametrize("action", ["resources", "registration", "resume"])
def test_persisted_legacy_zero_attempt_replacement(scene, counter, action):
    from herdr.task_resources import begin_launch_intent, record_launch_resources

    nd.reconcile_workflow(scene.store.db_path, "wf", now=1000)
    op = op_for(scene, "implementation")
    scene.store.save_task(
        dict(
            task_id="old",
            workflow_id="wf",
            node="implementation",
            execution_id="execution-1",
            run_id="old-run",
            status="superseded",
            dispatch_role="worker",
            dispatch_round=1,
        )
    )
    kwargs = dict(
        workflow_id="wf",
        node_id="implementation",
        task_id="replacement",
        dispatch_operation_id=op["id"],
        execution_id="execution-1",
        run_id="replacement-run",
        supersedes="old",
        dispatch_round=2,
        now=1000,
    )
    intent = begin_launch_intent(scene.store, **kwargs)["intent"]
    intent["dispatch_attempt"] = 0  # Actual value persisted by pre-fix implementation.
    scene.store.record_event(
        "launch_intent",
        intent,
        workflow_id="wf",
        task_id=intent["key"],
        source="launch",
        timestamp=1001,
    )
    if counter > 0:
        with recovery_store._transaction(scene.store.db_path) as conn:
            nd._write(
                conn,
                op,
                "running",
                {"deadline_at": 1900},
                1010,
                started=1,
                attempts=counter,
                owner="owner",
                lease=1100,
            )

    def perform():
        if action == "resources":
            return record_launch_resources(
                scene.store, intent, {"planned_clone_path": "/tmp/legacy"}, now=1010
            )
        if action == "resume":
            return begin_launch_intent(
                scene.store, dispatch_attempt=max(1, counter), **kwargs
            )
        return scene.store.save_task(
            dict(
                task_id="replacement",
                workflow_id="wf",
                node="implementation",
                execution_id="execution-1",
                run_id="replacement-run",
                status="working",
                supersedes="old",
                dispatch_role="worker",
                dispatch_round=2,
                dispatch_operation_id=op["id"],
                launch_intent_id=intent["intent_id"],
            )
        )

    if counter == 2:
        with pytest.raises(ValueError, match="attempt"):
            perform()
        assert scene.store.get_task("replacement") is None
    else:
        perform()
