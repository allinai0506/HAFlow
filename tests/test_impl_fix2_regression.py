"""Fix-loop regression tests for the six review blockers.

Every test uses temporary state, temporary repositories, or an injected
boundary.  No production controller, service, pane, or model is contacted.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import multiprocessing
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr import blocked_sla, completion, delivery_record, repo_hygiene, workflow_docs
from herdr.state_store import SQLiteStateStore, reset_state_store

ROOT = Path(__file__).resolve().parent.parent


class StopSentinelLoop(Exception):
    """Stop the daemon's otherwise infinite test loop."""


def load_script(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_task(
    store: SQLiteStateStore,
    task_id: str,
    *,
    workflow_id: str = "wf-fix2",
    status: str = "working",
    pane_id: str = "pane-fix2",
) -> dict:
    store.save_workflow({"workflow_id": workflow_id, "status": "running"})
    store.save_task(
        {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "opencode",
            "status": status,
            "pane_id": pane_id,
            "started_at": 1.0,
            "created_at": 1.0,
        }
    )
    return store.get_task(task_id)


def cas_child(db_path: str, task_id: str, ready, release, result_queue):
    try:
        store = SQLiteStateStore(Path(db_path))
        task = store.get_task(task_id)
        result_queue.put(("observed", task["version"]))
        ready.set()
        release.wait(10)
        result = store.compare_and_set_task_transition(
            task_id=task_id,
            to_status="agent_done",
            reason="completion_test",
            source="test",
            expected_status=task["status"],
            expected_version=task["version"],
        )
        result_queue.put(("accepted", bool(result.get("accepted"))))
    except (AssertionError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result_queue.put(("error", repr(exc)))


def test_completion_epoch_restart_sets_first_seen_and_can_complete(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-epoch-restart")

    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="working",
        observed_at=103.0,
    )

    # A human reopen changes the task epoch while the old marker remains visible.
    store.transition_task(
        task_id=task["task_id"],
        to_status="blocked",
        reason="human_reopen_probe",
        source="test",
    )
    store.transition_task(
        task_id=task["task_id"],
        to_status="working",
        reason="human_reopen",
        source="test",
    )

    restarted = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=106.0,
    )
    assert restarted["epoch_changed"] is True
    assert restarted["first_seen_at"] == pytest.approx(106.0)
    assert restarted["consecutive_samples"] == 1
    assert restarted["ready"] is False

    confirmed = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=109.0,
    )
    current = store.get_task(task["task_id"])
    assert current["status"] == "working"
    assert confirmed["ready"] is True
    assert confirmed["first_seen_at"] == pytest.approx(106.0)
    assert confirmed["observed_version"] == current["version"]


def test_completion_resident_marker_requires_a_new_absence_cycle(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-resident-marker")

    first = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=100.0,
    )
    second = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=103.0,
    )
    assert first["first_seen_at"] is None
    assert second["first_seen_at"] is None
    assert second["consecutive_samples"] == 0
    assert second["ready"] is False

    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="idle",
        observed_at=106.0,
    )
    restarted = store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=109.0,
    )
    assert restarted["first_seen_at"] == pytest.approx(109.0)
    assert restarted["consecutive_samples"] == 1
    assert restarted["ready"] is False


def test_completion_old_epoch_cannot_flip_after_independent_process_race(tmp_path):
    db_path = str(tmp_path / "state.db")
    store = SQLiteStateStore(Path(db_path))
    task = seed_task(store, "task-epoch-race")

    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="working",
        observed_at=103.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=106.0,
    )

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=cas_child,
        args=(db_path, task["task_id"], ready, release, result_queue),
    )
    process.start()
    try:
        assert result_queue.get(timeout=10)[0] == "observed"
        assert ready.wait(10)
        # Independent connection changes the epoch after the child observed it.
        store.update_task_metadata(task["task_id"], {"human_touch": "reopen"})
        release.set()
        result = result_queue.get(timeout=10)
    finally:
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert result == ("accepted", False)
    assert process.exitcode == 0
    assert store.get_task(task["task_id"])["status"] == "working"


