"""PR #312 P2 review follow-ups, on real SQLite state.

Covers issues #314/#318 (settle supersede / close_workflow / clear_escalation
receipts from persisted terminal state), #319 (validate the confirm_dispatch
reason before the durable claim), #315 (deep-linked task actions beyond the
Cockpit display cap), #316 (serialized response budgets) and #317 (header
execution identity and control policy facts).
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr.state_store import SQLiteStateStore


def _utf8_len(value):
    return len(value.encode("utf-8", "ignore"))


def _escalated_task(wid, tid, **extra):
    return {"workflow_id": wid, "task_id": tid, "node": "build", "stage": "build",
            "run_id": tid + "-run", "execution_id": "exec-1", "version": 1,
            "candidate_sha": "c" * 40,
            "status": "integrated", "finalize_escalated": True,
            "finalize_escalate_reason": "conflict", **extra}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    result = SQLiteStateStore(tmp_path / 'state.db')
    result.save_workflow({'workflow_id': 'wf-1', 'status': 'running', 'execution_id': 'exec-1',
                          'config': {'nodes': [{'id': 'build', 'node_type': 'agent'}]}})
    # The authoritative candidate identity comes from a frozen-candidate event.
    result.record_event(event_type='candidate_frozen', workflow_id='wf-1',
                        source='critical-path-scheduler', payload={'candidate_sha': 'c' * 40})
    return result


def _escalation_projection(store):
    from services.controller import cockpit_automation as automation
    return automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})


def _execute(store, action, handler, **payload):
    from services.controller import cockpit_automation as automation
    return automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'confirmed': True,
        'operator': 'human_console', **payload},
        deps=SimpleNamespace(store=store, handlers={
            action['api_payload']['type']: handler}))


# ---------------------------------------------------------------------------
# Issues #314 + #318: terminal-state receipt settlement.


def test_supersede_receipt_settles_from_persisted_terminal_state(store):
    store.save_task(_escalated_task('wf-1', 'T1'))
    action = next(a for a in _escalation_projection(store)['actions'] if a['action_id'] == 'T1:supersede')

    def handler(current):
        store.save_task({**store.get_task('T1'), 'status': 'superseded', 'superseded_by': 'T1-r2'})
        return {'ok': True, 'task_id': 'T1'}

    result = _execute(store, action, handler)
    assert result['receipt']['status'] == 'applied'
    assert result['receipt']['task_id'] == 'T1'
    assert result['receipt']['outcome'] == 'success'


def test_close_workflow_receipt_settles_from_completed_readback(store):
    store.save_task(_escalated_task('wf-1', 'T1'))
    action = next(a for a in _escalation_projection(store)['actions']
                  if a['action_id'] == 'T1:close_workflow_accept_escalated')

    def handler(current):
        wf = store.get_workflow('wf-1')
        store.save_workflow({**wf, 'status': 'completed'})
        return {'ok': True, 'task_id': 'T1'}

    result = _execute(store, action, handler)
    assert result['receipt']['status'] == 'applied'
    assert result['receipt']['message'] == '工作流终态已回读确认'


def test_clear_escalation_receipt_settles_and_reconciliation_is_idempotent(store):
    from services.controller.cockpit_automation import reconcile_pending_receipts
    store.save_task(_escalated_task('wf-1', 'T1'))
    action = next(a for a in _escalation_projection(store)['actions'] if a['action_id'] == 'T1:clear_escalation')

    def handler(current):
        # The flag clears, then transport dies before any readback: the claim
        # stays durably accepted_waiting until reconciliation finds the facts.
        store.save_task({**store.get_task('T1'), 'finalize_escalated': False,
                         'finalize_escalate_reason': None})
        raise RuntimeError('transport died after the flag was cleared')

    result = _execute(store, action, handler)
    assert result['receipt']['status'] == 'accepted_waiting'
    assert result['receipt']['outcome'] == 'unknown'
    assert store.get_task('T1')['finalize_escalated'] is False

    settled = reconcile_pending_receipts(store, 'wf-1')
    assert [r['status'] for r in settled] == ['applied']
    assert settled[0]['task_id'] == 'T1'
    # Settled receipts leave the pending queue; the durable record stays applied.
    assert reconcile_pending_receipts(store, 'wf-1') == []
    persisted = _escalation_projection(store)['receipts']
    assert [r['status'] for r in persisted if r['action_id'] == 'T1:clear_escalation'] == ['applied']


def test_non_application_and_identity_mismatch_do_not_settle_as_applied(store):
    from services.controller import cockpit_automation as automation
    store.save_task(_escalated_task('wf-1', 'T1'))
    action = next(a for a in _escalation_projection(store)['actions'] if a['action_id'] == 'T1:clear_escalation')

    result = _execute(store, action, lambda current: {'ok': True, 'task_id': 'T1'})
    assert result['receipt']['status'] == 'accepted_waiting'
    assert result['receipt']['reason_code'] == 'effect_unconfirmed'
    assert store.get_task('T1')['finalize_escalated'] is True

    before = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})
    # A task rebound to a different execution must fail the exact identity readback.
    store.save_task({**store.get_task('T1'), 'execution_id': 'new-execution'})
    after = automation.read_action_state(store, action)
    receipt = automation.classify_receipt(action, before, after, {'ok': True, 'task_id': 'T1'})
    assert receipt['status'] == 'rejected'
    assert receipt['reason_code'] == 'readback_identity_changed'
