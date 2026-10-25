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


# ---------------------------------------------------------------------------
# Issue #319: the blank confirm_dispatch reason can never strand a claim.


@pytest.fixture
def supervised_store(store):
    wf = store.get_workflow('wf-1')
    wf['config']['control_policy'] = {'mode': 'supervised'}
    store.save_workflow(wf)
    return store


def test_blank_reason_confirmed_dispatch_never_claims_and_retry_succeeds(supervised_store):
    from herdr import node_dispatch_store as dispatch
    from console.controller_cockpit_api import execute_controller_action, get_controller_cockpit
    op = dispatch.reconcile_workflow(supervised_store.db_path, 'wf-1', now=100)[0]
    assert op['status'] == 'waiting_human'
    action = next(a for a in get_controller_cockpit('wf-1')['actions']
                  if a['api_payload'].get('type') == 'confirm_dispatch')

    blank = execute_controller_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'reason': '   ',
        'confirmed': True, 'operator': 'human_console'})
    assert blank['ok'] is False
    assert blank['receipt']['status'] == 'rejected'
    assert blank['receipt']['reason_code'] == 'reason_required'
    # No durable claim exists and the operation is untouched.
    assert not any(r['status'] == 'accepted_waiting'
                   for r in get_controller_cockpit('wf-1')['receipts'])
    fresh = dispatch.operation_for_node(supervised_store.db_path, 'wf-1', 'build')
    assert fresh['version'] == op['version']
    assert not fresh['detail'].get('dispatch_confirmation')

    retry = execute_controller_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity'], 'reason': 'reviewed launch scope',
        'confirmed': True, 'operator': 'human_console'})
    assert retry['receipt']['status'] == 'applied'
    assert retry['receipt']['operation_id'] == fresh['id']


def test_console_requires_reason_for_dispatch_confirmation_ui():
    root = Path(__file__).parents[1]
    source = (root / 'console/static/console/scripts/controller-cockpit.js').read_text()
    assert "isDispatchConfirm=action.api_payload?.type==='confirm_dispatch'" in source
    assert '派发确认必须填写人工核查依据' in source
    assert "needsReason?'（必填）'" in source


# ---------------------------------------------------------------------------
# Issue #315: deep-linked task actions resolve beyond the display cap.


def test_deep_linked_task_actions_resolve_beyond_display_cap(store):
    for i in range(40):
        store.save_task(_escalated_task('wf-1', f'T{i:02d}'))
    plain = _escalation_projection(store)
    assert plain['actions_truncated']
    assert not any(a['target'].get('task_id') == 'T39' for a in plain['actions'])

    from services.controller import cockpit_automation as automation
    focused = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'}, target_task_id='T39')
    focused_ids = {a['action_id'] for a in focused['actions']}
    assert {'T39:clear_escalation', 'T39:close_workflow_accept_escalated', 'T39:supersede'} <= focused_ids
    assert all(a['target'].get('task_id') == 'T39' for a in focused['actions'])
    # The exact resolution stays bounded by the same display cap.
    assert len(focused['actions']) <= 100

    # Stale and foreign targets resolve nothing and never borrow another task's cards.
    store.save_task({**store.get_task('T00'), 'status': 'superseded', 'superseded_by': 'T00-r2'})
    stale = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'}, target_task_id='T00')
    assert stale['actions'] == []
    foreign = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'},
                                      target_task_id='other-workflow-task')
    assert foreign['actions'] == []


def test_console_cockpit_passes_target_task_lookup(store):
    from console.controller_cockpit_api import get_controller_cockpit
    for i in range(40):
        store.save_task(_escalated_task('wf-1', f'T{i:02d}'))
    payload = get_controller_cockpit('wf-1', target_task_id='T39')
    assert {a['target'].get('task_id') for a in payload['actions']} == {'T39'}


# ---------------------------------------------------------------------------
# Issue #316: serialized budgets with explicit truncation markers.


def test_single_oversized_field_is_truncated_with_explicit_marker(store):
    from herdr.controller_cockpit import MAX_FIELD_BYTES
    store.save_task(_escalated_task('wf-1', 'T1', goal='x' * 100_000,
                                    diagnostics={'log': 'y' * 50_000}))
    projection = _escalation_projection(store)
    task = next(t for t in projection['tasks'] if t['task_id'] == 'T1')
    assert _utf8_len(task['goal']) == MAX_FIELD_BYTES
    assert {'field': 'goal', 'omitted_bytes': 100_000 - MAX_FIELD_BYTES} in task['_truncated_fields']
    assert task['diagnostics']['truncated'] is True
    assert task['diagnostics']['omitted_bytes'] >= 50_000
    # Identity and safety facts stay complete.
    assert task['status'] == 'integrated'
    assert task['run_id'] == 'T1-run'
    assert task['execution_id'] == 'exec-1'
    assert task['finalize_escalated'] is True


