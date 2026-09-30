"""Runtime liveness does not resolve a pending exhausted-loop decision."""
from unittest.mock import Mock

import pytest
from tests.test_inner_loop_blocker_delivery import scene


@pytest.mark.parametrize('entry,runtime', [('event','working'), ('reconcile','working'), ('reconcile','done')])
def test_pending_arbitration_survives_runtime_signal_and_reaches_coordinator(scene, monkeypatch, entry, runtime):
    controller, store, item, transport = scene
    monkeypatch.delenv('HERDR_CONTROLLER_TEST', raising=False)  # Real CAS/persistence path.
    monkeypatch.setattr(controller, 'get_agent_runtime_status', lambda _: runtime)
    monkeypatch.setattr(controller, 'maybe_ack_on_working', Mock())
    before = store.get_task('task-blocker')
    if entry == 'event':
        controller.handle_event('task-blocker', runtime)
    else:
        controller.reconcile_task_state('task-blocker')
    after = store.get_task('task-blocker')
    assert after['status'] == 'blocked'
    assert after['version'] == before['version']
    controller._handle_coordinator_item(item)
    transport.assert_called_once()
    assert 'HERDR_CONTROLLER_BLOCKER_EVENT' in transport.call_args.args[0][4]


@pytest.mark.parametrize('entry', ['event','reconcile'])
def test_explicit_rework_allows_runtime_recovery(scene, monkeypatch, entry):
    controller, store, _item, _transport = scene
    monkeypatch.delenv('HERDR_CONTROLLER_TEST', raising=False)
    monkeypatch.setattr(controller, 'get_agent_runtime_status', lambda _: 'working')
    monkeypatch.setattr(controller, 'maybe_ack_on_working', Mock())
    store.transition_task('task-blocker', 'working', 'cli_set_status')
    store.transition_task('task-blocker', 'rework', 'cli_set_status')
    if entry == 'event':
        controller.handle_event('task-blocker', 'working')
    else:
        controller.reconcile_task_state('task-blocker')
    assert store.get_task('task-blocker')['status'] == 'working'


@pytest.mark.parametrize('entry', ['event','reconcile'])
def test_generic_blocker_recovery_ignores_old_exhaustion_metadata(scene, monkeypatch, entry):
    controller, store, _item, _transport = scene
    monkeypatch.delenv('HERDR_CONTROLLER_TEST', raising=False)
    monkeypatch.setattr(controller, 'get_agent_runtime_status', lambda _: 'working')
    monkeypatch.setattr(controller, 'maybe_ack_on_working', Mock())
    store.transition_task('task-blocker', 'working', 'cli_set_status')
    store.transition_task('task-blocker', 'blocked', 'recovery_blocked')
    task = store.get_task('task-blocker')
    assert task['sentinel_reason'] == 'inner_loop_exhausted'  # Stale metadata is retained.
    assert controller.blocked_event_type(task) == 'blocked'
    if entry == 'event':
        controller.handle_event('task-blocker', 'working')
    else:
        controller.reconcile_task_state('task-blocker')
    assert store.get_task('task-blocker')['status'] == 'working'


def test_restart_requeues_pending_arbitration_without_promoting_busy_task(scene, monkeypatch):
    controller, store, _old_item, transport = scene
    monkeypatch.delenv('HERDR_CONTROLLER_TEST', raising=False)
    monkeypatch.setattr(controller, 'get_agent_runtime_status', lambda _: 'working')
    controller.queued_events.clear()  # Emulate the volatile queue lost at restart.
    controller.reconcile_task_state('task-blocker')
    assert store.get_task('task-blocker')['status'] == 'blocked'
    item = controller.coordinator_queue.get_nowait()
    assert item['event_type'] == 'inner_loop_exhausted'
    controller._handle_coordinator_item(item)
    transport.assert_called_once()