def test_controller_done_marker_does_not_bypass_idle_fallback(tmp_path, monkeypatch):
    controller = load_script("herdr_controller_fix2_idle", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-idle-fallback")

    monkeypatch.delenv("HERDR_CONTROLLER_TEST", raising=False)
    pane_result = SimpleNamespace(
        returncode=0,
        stdout="HERDR_TASK_DONE:task-idle-fallback\n",
        stderr="",
    )
    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "get_task", return_value=task), \
         patch.object(controller, "process_completion_observation", return_value=False), \
         patch.object(store, "get_completion_observation", return_value=None), \
         patch.object(controller, "workflow_config_for", return_value={}), \
         patch.object(controller.subprocess, "run", return_value=pane_result), \
         patch.object(controller, "_set_observed_status", return_value=True) as transition, \
         patch.object(controller, "emit_done_if_allowed"):
        controller.handle_event(task["task_id"], "idle")

    transition.assert_called_once_with(task, "agent_done", "idle_marker")


def test_controller_waits_for_stable_marker_before_idle_fallback(tmp_path, monkeypatch):
    controller = load_script("herdr_controller_fix2_pending", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-pending-marker")
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=103.0,
    )
    monkeypatch.delenv("HERDR_CONTROLLER_TEST", raising=False)
    pane_result = SimpleNamespace(
        returncode=0,
        stdout="HERDR_TASK_DONE:task-pending-marker\n",
        stderr="",
    )
    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "get_task", return_value=task), \
         patch.object(controller, "process_completion_observation", return_value=False), \
         patch.object(controller, "workflow_config_for", return_value={}), \
         patch.object(controller.subprocess, "run", return_value=pane_result), \
         patch.object(controller, "_set_observed_status", return_value=True) as transition:
        controller.handle_event(task["task_id"], "idle")
    transition.assert_not_called()


def test_sentinel_crash_reaches_state_store_and_controller_auto_recover(tmp_path):
    sentinel = load_script("herdr_sentinel_fix2_crash", "services/herdr-sentinel.py")
    controller = load_script("herdr_controller_fix2_recover", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-crash", workflow_id="wf-crash")

    sentinel.STATE_FILE = tmp_path / "sentinel-state.json"
    crash_screen = "Bun has crashed while running task\n"
    with patch.object(sentinel, "_get_store", return_value=store), \
         patch.object(sentinel, "pane_visible", return_value=crash_screen), \
         patch.object(sentinel, "agent_status", return_value="working"), \
         patch.object(sentinel, "check_dispatch_fuse", return_value=False), \
         patch.object(sentinel, "check_task_stalls", return_value=False), \
         patch.object(sentinel, "save_json_atomic"), \
         patch.object(sentinel.time, "sleep", side_effect=StopSentinelLoop), \
         pytest.raises(StopSentinelLoop):
        sentinel.main()

    failed = store.get_task(task["task_id"])
    assert failed["status"] == "failed"
    assert any(
        entry.get("reason") == "agent_process_crash"
        for entry in failed.get("status_history", [])
    )
    assert store.list_events(
        task_id=task["task_id"],
        event_type="agent_process_crash_observed",
    )

    commands = []

    def fake_run(command, *args, **kwargs):
        commands.append(command)
        if "supersede" in command:
            current = store.get_task(task["task_id"])
            store.compare_and_set_task_transition(
                task_id=task["task_id"],
                to_status="superseded",
                reason="auto-recover: infrastructure failure",
                source="test",
                expected_status=current["status"],
                expected_version=current["version"],
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "workflow_closed", return_value=False), \
         patch.object(controller, "_workflow_entry", return_value={"status": "running"}), \
         patch.object(controller, "load_tasks", side_effect=store.list_tasks), \
         patch.object(controller, "clear_stage_advance"), \
         patch.object(controller.subprocess, "run", side_effect=fake_run):
        assert controller.recover_infra_failed_tasks("wf-crash", store.list_tasks())
        assert controller.recover_infra_failed_tasks("wf-crash", store.list_tasks()) is False

    assert len([command for command in commands if "supersede" in command]) == 1
    assert store.get_task(task["task_id"])["status"] == "superseded"


def test_blocked_repush_is_scheduled_off_polling_path_and_deduplicated(tmp_path):
    controller = load_script("herdr_controller_fix2_repush", "services/herdr-controller.py")
    from herdr import liveness

    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-repush", workflow_id="wf-repush", status="blocked")
    episode_store = liveness.EpisodeStore(tmp_path / "attention.json")
    entry = float(task["updated_at"])
    now = entry + blocked_sla.first_sla_seconds() + 1.0
    episode_store.upsert("task-repush:blocked_sla", {
        "task_id": task["task_id"],
        "workflow_id": task["workflow_id"],
        "entry_updated_at": entry,
        "entry_version": task["version"],
        "episode_id": f"task-repush:{int(entry)}:{task['version']}",
        "active_seconds": blocked_sla.first_sla_seconds(),
        "last_tick_at": now - blocked_sla.first_sla_seconds(),
        "coordinator_notices": 0,
        "repushes": 0,
        "repush_state": "pending",
        "delivery_attempts": 0,
        "recovery_attempts": 0,
        "human_escalations": 0,
        "last_action_at": None,
    })

    release = threading.Event()
    finished = threading.Event()
    calls = []

    def slow_sender(_task, _decision):
        calls.append(time.monotonic())
        release.wait(1.0)
        finished.set()
        return True, "delivered"

    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "_attention_store", episode_store), \
         patch.object(controller, "_send_blocked_repush", side_effect=slow_sender), \
         patch.object(controller, "enqueue_coordinator_event"):
        started = time.monotonic()
        try:
            first = controller.process_blocked_sla_task(task, now=now)
            elapsed = time.monotonic() - started
            second = controller.process_blocked_sla_task(task, now=now + 1.0)
            assert elapsed < 0.5
            assert first["action"] == "repush"
            assert second["action"] != "repush"
            assert len(calls) == 1
        finally:
            release.set()
        assert finished.wait(2.0)
        for _ in range(20):
            events = store.list_events(task_id=task["task_id"])
            if any(event["event_type"] == "blocked_auto_repush" for event in events):
                break
            time.sleep(0.01)

    events = store.list_events(task_id=task["task_id"])
    assert sum(event["event_type"] == "blocked_auto_repush" for event in events) == 1