def test_aggregate_oversized_task_rows_stay_bounded(store):
    from herdr.controller_cockpit import MAX_TASK_BYTES
    payload = {f'note_{i:02d}': 'n' * 1500 for i in range(40)}
    store.save_task(_escalated_task('wf-1', 'T1', **payload))
    projection = _escalation_projection(store)
    task = next(t for t in projection['tasks'] if t['task_id'] == 'T1')
    assert _utf8_len(json.dumps(task, ensure_ascii=False, default=str)) <= MAX_TASK_BYTES
    truncated_fields = {entry['field'] for entry in task['_truncated_fields']}
    assert truncated_fields
    assert task['task_id'] == 'T1' and task['status'] == 'integrated'


def test_oversized_workflow_metadata_stays_bounded_with_identity_intact(store):
    from herdr.controller_cockpit import MAX_FIELD_BYTES, MAX_WORKFLOW_BYTES
    wf = store.get_workflow('wf-1')
    wf['notes'] = 'w' * 100_000
    store.save_workflow(wf)
    projection = _escalation_projection(store)
    assert _utf8_len(projection['workflow']['notes']) == MAX_FIELD_BYTES
    assert projection['workflow']['workflow_id'] == 'wf-1'
    assert projection['workflow']['status'] == 'running'
    assert projection['workflow']['execution_id'] == 'exec-1'
    assert projection['workflow']['candidate_sha'] == 'c' * 40
    assert _utf8_len(json.dumps(projection['workflow'], ensure_ascii=False, default=str)) <= MAX_WORKFLOW_BYTES


def test_response_budget_sheds_optional_sections_then_fails_closed(store, monkeypatch):
    import herdr.controller_cockpit as cockpit_module
    from services.controller import cockpit_automation as automation
    store.save_task(_escalated_task('wf-1', 'T1'))
    for _ in range(150):
        store.record_event(event_type='test.fact', workflow_id='wf-1', task_id='T1',
                           payload={'blob': 'z' * 2000})
    monkeypatch.setattr(cockpit_module, 'MAX_RESPONSE_BYTES', 120_000)
    projection = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})
    assert projection.get('response_shed') == ['timeline_payloads']
    assert all('payload' not in event for event in projection['timeline'])
    assert projection['read_state'] == 'confirmed'

    monkeypatch.setattr(cockpit_module, 'MAX_RESPONSE_BYTES', 1_000)
    projection = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})
    assert projection['read_state'] == 'unknown'
    assert projection['errors'].get('response_budget') == 'exceeded'


# ---------------------------------------------------------------------------
# Issue #317: authoritative identity and policy in the projection header.


def test_projection_exposes_resolved_policy_and_controller_version(store):
    from services.controller import cockpit_automation as automation
    wf = store.get_workflow('wf-1')
    wf['config']['control_policy'] = {'mode': 'supervised'}
    store.save_workflow(wf)
    projection = automation.read_cockpit(store, 'wf-1', controller={'status': 'running', 'version': 'sha-abc'})
    assert projection['control_policy'] == {'mode': 'confirmation_required', 'source': 'workflow'}
    assert projection['controller']['version'] == 'sha-abc'
    assert projection['workflow']['execution_id'] == 'exec-1'


def test_projection_policy_stays_unknown_without_authoritative_config(store):
    from herdr.controller_cockpit import build_cockpit_projection
    projection = build_cockpit_projection(workflow={'workflow_id': 'wf-1'}, config=None, tasks=None,
                                          graph=None, actions=None, recovery=None, controller=None,
                                          events=None)
    assert projection['control_policy'] is None
    assert projection['read_state'] == 'unknown'


def test_console_scripts_wire_target_lookup_header_facts_and_reason():
    root = Path(__file__).parents[1]
    cockpit = (root / 'console/static/console/scripts/controller-cockpit.js').read_text()
    reader = (root / 'console/static/console/scripts/workflow-reader.js').read_text()
    assert "cockpitTargetLookup" in reader and "target_task_id='+encodeURIComponent" in reader
    assert "cockpitTargetLookup(wid)" in cockpit
    assert '执行身份' in cockpit and '控制策略' in cockpit and '候选 / 回合' in cockpit
    assert "c.version||'未确认'" in cockpit and "wf.execution_id||'未确认'" in cockpit
