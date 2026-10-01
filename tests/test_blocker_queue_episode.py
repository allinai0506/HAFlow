"""Queued blockers belong to a durable transition episode, not just a status."""
import pytest
from tests.test_inner_loop_blocker_delivery import scene


@pytest.mark.parametrize("reason", ["recovery_blocked", "inner_loop_exhausted"])
def test_old_blocker_dropped_after_recovery_and_new_blocker(scene, reason):
    controller, store, item, transport = scene
    store.transition_task("task-blocker", "working", "cli_set_status")
    store.transition_task("task-blocker", "blocked", reason)
    controller._handle_coordinator_item(item)
    transport.assert_not_called()


def test_same_episode_metadata_save_does_not_drop_delivery(scene):
    controller, store, item, transport = scene
    task = store.get_task("task-blocker")
    task["diagnostic_note"] = "runtime observed"
    store.save_task(task)
    controller._handle_coordinator_item(item)
    transport.assert_called_once()


def test_new_episode_not_suppressed_or_removed_by_old_queue(scene):
    controller, store, old, transport = scene
    store.transition_task("task-blocker", "working", "cli_set_status")
    store.transition_task("task-blocker", "blocked", "inner_loop_exhausted")
    task = store.get_task("task-blocker")
    controller.enqueue_coordinator_event(task, "inner_loop_exhausted")
    assert controller.coordinator_queue.qsize() == 1
    new = controller.coordinator_queue.get_nowait()
    controller._handle_coordinator_item(old)
    transport.assert_not_called()
    controller.enqueue_coordinator_event(task, "inner_loop_exhausted")
    assert controller.coordinator_queue.empty(), "old cleanup must retain new dedup ownership"
    controller._handle_coordinator_item(new)
    transport.assert_called_once()


def test_run_change_rejects_old_blocker(scene):
    controller, store, item, transport = scene
    task = store.get_task("task-blocker")
    task["run_id"] = "different-run"
    store.save_task(task)
    controller._handle_coordinator_item(item)
    transport.assert_not_called()


def test_same_timestamp_transitions_still_start_new_episode(scene, monkeypatch):
    from herdr import state_db
    controller, store, item, transport = scene
    stamp = store.get_task("task-blocker")["status_history"][-1]["timestamp"]
    monkeypatch.setattr(state_db.time, "time", lambda: stamp)
    store.transition_task("task-blocker", "working", "cli_set_status")
    store.transition_task("task-blocker", "blocked", "inner_loop_exhausted")
    assert store.get_task("task-blocker")["status_history"][-1]["timestamp"] == stamp
    controller._handle_coordinator_item(item)
    transport.assert_not_called()


def test_waiting_event_rechecks_episode_after_busy(scene, monkeypatch):
    controller, store, item, transport = scene
    statuses = iter(["working", "idle"])
    monkeypatch.setattr(controller, "coordinator_status", lambda _: next(statuses))
    def recover_and_block(_seconds):
        store.transition_task("task-blocker", "working", "cli_set_status")
        store.transition_task("task-blocker", "blocked", "inner_loop_exhausted")
    monkeypatch.setattr(controller.time, "sleep", recover_and_block)
    controller._handle_coordinator_item(item)
    transport.assert_not_called()


@pytest.mark.parametrize("reblock", [False, True])
def test_message_build_recovery_rechecked_before_send(scene, monkeypatch, reblock):
    controller, store, item, transport = scene
    original = controller.build_coordinator_message
    def build_then_recover(task, event_type):
        message = original(task, event_type)
        store.transition_task("task-blocker", "working", "cli_set_status")
        if reblock:
            store.transition_task("task-blocker", "blocked", "inner_loop_exhausted")
        return message
    monkeypatch.setattr(controller, "build_coordinator_message", build_then_recover)
    controller._handle_coordinator_item(item)
    transport.assert_not_called()


def test_legacy_metadata_change_requeues_current_blocker(scene):
    controller, store, _old, transport = scene
    task = store.get_task("task-blocker")
    task.pop("status_history")
    store.save_task(task)
    controller.queued_events.clear()
    controller.enqueue_coordinator_event(store.get_task("task-blocker"), "inner_loop_exhausted")
    old = controller.coordinator_queue.get_nowait()
    task = store.get_task("task-blocker")
    task["diagnostic_note"] = "legacy runtime update"
    store.save_task(task)
    controller._handle_coordinator_item(old)
    transport.assert_not_called()
    assert controller.coordinator_queue.qsize() == 1
    current = controller.coordinator_queue.get_nowait()
    controller._handle_coordinator_item(current)
    transport.assert_called_once()