def test_blocked_repush_stale_worker_lease_recovers_once():
    task = {
        "task_id": "task-stale-repush",
        "status": "blocked",
        "updated_at": 100.0,
    }
    episode = blocked_sla.new_episode(task, now=100.0)
    episode.update({
        "active_seconds": blocked_sla.first_sla_seconds(),
        "repushes": blocked_sla.MAX_AUTO_REPUSHES_PER_EPISODE,
        "repush_state": "in_flight",
        "repush_inflight_at": 0.0,
        "last_action_at": None,
    })
    now = (
        100.0
        + blocked_sla.repush_inflight_timeout_seconds()
        + blocked_sla.jitter_window_seconds()
        + 1.0
    )
    assert blocked_sla.repush_inflight_is_stale(episode, now=now) is True
    recovered = blocked_sla.recover_stale_repush(episode, now=now)
    decision = blocked_sla.decide_blocked_action(
        task=task,
        episode=recovered,
        now=now,
    )
    assert decision["action"] == "repush_recover"
    assert recovered["repushes"] == 1


def test_delivery_invalidation_is_scoped_by_node_identity_and_order(tmp_path, monkeypatch):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path))
    base = "base-fix2"
    old = workflow_docs.append_note(
        "wf-delivery-fix2",
        kind="delivery",
        title="old",
        node="wrapup",
        base_sha=base,
        fields={
            "delivery_id": "candidate-a",
            "delivery_branch": "agent/test",
            "candidate_sha": "sha-a",
            "review_task": "review-a",
            "test_gate": "test-a",
        },
    )
    sibling = workflow_docs.append_note(
        "wf-delivery-fix2",
        kind="delivery",
        title="sibling",
        node="test",
        base_sha=base,
        fields={
            "delivery_id": "candidate-sibling",
            "delivery_branch": "agent/test-sibling",
            "candidate_sha": "sha-sibling",
            "review_task": "review-sibling",
            "test_gate": "test-sibling",
        },
    )
    replacement = workflow_docs.append_note(
        "wf-delivery-fix2",
        kind="delivery",
        title="replacement",
        node="wrapup",
        base_sha=base,
        fields={
            "delivery_id": "candidate-b",
            "delivery_branch": "agent/test",
            "candidate_sha": "sha-b",
            "review_task": "review-b",
            "test_gate": "test-b",
            "supersedes": "candidate-a",
        },
    )
    invalidation = workflow_docs.append_note(
        "wf-delivery-fix2",
        kind="invalidation",
        title="fix-loop",
        node="wrapup",
        base_sha=base,
        invalidates=["wrapup"],
        fields={"invalidated_candidates": ["candidate-b"]},
    )
    future = workflow_docs.append_note(
        "wf-delivery-fix2",
        kind="delivery",
        title="future",
        node="wrapup",
        base_sha=base,
        fields={
            "delivery_id": "candidate-future",
            "delivery_branch": "agent/test-future",
            "candidate_sha": "sha-future",
            "review_task": "review-future",
            "test_gate": "test-future",
        },
    )

    annotated = workflow_docs.annotate_notes(
        [old, sibling, replacement, invalidation, future],
        current_base_sha=base,
    )
    by_id = {item["note_id"]: item for item in annotated}
    assert by_id[invalidation["note_id"]]["stale"] is False
    assert by_id[sibling["note_id"]]["stale"] is False
    assert by_id[old["note_id"]]["stale"] is False
    assert by_id[replacement["note_id"]]["stale"] is True
    assert by_id[future["note_id"]]["stale"] is False
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery(annotated)


