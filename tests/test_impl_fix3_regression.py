"""FR-1/2/4/5/6 regression contracts for impl-fix3.

These tests intentionally use temporary SQLite stores, workflow-document roots,
and controller/CLI module entry points.  They are kept separate from the
implementation helpers so a passing test proves the durable boundary rather
than only a pure predicate.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import multiprocessing
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from herdr import blocked_sla, delivery_record, liveness, workflow_docs
from herdr.state_store import SQLiteStateStore, reset_state_store

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(ROOT / relative)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_task(
    store: SQLiteStateStore,
    task_id: str = "task-fix3",
    *,
    workflow_id: str = "wf-fix3",
    status: str = "working",
    agent: str = "opencode",
    started_at: float = 1.0,
) -> dict:
    store.save_workflow({"workflow_id": workflow_id, "status": "running"})
    store.save_task(
        {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "run_id": f"run-{task_id}",
            "node": "implementation",
            "stage": "implementation",
            "agent": agent,
            "status": status,
            "started_at": started_at,
            "created_at": started_at,
            "pane_id": "pane-fix3",
        }
    )
    return store.get_task(task_id)


def delivery_fields(
    delivery_id: str,
    candidate_sha: str,
    *,
    review_task: str = "review-task",
    test_gate: str = "test-gate",
    base: str = "base-sha",
    **extra: object,
) -> dict:
    fields = {
        "delivery_id": delivery_id,
        "delivery_branch": "agent/opencode/fix3",
        "candidate_sha": candidate_sha,
        "review_task": review_task,
        "test_gate": test_gate,
        "base": base,
    }
    fields.update(extra)
    return fields


def append_delivery(
    workflow_id: str,
    delivery_id: str,
    candidate_sha: str,
    **extra: object,
) -> dict:
    return workflow_docs.append_note(
        workflow_id,
        kind="delivery",
        title=f"delivery {delivery_id}",
        node="wrapup",
        fields=delivery_fields(delivery_id, candidate_sha, **extra),
    )


@pytest.fixture(autouse=True)
def _reset_global_store():
    env_keys = (
        "HERDR_STATE_DB",
        "TASKS_FILE",
        "WORKFLOWS_FILE",
        "HERDR_ATTENTION_FILE",
        workflow_docs.DOCS_DIR_ENV,
    )
    previous = {key: os.environ.get(key) for key in env_keys}
    reset_state_store()
    yield
    reset_state_store()
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# FR-1: durable sampling and one atomic completion transition
# ---------------------------------------------------------------------------


def test_completion_epoch_reset_recovers_and_commits_once(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-epoch-recovery", started_at=1.0)

    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=100.0
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="working", observed_at=103.0
    )
    # Same-status human reopen bumps the monotonic task version.
    store.update_task_metadata(task["task_id"], {"human_touch": "reopen"})

    first_new_epoch = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=106.0
    )
    assert first_new_epoch["epoch_changed"] is True
    assert first_new_epoch["ready"] is False
    assert first_new_epoch["first_seen_at"] == 106.0

    ready = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=109.0
    )
    assert ready["ready"] is True
    assert ready["consecutive_samples"] == 2

    result = store.compare_and_set_completion_transition(
        task["task_id"],
        reason="completion_sentinel",
        source="test-controller",
        expected_status="working",
        expected_version=ready["observed_version"],
        now=109.0,
    )
    assert result["accepted"] is True
    assert store.get_task(task["task_id"])["status"] == "agent_done"

    # A replay after the observation was consumed must not create a second
    # transition or a second completion event.
    replay = store.compare_and_set_completion_transition(
        task["task_id"],
        reason="completion_sentinel",
        source="test-controller",
        expected_status="working",
        expected_version=ready["observed_version"],
        now=109.0,
    )
    assert replay["accepted"] is False
    transitions = store.list_events(
        task_id=task["task_id"], event_type="task_transition"
    )
    assert len(transitions) == 1
    assert transitions[0]["payload"]["reason"] == "completion_sentinel"


def test_completion_requires_a_poll_interval_and_marker_reappearance(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sampling-boundary", started_at=1.0)

    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=100.0
    )
    first = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=103.0
    )
    too_soon = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=104.0
    )
    assert first["consecutive_samples"] == 1
    assert too_soon["consecutive_samples"] == 1
    assert too_soon["ready"] is False

    second = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=106.0
    )
    assert second["consecutive_samples"] == 2
    assert second["ready"] is True

    # A disappearance starts a new absent -> present cycle; the old samples
    # cannot be revived by another immediate marker sighting.
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=107.0
    )
    reappeared = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=110.0
    )
    assert reappeared["consecutive_samples"] == 1
    assert reappeared["ready"] is False


def test_completion_cas_rechecks_marker_in_same_transaction(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-atomic-marker", started_at=1.0)
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=100.0
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=103.0
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=106.0
    )
    # The controller must not be able to commit a stale ready snapshot after
    # the next durable sample says the marker vanished.
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=107.0
    )
    result = store.compare_and_set_completion_transition(
        task["task_id"],
        reason="completion_sentinel",
        source="test-controller",
        expected_status="working",
        expected_version=task["version"],
        now=107.0,
    )
    assert result["accepted"] is False
    assert store.get_task(task["task_id"])["status"] == "working"


def _completion_cas_child(db_path, task_id, expected_version, result_queue):
    try:
        store = SQLiteStateStore(Path(db_path))
        result = store.compare_and_set_completion_transition(
            task_id,
            reason="completion_sentinel",
            source="competing-controller",
            expected_status="working",
            expected_version=expected_version,
            now=110.0,
        )
        result_queue.put((result.get("accepted"), result.get("reason")))
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:  # pragma: no cover - surfaced in assertion
        result_queue.put(("error", repr(exc)))


def test_two_controller_processes_complete_only_once(tmp_path):
    db_path = str(tmp_path / "state.db")
    store = SQLiteStateStore(Path(db_path))
    task = seed_task(store, "task-cas-processes", started_at=1.0)
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=100.0
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=103.0
    )
    ready = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=106.0
    )
    assert ready["ready"] is True

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_completion_cas_child,
            args=(db_path, task["task_id"], ready["observed_version"], result_queue),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert sorted(result[0] for result in results) == [False, True]
    assert len(store.list_events(task_id=task["task_id"], event_type="task_transition")) == 1
    assert store.get_task(task["task_id"])["status"] == "agent_done"


def test_controller_uses_atomic_completion_gateway_and_does_not_replay_done(tmp_path):
    controller = load_script("ctrl-completion-fix3", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-controller-completion", started_at=1.0)
    for marker, status, at in (
        (False, "working", 100.0),
        (True, "idle", 103.0),
        (True, "idle", 106.0),
    ):
        store.observe_completion(
            task["task_id"],
            marker_present=marker,
            agent_status=status,
            observed_at=at,
        )
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "enqueue_coordinator_event"
    ) as enqueue:
        assert controller.process_completion_observation(task, now=106.0) is True
        assert controller.process_completion_observation(task, now=107.0) is False
    assert store.get_task(task["task_id"])["status"] == "agent_done"
    assert enqueue.call_count == 1
    assert len(store.list_events(task_id=task["task_id"], event_type="task_transition")) == 1


def test_sentinel_crash_observation_reaches_controller_recovery_policy(tmp_path):
    sentinel = load_script("herdr-sentinel-crash-fix3", "services/herdr-sentinel.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-crash-recovery", status="working")
    changes = {}
    expected = {}
    versions = {}
    sentinel.observe_crash_pattern(
        task, changes=changes, expected=expected, versions=versions
    )
    assert changes == {"task-crash-recovery": ("failed", "agent_process_crash")}
    with patch.object(sentinel, "_get_store", return_value=store):
        assert sentinel.update_statuses(
            changes, expected=expected, versions=versions
        ) is True
    failed = store.get_task(task["task_id"])
    assert failed["status"] == "failed"
    assert liveness.select_infra_failures_for_recovery(
        [failed], {"agent_process_crash"}
    )[0]["task_id"] == task["task_id"]


def test_idle_without_done_marker_cannot_bypass_completion_samples(tmp_path):
    controller = load_script("ctrl-no-marker-fix3", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-no-marker", status="working")
    screen = subprocess.CompletedProcess([], 0, "idle without completion marker\n", "")
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "subprocess", wraps=controller.subprocess
    ) as subprocess_module, patch.object(
        controller, "get_agent_runtime_status", return_value="idle"
    ), patch("time.sleep"), patch.object(
        controller, "workflow_config_for", return_value={}
    ), patch.object(controller, "enqueue_coordinator_event"):
        subprocess_module.run.return_value = screen
        controller.handle_event(task["task_id"], "idle")
    assert store.get_task(task["task_id"])["status"] == "working"
    assert store.list_events(task_id=task["task_id"], event_type="task_transition") == []


def test_done_event_without_marker_cannot_bypass_completion_samples(tmp_path):
    controller = load_script("ctrl-done-no-marker-fix3", "services/herdr-controller.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-done-no-marker", status="working")
    screen = subprocess.CompletedProcess([], 0, "done event without marker\n", "")
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "subprocess", wraps=controller.subprocess
    ) as subprocess_module, patch.object(controller, "enqueue_coordinator_event"):
        subprocess_module.run.return_value = screen
        controller.handle_event(task["task_id"], "done")
    assert store.get_task(task["task_id"])["status"] == "working"


def test_out_of_order_completion_samples_cannot_overwrite_newer_state(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-out-of-order", started_at=1.0)
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=100.0
    )
    first = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=103.0
    )
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=106.0
    )
    delayed = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=104.0
    )
    assert delayed["last_sample_at"] == 106.0
    assert delayed["consecutive_samples"] == 0
    assert delayed["ready"] is False
    assert first["consecutive_samples"] == 1


def test_legacy_completion_row_with_null_sample_time_recovers(tmp_path):
    db_path = tmp_path / "state.db"
    store = SQLiteStateStore(db_path)
    task = seed_task(store, "task-legacy-observation", started_at=1.0)
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=100.0
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=103.0
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE completion_observations SET last_sample_at = NULL "
            "WHERE task_id = ?",
            (task["task_id"],),
        )
    recovered = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=106.0
    )
    assert recovered["consecutive_samples"] == 2
    assert recovered["ready"] is True


def test_atomic_completion_rejects_stale_ready_observation(tmp_path):
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-stale-ready", started_at=1.0)
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="working", observed_at=100.0
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=103.0
    )
    ready = store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=106.0
    )
    result = store.compare_and_set_completion_transition(
        task["task_id"],
        expected_status="working",
        expected_version=ready["observed_version"],
        now=1000.0,
    )
    assert result["accepted"] is False
    assert result["reason"] == "completion_observation_stale"
    assert store.get_task(task["task_id"])["status"] == "working"


def test_sentinel_completion_change_cannot_use_generic_status_cas(tmp_path):
    sentinel = load_script("herdr-sentinel-completion-fix3", "services/herdr-sentinel.py")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sentinel-completion", started_at=1.0)
    base = time.time() - 6.0
    store.observe_completion(
        task["task_id"], marker_present=False, agent_status="idle", observed_at=base
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=base + 3
    )
    store.observe_completion(
        task["task_id"], marker_present=True, agent_status="idle", observed_at=base + 6
    )
    with patch.object(sentinel, "_get_store", return_value=store):
        assert sentinel.update_statuses({
            task["task_id"]: ("agent_done", "completion_sentinel")
        }) is True
    assert store.get_task(task["task_id"])["status"] == "agent_done"
    assert len(store.list_events(task_id=task["task_id"], event_type="task_transition")) == 1


# ---------------------------------------------------------------------------
# FR-2: one bounded repush, durable episode claim, recovery and escalation
# ---------------------------------------------------------------------------


def load_controller_for_sla(tmp_path, name: str):
    attention_file = tmp_path / f"{name}-attention.json"
    os.environ["HERDR_ATTENTION_FILE"] = str(attention_file)
    return load_script(name, "services/herdr-controller.py"), attention_file


def seed_blocked_episode(store, episode_store, task, key, active_seconds):
    episode_store.upsert(
        key,
        {
            "task_id": task["task_id"],
            "workflow_id": task["workflow_id"],
            "run_id": task.get("run_id"),
            "entry_updated_at": task["updated_at"],
            "entry_version": task["version"],
            "active_seconds": active_seconds,
            "last_tick_at": task["updated_at"],
            "repushes": 0,
            "human_escalations": 0,
            "last_action_at": None,
            "episode_id": blocked_sla.blocked_episode_id(
                task["task_id"], task["updated_at"], task["version"]
            ),
        },
    )


def test_blocked_repush_claim_is_atomic_across_controller_threads(tmp_path):
    os.environ["HERDR_STATE_DB"] = str(tmp_path / "state.db")
    controller, attention_file = load_controller_for_sla(tmp_path, "ctrl-sla-claim")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sla-claim", status="blocked")
    episode_store = liveness.EpisodeStore(attention_file)
    key = controller._blocked_sla_key(task["task_id"])
    now = task["updated_at"] + 2000.0
    seed_blocked_episode(store, episode_store, task, key, blocked_sla.first_sla_seconds())

    entered = threading.Event()
    release = threading.Event()
    result_queue: queue.Queue = queue.Queue()
    sender_calls = []
    sender_lock = threading.Lock()
    start = threading.Barrier(2)

    def send_prompt(_task, _decision):
        with sender_lock:
            sender_calls.append(threading.current_thread().name)
        entered.set()
        assert release.wait(5), "test did not release sender"
        return True, "delivered"

    def run_controller():
        start.wait(5)
        decision = controller.process_blocked_sla_task(
            task, now=now, send_prompt=send_prompt
        )
        result_queue.put(decision)

    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "_attention_store", episode_store
    ), patch.object(controller, "enqueue_coordinator_event"):
        workers = [threading.Thread(target=run_controller) for _ in range(2)]
        for worker in workers:
            worker.start()
        assert entered.wait(5), "neither controller claimed the repush"
        # With an atomic claim, the other controller returns while the first
        # sender is still in flight.  The old implementation leaves both
        # senders blocked here and fails this assertion.
        try:
            other = result_queue.get(timeout=1.5)
            assert other["action"] in {"suppressed_inflight", "suppressed_bounds"}
        except queue.Empty:
            release.set()
            for worker in workers:
                worker.join(5)
            pytest.fail("competing controller did not observe the durable claim")
        finally:
            release.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()

    assert len(sender_calls) == 1
    events = store.list_events(task_id=task["task_id"])
    assert [event["event_type"] for event in events].count("blocked_auto_repush") == 1


def test_expired_inflight_repush_claim_recovers_after_controller_restart(tmp_path):
    controller, attention_file = load_controller_for_sla(tmp_path, "ctrl-sla-restart")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sla-restart", status="blocked")
    episode_store = liveness.EpisodeStore(attention_file)
    key = controller._blocked_sla_key(task["task_id"])
    now = task["updated_at"] + 2000.0
    seed_blocked_episode(store, episode_store, task, key, blocked_sla.first_sla_seconds())
    episode = episode_store.get(key)
    episode.update({
        "repush_state": "in_flight",
        "repush_claim": {
            "claim_id": "dead-controller",
            "claimed_at": now - 300,
            "lease_until": now - 1,
        },
    })
    episode_store.upsert(key, episode)
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "_attention_store", episode_store
    ), patch.object(controller, "enqueue_coordinator_event"):
        decision = controller.process_blocked_sla_task(
            task, now=now, send_prompt=lambda _task, _value: (True, "restart recovery")
        )
    assert decision["action"] == "repush_recover"
    assert decision["recovery"] is True
    assert episode_store.get(key)["recovery_attempts"] == 1
    assert any(
        event["event_type"] == "blocked_auto_repush_recovered"
        for event in store.list_events(task_id=task["task_id"])
    )


def test_blocked_failure_recovery_and_second_sla_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_BLOCKED_FIRST_SLA", "10")
    monkeypatch.setenv("HERDR_BLOCKED_SECOND_SLA", "10")
    monkeypatch.setenv("HERDR_BLOCKED_COOLDOWN", "1")
    monkeypatch.setenv("HERDR_BLOCKED_JITTER_WINDOW", "0.1")
    controller, attention_file = load_controller_for_sla(tmp_path, "ctrl-sla-recovery")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sla-recovery", status="blocked")
    episode_store = liveness.EpisodeStore(attention_file)
    key = controller._blocked_sla_key(task["task_id"])
    first_now = task["updated_at"] + 11.0
    seed_blocked_episode(store, episode_store, task, key, 10.0)

    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "_attention_store", episode_store
    ):
        failed = controller.process_blocked_sla_task(
            task,
            now=first_now,
            send_prompt=lambda _task, _decision: (False, "transport down"),
        )
        assert failed["action"] == "repush"
        recovered = controller.process_blocked_sla_task(
            task,
            now=first_now + 2.0,
            send_prompt=lambda _task, _decision: (True, "recovered"),
        )
        assert recovered["action"] == "repush_recover"

        episode = episode_store.get(key)
        episode["active_seconds"] = 30.0
        episode["last_action_at"] = first_now + 2.0
        episode_store.upsert(key, episode)
        with patch.object(controller, "_notify_blocked_human_upgrade", return_value=True):
            escalated = controller.process_blocked_sla_task(
                task, now=first_now + 4.0
            )
        assert escalated["action"] == "escalate"

        # Repeated sweeps after both budgets are consumed do not send or
        # notify again.
        episode = episode_store.get(key)
        episode["active_seconds"] = 100.0
        episode["last_action_at"] = first_now + 4.0
        episode_store.upsert(key, episode)
        with patch.object(controller, "_notify_blocked_human_upgrade") as notify:
            bounded = controller.process_blocked_sla_task(
                task, now=first_now + 100.0
            )
        assert bounded["action"] == "suppressed_bounds"
        notify.assert_not_called()

    event_types = [event["event_type"] for event in store.list_events(task_id=task["task_id"])]
    assert event_types.count("prompt_delivery_failed") == 1
    assert event_types.count("blocked_repush_failed") == 1
    assert event_types.count("blocked_auto_repush_recovered") == 1
    assert event_types.count("blocked_human_escalated") == 1


def test_blocked_sla_scheduler_is_bounded_and_deduplicated(tmp_path):
    controller = load_script("ctrl-sla-scheduler-fix3", "services/herdr-controller.py")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_step(_task, now=None):
        started.set()
        release.wait(5)
        finished.set()

    with patch.object(controller, "process_blocked_sla_task", side_effect=slow_step):
        task = {"task_id": "task-sla-scheduler", "status": "blocked"}
        assert controller.schedule_blocked_sla_task(task, now=100.0) is True
        assert controller.schedule_blocked_sla_task(task, now=101.0) is False
        assert started.wait(5)
        release.set()
        assert finished.wait(5)


def test_blocked_claim_revalidates_task_before_prompt_transport(tmp_path):
    controller, attention_file = load_controller_for_sla(tmp_path, "ctrl-sla-revalidate")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sla-revalidate", status="blocked")
    episode_store = liveness.EpisodeStore(attention_file)
    key = controller._blocked_sla_key(task["task_id"])
    now = task["updated_at"] + 2000.0
    seed_blocked_episode(store, episode_store, task, key, blocked_sla.first_sla_seconds())
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "_attention_store", episode_store
    ):
        _episode, decision = controller._claim_blocked_sla_action(task, now)
        assert decision["claimed"] is True
        store.update_task_metadata(task["task_id"], {"human_touch": "reopen"})
        current, reason = controller._blocked_task_claim_is_current(task, decision)
        assert current is False
        assert reason == "task_version_changed"
        released = controller._release_stale_blocked_claim(task, decision, reason)
        assert released["action"] == "suppressed_stale_task"
    assert episode_store.get(key)["repush_state"] == "pending"
    assert not any(
        event["event_type"] == "blocked_auto_repush"
        for event in store.list_events(task_id=task["task_id"])
    )


def test_human_escalation_delivery_failure_can_use_one_bounded_retry(tmp_path):
    controller, attention_file = load_controller_for_sla(tmp_path, "ctrl-sla-escalation-retry")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sla-escalation-retry", status="blocked")
    episode_store = liveness.EpisodeStore(attention_file)
    key = controller._blocked_sla_key(task["task_id"])
    now = task["updated_at"] + 2000.0
    episode_store.upsert(
        key,
        {
            "task_id": task["task_id"],
            "workflow_id": task["workflow_id"],
            "entry_updated_at": task["updated_at"],
            "entry_version": task["version"],
            "active_seconds": blocked_sla.first_sla_seconds() + blocked_sla.second_sla_seconds(),
            "last_tick_at": now - 1,
            "repushes": 1,
            "repush_state": "delivered",
            "human_escalations": 0,
            "last_action_at": now - 1000,
            "episode_id": "episode-escalation-retry",
        },
    )
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "_attention_store", episode_store
    ), patch.object(
        controller, "_notify_blocked_human_upgrade", return_value=False
    ) as notify:
        first = controller.process_blocked_sla_task(task, now=now)
        assert first["action"] == "escalate"
        second = controller.process_blocked_sla_task(task, now=now + 601)
        assert second["action"] == "escalate"
        third = controller.process_blocked_sla_task(task, now=now + 1202)
        assert third["action"] == "suppressed_bounds"
    assert notify.call_count == 2
    event_types = [event["event_type"] for event in store.list_events(task_id=task["task_id"])]
    assert event_types.count("blocked_human_escalation_failed") == 2
    assert "blocked_human_escalated" not in event_types


def test_corrupt_attention_ledger_fails_closed_instead_of_reusing_budget(tmp_path):
    controller, attention_file = load_controller_for_sla(tmp_path, "ctrl-sla-corrupt")
    store = SQLiteStateStore(tmp_path / "state.db")
    task = seed_task(store, "task-sla-corrupt", status="blocked")
    episode_store = liveness.EpisodeStore(attention_file)
    key = controller._blocked_sla_key(task["task_id"])
    now = task["updated_at"] + 2000.0
    seed_blocked_episode(store, episode_store, task, key, blocked_sla.first_sla_seconds())
    attention_file.write_text("{not-json", encoding="utf-8")
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        controller, "_attention_store", episode_store
    ), pytest.raises(RuntimeError, match="episode ledger"):
        controller.process_blocked_sla_task(
            task, now=now, send_prompt=lambda _task, _decision: (True, "must not send")
        )


# ---------------------------------------------------------------------------
# FR-4: explicit identity, supersession and invalidation are fail-closed
# ---------------------------------------------------------------------------


def test_duplicate_delivery_identity_with_different_payload_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    first = append_delivery("wf-delivery-conflict", "candidate-a", "sha-a")
    second = append_delivery(
        "wf-delivery-conflict",
        "candidate-a",
        "sha-b",
        test_gate="different-gate",
    )
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery([first, second])


def test_delivery_identity_does_not_cross_workflow_scope(tmp_path, monkeypatch):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    first = append_delivery("wf-delivery-a", "candidate-shared", "sha-a")
    second = append_delivery("wf-delivery-b", "candidate-shared", "sha-b")
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery([first, second])


def test_fix_loop_invalidation_is_candidate_scoped_and_not_self_invalidating(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    first = append_delivery(
        "wf-delivery-invalidation", "candidate-a", "sha-a", base="same-base"
    )
    second = append_delivery(
        "wf-delivery-invalidation", "candidate-b", "sha-b", base="same-base"
    )
    invalidation = workflow_docs.append_note(
        "wf-delivery-invalidation",
        kind="invalidation",
        title="fix-loop candidate-b",
        node="wrapup",
        base_sha="same-base",
        invalidates=["wrapup"],
        fields={"invalidated_candidates": ["candidate-b"]},
    )
    annotated = workflow_docs.annotate_notes([first, second, invalidation])
    by_id = {item.get("delivery_id"): item for item in annotated}
    assert by_id["candidate-a"]["stale"] is False
    assert by_id["candidate-b"]["stale"] is True
    assert invalidation.get("stale") is not True


def test_replayed_identical_delivery_is_idempotent_but_distinct_candidates_fail(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    first = append_delivery("wf-delivery-replay", "candidate-a", "sha-a")
    replay = append_delivery("wf-delivery-replay", "candidate-a", "sha-a")
    assert delivery_record.select_effective_delivery([first, replay])["delivery_id"] == "candidate-a"

    other = append_delivery("wf-delivery-replay", "candidate-b", "sha-b")
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery([first, replay, other])


def test_concurrent_delivery_append_is_visible_and_ambiguity_is_fail_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        notes = list(
            pool.map(
                lambda item: append_delivery("wf-delivery-concurrent", *item),
                [("candidate-a", "sha-a"), ("candidate-b", "sha-b")],
            )
        )
    assert len(workflow_docs.load_notes("wf-delivery-concurrent")) == 2
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery(notes)


def test_delivery_cli_rejects_unknown_supersede_and_stale_replay(tmp_path, monkeypatch):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    module = load_script("herdr-task-delivery-fix3", "bin/herdr-task")
    first = module.record_delivery_note(
        "wf-delivery-cli",
        "agent/opencode/fix3",
        "sha-cli",
        "review-cli",
        "test-cli",
        base="base-cli",
    )
    replay = module.record_delivery_note(
        "wf-delivery-cli",
        "agent/opencode/fix3",
        "sha-cli",
        "review-cli",
        "test-cli",
        base="base-cli",
    )
    assert replay["note_id"] == first["note_id"]
    with pytest.raises(SystemExit):
        module.record_delivery_note(
            "wf-delivery-cli",
            "agent/opencode/fix3",
            "sha-cli",
            "review-cli",
            "different-gate",
            base="base-cli",
        )
    with pytest.raises(SystemExit):
        module.record_delivery_note(
            "wf-delivery-cli",
            "agent/opencode/fix3",
            "sha-new",
            "review-cli",
            "test-cli",
            base="base-cli",
            supersedes="candidate-does-not-exist",
        )


def test_malformed_delivery_note_is_not_an_eligible_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    malformed = workflow_docs.append_note(
        "wf-delivery-malformed", kind="delivery", title="malformed", node="wrapup"
    )
    with pytest.raises(delivery_record.DeliveryAmbiguityError):
        delivery_record.select_effective_delivery([malformed])


def test_concurrent_record_delivery_has_one_winner_and_no_false_success(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    module = load_script("herdr-task-delivery-race-fix3", "bin/herdr-task")

    def record(candidate):
        try:
            return module.record_delivery_note(
                "wf-delivery-race",
                "agent/opencode/race",
                candidate,
                "review-race",
                "test-race",
                base="base-race",
            )
        except SystemExit as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, ["sha-race-a", "sha-race-b"]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(item == 2 for item in results) == 1
    assert len(workflow_docs.load_notes("wf-delivery-race")) == 1


def test_router_isolation_failure_recovers_after_pool_becomes_eligible(tmp_path):
    controller = load_script("ctrl-router-recovery-fix3", "services/herdr-controller.py")
    from herdr import agent_router
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-router-recovery", "status": "running"})
    store.save_task({
        "task_id": "review-router-failed",
        "workflow_id": "wf-router-recovery",
        "node": "review",
        "stage": "review",
        "agent": "auto",
        "status": "failed",
        "failure_reason": "router_isolation_rejected",
        "task_type": "test",
    })
    completed = subprocess.CompletedProcess([], 0, "", "")
    with patch.object(controller, "_get_store", return_value=store), patch.object(
        agent_router, "choose_agent", return_value="claude"
    ), patch.object(controller.subprocess, "run", return_value=completed) as run:
        assert controller.recover_router_isolation_tasks(
            "wf-router-recovery", store.list_tasks()
        ) is True
    command = run.call_args.args[0]
    assert command[1:3] == ["supersede", "review-router-failed"]


def test_unknown_workflow_does_not_create_ghost_failed_task(tmp_path, monkeypatch):
    module = load_script("herdr-task-unknown-workflow-fix3", "bin/herdr-task")
    os.environ["HERDR_STATE_DB"] = str(tmp_path / "state.db")
    args = SimpleNamespace(
        task_id="ghost-task", workflow_id="missing-workflow", node="review",
        stage=None, source=str(tmp_path), integration_mode="none", agent="auto",
        task_type="review", goal="review", acceptance=[], prompt="review",
        run_id="run-ghost", agent_policy=None, allow_reuse=False,
        reuse_reason="", onto=None,
    )
    with pytest.raises(SystemExit) as exc:
        module._launch_task(args)
    assert exc.value.code == 2
    assert SQLiteStateStore(tmp_path / "state.db").get_task("ghost-task") is None


def test_ambiguous_delivery_preflight_refuses_before_pane(tmp_path, monkeypatch):
    module, store, root = make_router_lifecycle(tmp_path, monkeypatch)

    monkeypatch.setenv(workflow_docs.DOCS_DIR_ENV, str(tmp_path / "docs"))
    append_delivery("wf-empty-review", "candidate-a", "sha-a")
    append_delivery("wf-empty-review", "candidate-b", "sha-b")
    pools = root / "pools.json"
    pools.write_text(
        json.dumps({
            "projects": {
                "project-empty-review": {
                    "allowed_agents": ["claude"],
                    "disabled_agents": [],
                    "stage_preferences": {"review": ["claude"]},
                    "task_type_preferences": {"review": ["claude"]},
                }
            }
        }),
        encoding="utf-8",
    )
    args = launch_args(root, "review-delivery-ambiguous")
    with patch.object(module, "ensure_stage_topology", side_effect=AssertionError("topology called")), patch.object(
        module, "acquire_pane_for_task", side_effect=AssertionError("pane called")
    ), pytest.raises(SystemExit) as exc:
        module._launch_task(args)
    assert exc.value.code == 2
    assert store.get_task("review-delivery-ambiguous") is None
    assert not any(
        event["event_type"] == "task_created"
        for event in store.list_events(task_id="review-delivery-ambiguous")
    )


# ---------------------------------------------------------------------------
# FR-5: the real command dispatcher keeps the legacy --force contract
# ---------------------------------------------------------------------------


def test_cli_dispatches_legacy_force_without_a_second_confirmation_argument(monkeypatch):
    module = load_script("herdr-task-force-fix3", "bin/herdr-task")
    captured = {}

    def fake_close(workflow_id, **kwargs):
        captured["workflow_id"] = workflow_id
        captured.update(kwargs)
        return {"workflow_id": workflow_id, "tasks": []}

    monkeypatch.setattr(module, "resolve_workflow_id_for_close", lambda _value: "wf-force")
    monkeypatch.setattr(module, "close_workflow", fake_close)
    monkeypatch.setattr(
        sys,
        "argv",
        ["herdr-task", "close-workflow", "wf-force", "--force"],
    )
    module.main()
    assert captured["force"] is True
    assert "confirm_force" not in captured


def test_router_failure_lifecycle_does_not_report_success_when_event_write_fails(
    tmp_path, monkeypatch
):
    module, store, root = make_router_lifecycle(tmp_path, monkeypatch)

    class EventFailingStore:
        def __getattr__(self, name):
            return getattr(store, name)

        def record_event(self, *_args, **_kwargs):
            raise OSError("event disk full")

    args = launch_args(root, "review-event-failure")
    monkeypatch.setattr(module, "_get_store", lambda: EventFailingStore())
    with pytest.raises(RuntimeError, match="router failure lifecycle"):
        module._record_router_failure_task(
            args, "review", RuntimeError("empty review pool")
        )
    assert store.get_task("review-event-failure")["status"] == "failed"


# ---------------------------------------------------------------------------
# FR-6: real router/CLI lifecycle and audit failure boundary
# ---------------------------------------------------------------------------


def make_router_lifecycle(tmp_path, monkeypatch):
    from herdr import agent_router

    root = tmp_path
    os.environ["HERDR_STATE_DB"] = str(root / "state.db")
    os.environ["TASKS_FILE"] = str(root / "tasks.json")
    os.environ["WORKFLOWS_FILE"] = str(root / "workflows.json")
    workflow_file = root / "workflow.json"
    workflow_file.write_text(
        json.dumps({"nodes": [{"id": "review", "agent_policy": {}}]}),
        encoding="utf-8",
    )
    store = SQLiteStateStore(root / "state.db")
    store.save_workflow(
        {
            "workflow_id": "wf-empty-review",
            "status": "running",
            "project_id": "project-empty-review",
            "project_root": str(root),
            "execution": {"mode": "git"},
            "workflow_file": str(workflow_file),
        }
    )
    store.save_task(
        {
            "task_id": "implementation-used-agent",
            "workflow_id": "wf-empty-review",
            "run_id": "run-empty-review",
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        }
    )
    pools = root / "pools.json"
    pools.write_text(
        json.dumps(
            {
                "projects": {
                    "project-empty-review": {
                        "allowed_agents": ["codex"],
                        "disabled_agents": [],
                        "stage_preferences": {"review": ["codex"]},
                        "task_type_preferences": {"review": ["codex"]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_router, "POOLS_FILE", pools)
    monkeypatch.setattr(agent_router, "ROUTER_LOCK_FILE", root / "router.lock")
    monkeypatch.setattr(agent_router, "RESERVATIONS_FILE", root / "reservations.json")
    return load_script("herdr-task-empty-review-fix3", "bin/herdr-task"), store, root


def launch_args(root: Path, task_id: str, **extra):
    values = {
        "task_id": task_id,
        "workflow_id": "wf-empty-review",
        "run_id": f"run-{task_id}",
        "node": "review",
        "stage": None,
        "agent": "auto",
        "task_type": "review",
        "integration_mode": "git",
        "onto": None,
        "source": str(root),
        "goal": "review",
        "acceptance": [],
        "prompt": "review",
        "test_cmd": None,
        "lint_cmd": None,
        "repro_cmd": None,
        "agent_policy": None,
        "allow_reuse": False,
        "reuse_reason": "",
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_empty_review_pool_has_one_failed_task_and_no_pane_before_topology(tmp_path, monkeypatch):
    module, store, root = make_router_lifecycle(tmp_path, monkeypatch)
    args = launch_args(root, "review-empty")
    with patch.object(module, "ensure_stage_topology", side_effect=AssertionError("topology called")), patch.object(
        module, "acquire_pane_for_task", side_effect=AssertionError("pane called")
    ), pytest.raises(SystemExit) as exc:
        module._launch_task(args)
    assert exc.value.code == 2

    task = store.get_task("review-empty")
    assert task is not None
    assert task["status"] == "failed"
    assert task["pane_id"] in (None, "")
    assert task["failure_reason"] == "router_isolation_rejected"
    events = store.list_events(task_id="review-empty")
    assert [event["event_type"] for event in events] == ["router_isolation_rejected"]
    assert events[0]["payload"]["pane_dispatched"] is False
    workflow = store.get_workflow("wf-empty-review")
    assert workflow["last_dispatch_failure"]["task_id"] == "review-empty"
    assert workflow["last_dispatch_failure"]["run_id"] == "run-review-empty"
    assert workflow["last_dispatch_failure"]["pane_dispatched"] is False


def test_successful_opt_out_records_one_scoped_audit_before_selection(tmp_path, monkeypatch):
    from herdr import agent_router

    _module, store, _root = make_router_lifecycle(tmp_path, monkeypatch)
    monkeypatch.setattr(
        agent_router,
        "workflow_config_for",
        lambda _workflow_id: {
            "nodes": [{
                "id": "review",
                "agent_policy": {
                    "allow_reuse_implementation_agents": True,
                    "reuse_reason": "controlled emergency review",
                },
            }]
        },
    )
    selected = agent_router.choose_agent(
        "wf-empty-review",
        "review",
        "review",
        requested="codex",
        reservation_key="review-opt-out",
        run_id="run-opt-out",
    )
    assert selected == "codex"
    events = store.list_events(
        task_id="review-opt-out", event_type="router_opt_out_used"
    )
    assert len(events) == 1
    assert events[0]["run_id"] == "run-opt-out"
    assert events[0]["payload"]["task_id"] == "review-opt-out"
    assert events[0]["payload"]["audit_required"] is True


def test_cli_main_empty_review_exit_is_readable_from_state_store(tmp_path, monkeypatch):
    module, store, root = make_router_lifecycle(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "herdr-task", "launch",
            "--task-id", "review-cli-empty",
            "--workflow-id", "wf-empty-review",
            "--node", "review",
            "--source", str(root),
            "--task-type", "feat",
            "--integration-mode", "git",
            "--goal", "review",
            "--prompt", "review",
            "--run-id", "run-review-cli-empty",
        ],
    )
    with patch.object(module, "ensure_stage_topology", side_effect=AssertionError("topology called")), patch.object(
        module, "acquire_pane_for_task", side_effect=AssertionError("pane called")
    ), pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
    task = store.get_task("review-cli-empty")
    assert task["status"] == "failed"
    assert task["pane_id"] in (None, "")
    events = store.list_events(task_id="review-cli-empty")
    assert [event["event_type"] for event in events] == ["router_isolation_rejected"]
    assert events[0]["run_id"] == "run-review-cli-empty"


def test_opt_out_audit_failure_is_fail_closed_and_keeps_failure_identity(tmp_path, monkeypatch):
    from herdr import agent_router

    module, store, root = make_router_lifecycle(tmp_path, monkeypatch)
    real_store = store

    class AuditFailingStore:
        def __getattr__(self, name):
            return getattr(real_store, name)

        def record_event(self, event_type, *args, **kwargs):
            if event_type == "router_opt_out_used":
                raise OSError("audit disk full")
            return real_store.record_event(event_type, *args, **kwargs)

    monkeypatch.setattr(agent_router, "_get_store", lambda: AuditFailingStore())
    args = launch_args(
        root,
        "review-audit-failure",
        allow_reuse=True,
        reuse_reason="controlled emergency review",
    )
    with patch.object(module, "ensure_stage_topology", side_effect=AssertionError("topology called")), patch.object(
        module, "acquire_pane_for_task", side_effect=AssertionError("pane called")
    ), pytest.raises(SystemExit) as exc:
        module._launch_task(args)
    assert exc.value.code == 2
    task = store.get_task("review-audit-failure")
    assert task["status"] == "failed"
    assert task["pane_id"] in (None, "")
    assert "audit" in task["failure_detail"].lower()
    events = store.list_events(task_id="review-audit-failure")
    assert any(event["event_type"] == "router_isolation_rejected" for event in events)
    assert all(event["event_type"] != "router_opt_out_used" for event in events)
    assert events[-1]["run_id"] == "run-review-audit-failure"
    assert events[-1]["payload"]["task_id"] == "review-audit-failure"
    assert events[-1]["payload"]["failure_code"] == "router_opt_out_audit_failed"