def test_delivery_selector_fails_closed_for_same_identity_and_same_tick_candidates():
    first = {
        "note_id": "n-a",
        "kind": "delivery",
        "ts": 100.0,
        "workflow_id": "wf-a",
        "node": "wrapup",
        "base_sha": "base",
        "delivery_id": "candidate-same",
        "delivery_branch": "branch-a",
        "candidate_sha": "sha-a",
        "review_task": "review-a",
        "test_gate": "test-a",
    }
    second = {
        "note_id": "n-b",
        "kind": "delivery",
        "ts": 100.0,
        "workflow_id": "wf-a",
        "node": "wrapup",
        "base_sha": "base",
        "delivery_id": "candidate-other",
        "delivery_branch": "branch-b",
        "candidate_sha": "sha-b",
        "review_task": "review-b",
        "test_gate": "test-b",
    }
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery([first, second])

    conflicting = dict(second)
    conflicting["delivery_id"] = "candidate-same"
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery([first, conflicting])


def test_delivery_selector_rejects_unknown_supersession_edge():
    candidate = {
        "note_id": "n-unknown-edge",
        "kind": "delivery",
        "ts": 100.0,
        "workflow_id": "wf-unknown-edge",
        "node": "wrapup",
        "base_sha": "base",
        "delivery_id": "candidate-unknown",
        "candidate_sha": "sha-unknown",
        "delivery_branch": "agent/test",
        "review_task": "review",
        "test_gate": "test",
        "supersedes": "candidate-not-written",
    }
    assert delivery_record.select_effective_delivery([candidate]) is None


def test_repo_hygiene_preserves_spaces_and_reaches_owner_branch_metadata(tmp_path):
    repo = tmp_path / "main repo"
    repo.mkdir()
    def run(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    run("init", "-q")
    run("config", "user.email", "owner@example.test")
    tracked = repo / "docs with spaces.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    run("add", "docs with spaces.txt")
    run("commit", "-qm", "baseline")

    clean_status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    )
    assert repo_hygiene.parse_porcelain_paths(clean_status) == []

    tracked.write_text("dirty\n", encoding="utf-8")
    status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    )
    diagnosis = repo_hygiene.diagnose_main_dirty(
        porcelain=status,
        configured_email="owner@example.test",
        task_branch="agent/opencode/feat-fix2",
        task_id="task-fix2",
    )
    assert diagnosis["blocked_files"] == ["docs with spaces.txt"]
    assert diagnosis["blocked_total"] == 1
    assert diagnosis["owner"] == "owner@example.test"
    assert diagnosis["owner_source"] == "git_config_user_email"
    assert diagnosis["task_branch"] == "agent/opencode/feat-fix2"

    tracked.write_text("clean\n", encoding="utf-8")
    untracked = repo / "clone infra.txt"
    untracked.write_text("untracked\n", encoding="utf-8")
    untracked_status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    )
    assert repo_hygiene.launch_precheck_message(
        porcelain=untracked_status,
        task_id="task-fix2",
        integration_mode="git",
    ) is None

    clean_again = repo_hygiene.launch_precheck_message(
        porcelain="",
        task_id="task-fix2",
        integration_mode="git",
    )
    assert clean_again is None
    tracked.write_text("dirty-after-precheck\n", encoding="utf-8")
    toctou_status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    )
    assert repo_hygiene.parse_porcelain_paths(toctou_status) == ["docs with spaces.txt"]


def test_repo_hygiene_parses_rename_and_multiple_paths_with_spaces():
    porcelain = " M docs/one two.txt\nR  old name.txt -> new name.txt\n"
    assert repo_hygiene.parse_porcelain_paths(porcelain) == [
        "docs/one two.txt",
        "new name.txt",
    ]


def test_integrate_exit5_reports_real_git_paths_owner_and_branch(tmp_path, capsys):
    task_bin = load_script("herdr_task_fix2_integrate", "bin/herdr-task")
    main_repo = tmp_path / "main repository"
    clone = tmp_path / "task clone"
    main_repo.mkdir()

    def git(repo, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    git(main_repo, "init", "-q")
    git(main_repo, "config", "user.email", "owner@example.test")
    git(main_repo, "branch", "-M", "main")
    tracked = main_repo / "source file with spaces.txt"
    tracked.write_text("base\n", encoding="utf-8")
    git(main_repo, "add", "source file with spaces.txt")
    git(main_repo, "commit", "-qm", "base")
    subprocess.run(
        ["git", "clone", "-q", str(main_repo), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    git(clone, "config", "user.email", "owner@example.test")
    git(clone, "checkout", "-qb", "agent/opencode/feat-fix2")
    (clone / "source file with spaces.txt").write_text("candidate\n", encoding="utf-8")
    git(clone, "commit", "-qam", "candidate")
    tracked.write_text("dirty WIP\n", encoding="utf-8")

    task = {
        "task_id": "task-integrate-fix2",
        "workflow_id": "wf-integrate-fix2",
        "status": "committed",
        "integration_mode": "git",
        "execution_mode": "git",
        "node": "implementation",
        "stage": "implementation",
        "clone_path": str(clone),
        "branch": "agent/opencode/feat-fix2",
        "source_repo": str(main_repo),
        "base_branch": "main",
        "baseline_fingerprint": {"tracked": {}, "untracked": {}},
        "baseline_commit": git(clone, "rev-parse", "HEAD").stdout.strip(),
        "created_at": 1.0,
    }

    def load_tasks():
        return {"tasks": [task]}

    project = {"project_root": str(main_repo), "base_branch": "main"}
    with patch.object(task_bin, "load_tasks", side_effect=load_tasks), \
         patch.object(task_bin, "project_for_workflow", return_value=project), \
         patch.object(task_bin, "ensure_branch_available"), \
         patch.object(task_bin, "current_workspace_fingerprint", return_value=task["baseline_fingerprint"]), \
         patch.object(task_bin, "ensure_no_git_processes"), \
         pytest.raises(SystemExit) as exc:
        task_bin.integrate_task(task["task_id"])

    assert exc.value.code == 5
    output = capsys.readouterr().out
    result_line = next(
        line for line in output.splitlines()
        if line.startswith("HERDR_INTEGRATE_RESULT=")
    )
    result = json.loads(result_line.split("=", 1)[1])
    assert result["blocked_files"] == ["source file with spaces.txt"]
    assert result["blocked_total"] == 1
    assert result["owner"] == "owner@example.test"
    assert result["owner_source"] == "git_config_user_email"
    assert result["task_branch"] == "agent/opencode/feat-fix2"
    assert "herdr-task integrate task-integrate-fix2" in result["remediation_cmd"]


def test_prompt_sanitizer_early_signal_and_uncertain_completion_are_observable(tmp_path):
    sanitized, replacements = completion.sanitize_prompt(
        "Acceptance: HERDR_TASK_DONE:task-sanitize",
        "task-sanitize",
    )
    assert replacements == 1
    assert "HERDR_TASK_DONE:task-sanitize" not in sanitized
    assert "HERDR_TASK_DONE:<TASK_ID>" in sanitized

    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-uncertain-fix2")
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="working",
        observed_at=103.0,
    )
    uncertain = store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=106.0,
    )
    assert uncertain["uncertain"] is True
    assert uncertain["ready"] is False

    sentinel = load_script("herdr_sentinel_fix2_early", "services/herdr-sentinel.py")
    sentinel.STATE_FILE = tmp_path / "early-state.json"
    early_task = seed_task(store, "task-early-fix2")
    marker = f"HERDR_TASK_DONE:{early_task['task_id']}"
    sleep_calls = 0

    def stop_after_three_samples(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 3:
            raise StopSentinelLoop

    with patch.object(sentinel, "_get_store", return_value=store), \
         patch.object(sentinel, "pane_visible", return_value=marker), \
         patch.object(sentinel, "agent_status", return_value="working"), \
         patch.object(sentinel, "check_dispatch_fuse", return_value=False), \
         patch.object(sentinel, "check_task_stalls", return_value=False), \
         patch.object(sentinel, "save_json_atomic"), \
         patch.object(sentinel.time, "sleep", side_effect=stop_after_three_samples), \
         pytest.raises(StopSentinelLoop):
        sentinel.main()
    early_events = store.list_events(
        task_id=early_task["task_id"],
        event_type="early_done_signal",
    )
    assert len(early_events) >= 3
    assert early_events[-1]["payload"]["count"] >= 3

    store.compare_and_set_task_transition(
        task_id=task["task_id"],
        to_status="agent_done",
        reason="test_cleanup",
        source="test",
    )
    store.compare_and_set_task_transition(
        task_id=early_task["task_id"],
        to_status="agent_done",
        reason="test_cleanup",
        source="test",
    )
    uncertain_task = seed_task(store, "task-uncertain-event")
    uncertain_marker = f"HERDR_TASK_DONE:{uncertain_task['task_id']}"
    uncertain_sleeps = 0

    def stop_after_two_samples(_seconds):
        nonlocal uncertain_sleeps
        uncertain_sleeps += 1
        if uncertain_sleeps >= 2:
            raise StopSentinelLoop

    with patch.object(sentinel, "_get_store", return_value=store), \
         patch.object(sentinel, "pane_visible", side_effect=[uncertain_marker, ""]), \
         patch.object(sentinel, "agent_status", return_value="working"), \
         patch.object(sentinel, "check_dispatch_fuse", return_value=False), \
         patch.object(sentinel, "check_task_stalls", return_value=False), \
         patch.object(sentinel, "save_json_atomic"), \
         patch.object(sentinel.time, "sleep", side_effect=stop_after_two_samples), \
         pytest.raises(StopSentinelLoop):
        sentinel.main()
    assert store.list_events(
        task_id=uncertain_task["task_id"],
        event_type="completion_uncertain",
    )


def test_controller_completes_after_epoch_restart_with_current_state(tmp_path):
    controller = load_script("herdr_controller_fix2_epoch_accept", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-controller-epoch-accept")
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="working",
        observed_at=103.0,
    )
    store.transition_task(
        task_id=task["task_id"],
        to_status="blocked",
        reason="human_reopen_probe",
        source="test",
    )
    store.transition_task(
        task_id=task["task_id"],
        to_status="working",
        reason="human_reopen",
        source="test",
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=106.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=109.0,
    )
    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "enqueue_coordinator_event"):
        assert controller.process_completion_observation(
            store.get_task(task["task_id"]),
            now=109.5,
        ) is True
    assert store.get_task(task["task_id"])["status"] == "agent_done"
    assert store.get_completion_observation(task["task_id"]) is None
    assert store.list_events(
        task_id=task["task_id"],
        event_type="completion_sentinel_accepted",
    )


def test_actual_controller_state_store_completion_chain_uses_authoritative_epoch(tmp_path):
    controller = load_script("herdr_controller_fix2_chain", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-controller-chain")
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=False,
        agent_status="working",
        observed_at=100.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="working",
        observed_at=103.0,
    )
    store.observe_completion(
        task_id=task["task_id"],
        marker_present=True,
        agent_status="idle",
        observed_at=106.0,
    )
    stale_task = store.get_task(task["task_id"])
    store.update_task_metadata(task["task_id"], {"controller_rewrite": "recovery"})

    with patch.object(controller, "_get_store", return_value=store), \
         patch.object(controller, "enqueue_coordinator_event"):
        assert controller.process_completion_observation(stale_task, now=109.0) is False

    assert store.get_task(task["task_id"])["status"] == "working"
    rejected = store.list_events(
        task_id=task["task_id"],
        event_type="completion_sentinel_cas_rejected",
    )
    assert rejected
    reset_state_store()
