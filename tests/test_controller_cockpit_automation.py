"""Controller-owned execution and restart-safe receipts in real SQLite."""
from types import SimpleNamespace
import pytest
from herdr.state_store import SQLiteStateStore


def _race_resume(db_path, barrier, queue):
    from services.controller import cockpit_automation as automation
    selected = SQLiteStateStore(db_path)
    first = True
    def project(wid):
        nonlocal first
        result = automation.read_cockpit(selected, wid, controller={'status': 'running'})
        if first:
            first = False
            barrier.wait(timeout=10)
        return result
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': 'wf-1:resume_workflow',
        'expected_identity': {'execution_id': 'exec-1'}}, deps=deps_for(selected, project=project), automatic=True)
    queue.put((result['receipt']['status'], result.get('duplicate', False)))


@pytest.fixture
def store(tmp_path):
    result = SQLiteStateStore(tmp_path / 'state.db')
    result.save_workflow({'workflow_id': 'wf-1', 'status': 'paused', 'execution_id': 'exec-1',
                          'config': {'nodes': [{'id': 'build', 'node_type': 'agent'}]}})
    return result


def deps_for(store, **kw):
    return SimpleNamespace(store=store, **kw)


def test_controller_executes_without_http_and_reads_durable_applied_receipt(store):
    from services.controller.cockpit_automation import run_automatic_controller_actions, read_cockpit
    receipts = run_automatic_controller_actions('wf-1', deps=deps_for(store))
    assert receipts[0]['status'] == 'applied'
    assert store.get_workflow('wf-1')['status'] == 'running'
    assert read_cockpit(store, 'wf-1', controller={'status': 'running'})['receipts'][0] == receipts[0]


def test_confirmation_remains_pending_human_action(store):
    from services.controller.cockpit_automation import run_automatic_controller_actions, read_cockpit
    wf = store.get_workflow('wf-1')
    wf['config']['control_policy'] = {'mode': 'supervised'}
    store.save_workflow(wf)
    assert run_automatic_controller_actions('wf-1', deps=deps_for(store)) == []
    assert store.get_workflow('wf-1')['status'] == 'paused'
    assert read_cockpit(store, 'wf-1', controller={'status': 'running'})['todos'][0]['kind'] == 'action'


def test_claim_survives_crash_and_does_not_resend_after_restart(store):
    from services.controller.cockpit_automation import run_automatic_controller_actions, read_cockpit
    calls = []
    def crash(action):
        calls.append(action['action_id'])
        raise KeyboardInterrupt('process ended after external effect')
    with pytest.raises(KeyboardInterrupt):
        run_automatic_controller_actions('wf-1', deps=deps_for(store, handlers={'resume': crash}))
    store.update_workflow_metadata('wf-1', {'unrelated_note': 'reviewed after interruption'})
    reopened = SQLiteStateStore(store.db_path)
    assert run_automatic_controller_actions('wf-1', deps=deps_for(reopened, handlers={'resume': crash})) == []
    assert calls == ['wf-1:resume_workflow']
    receipt = read_cockpit(reopened, 'wf-1', controller={'status': 'running'})['receipts'][0]
    assert receipt['status'] == 'accepted_waiting'
    assert receipt['outcome'] == 'claimed'


def test_identity_changes_after_projection_are_rejected_before_handler(store):
    from services.controller.cockpit_automation import execute_action, read_cockpit
    action = read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    store.update_workflow_metadata('wf-1', {'execution_id': 'new-execution'})
    result = execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
                             'expected_identity': action['expected_identity']}, deps=deps_for(store))
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['refresh_required']
    assert store.get_workflow('wf-1')['status'] == 'paused'


def test_candidate_publication_after_projection_rejects_stale_action(store):
    from services.controller import cockpit_automation as automation
    action = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    store.record_event('candidate_frozen', {'candidate_sha': 'a' * 40}, workflow_id='wf-1',
                        source='critical-path-scheduler')
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']}, deps=deps_for(store), automatic=True)
    assert result['receipt']['status'] == 'rejected'
    assert result['receipt']['reason_code'] == 'identity_changed'
    assert store.get_workflow('wf-1')['status'] == 'paused'


@pytest.mark.parametrize('change', [{'is_destructive': True}, {'risk': 'evidence_bypass'},
                                  {'category': 'bypass'}, {'api_payload': {'type': 'force_pass'}}])
def test_automatic_boundary_rejects_unsafe_even_if_projection_says_automatic(store, change):
    from services.controller.cockpit_automation import execute_action, read_cockpit
    action = read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    action.update(change)
    def projection(wid):
        return {'workflow': store.get_workflow(wid), 'actions': [action], 'recovery': [], 'tasks': []}
    result = execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
                             'expected_identity': action['expected_identity']},
                            deps=deps_for(store, project=projection), automatic=True)
    assert result['receipt']['status'] == 'rejected'
    assert store.get_workflow('wf-1')['status'] == 'paused'


def test_unregistered_handler_does_not_claim_success(store):
    from services.controller.cockpit_automation import execute_action, read_cockpit
    action = read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    result = execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
                             'expected_identity': action['expected_identity']},
                            deps=deps_for(store, handlers={}), automatic=True)
    assert result['receipt']['reason_code'] == 'unsupported_handler'
    assert store.get_workflow('wf-1')['status'] == 'paused'


def test_registry_keeps_continuation_fix_stage_order_and_schedules_controller_execution(store, monkeypatch):
    from services.controller import registry, cockpit_automation as automation
    trace = []
    def schedule(selected, ids):
        trace.append('automation')
        automation.run_automatic_controller_actions('wf-1', deps=deps_for(selected))
    monkeypatch.setattr(automation, 'schedule_automatic_controller_actions', schedule)
    original = automation.recovery_store.reconcile
    def reconcile(*args, **kwargs):
        trace.append('recovery')
        return original(*args, **kwargs)
    monkeypatch.setattr(automation.recovery_store, 'reconcile', reconcile)
    ports = SimpleNamespace(time=SimpleNamespace(time=lambda: 100), _continuation_scan_at=0,
        active_registered_workflows=lambda: ['wf-1'], _get_store=lambda: store,
        schedule_workflow_continuations=lambda ids, now: trace.append('continuation'),
        redeliver_pending_fix_loop=lambda wid: trace.append('fix'),
        check_workflow_stage_advance=lambda wid: trace.append('stage'))
    registry.check_all_workflows_stage_advance(ports)
    assert trace == ['continuation', 'fix', 'stage', 'automation', 'recovery']
    assert store.get_workflow('wf-1')['status'] == 'running'


def test_handler_failure_has_durable_uncertain_receipt_and_preserves_workflow(store):
    from services.controller.cockpit_automation import run_automatic_controller_actions, read_cockpit
    def fail(action):
        raise OSError('transport unavailable')
    receipts = run_automatic_controller_actions('wf-1', deps=deps_for(store, handlers={'resume': fail}))
    assert receipts[0]['status'] == 'accepted_waiting'
    assert receipts[0]['outcome'] == 'unknown'
    assert read_cockpit(store, 'wf-1', controller={'status': 'running'})['receipts'][0] == receipts[0]
    assert store.get_workflow('wf-1')['status'] == 'paused'


def test_claim_revalidates_identity_before_external_effect(store, monkeypatch):
    from services.controller import cockpit_automation as automation
    action = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    original = automation.persist_receipt
    def claim_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('claim'):
            store.update_workflow_metadata('wf-1', {'execution_id': 'changed-after-claim'})
        return result
    monkeypatch.setattr(automation, 'persist_receipt', claim_then_change)
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']}, deps=deps_for(store), automatic=True)
    assert result['receipt']['status'] == 'rejected'
    assert store.get_workflow('wf-1')['status'] == 'paused'


def test_two_processes_compete_for_one_durable_execution_claim(store):
    import multiprocessing
    ctx = multiprocessing.get_context('spawn')
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_race_resume, args=(str(store.db_path), barrier, queue)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sum(duplicate for status, duplicate in results) == 1
        assert store.get_workflow('wf-1')['status'] == 'running'
        transitions = store.list_events(workflow_id='wf-1', event_type='workflow_transition')
        assert len(transitions) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()


def test_workflow_mutation_rechecks_execution_inside_write_transaction(store):
    from services.controller import cockpit_automation as automation
    action = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    real_resume = automation.default_handlers(store)['resume']
    def race(action):
        store.update_workflow_metadata('wf-1', {'execution_id': 'replacement-execution'})
        return real_resume(action)
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']}, deps=deps_for(store, handlers={'resume': race}))
    assert result['receipt']['status'] == 'rejected'
    assert store.get_workflow('wf-1')['status'] == 'paused'


def test_default_automatic_work_is_bounded_per_tick(store):
    from services.controller import cockpit_automation as automation
    from copy import deepcopy
    action = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    actions = [{**deepcopy(action), 'action_id': f'resume-{i}'} for i in range(9)]
    def project(wid):
        return {'workflow': store.get_workflow(wid), 'tasks': [], 'recovery': [], 'actions': actions}
    def effect(action):
        store.record_event('test.side_effect', {'action_id': action['action_id']}, workflow_id='wf-1')
        return {'ok': True}
    result = automation.run_automatic_controller_actions('wf-1', deps=deps_for(store, project=project, handlers={'resume': effect}))
    assert len(result) == 4
    assert len(store.list_events(workflow_id='wf-1', event_type='test.side_effect')) == 4


def test_a_new_pause_episode_can_resume(store):
    from services.controller import cockpit_automation as automation
    from herdr import kernel
    first = automation.run_automatic_controller_actions('wf-1', deps=deps_for(store))
    assert first[0]['status'] == 'applied'
    kernel.pause_workflow('wf-1', store=store)
    second = automation.run_automatic_controller_actions('wf-1', deps=deps_for(store))
    assert second[0]['status'] == 'applied'
    assert store.get_workflow('wf-1')['status'] == 'running'


@pytest.mark.parametrize('failure', [None, 'exit', 'timeout'])
def test_manual_external_handler_success_or_uncertain_failure_has_durable_readback(store, failure):
    from services.controller import cockpit_automation as automation
    import subprocess
    wf = store.get_workflow('wf-1')
    wf['status'] = 'running'
    store.save_workflow(wf)
    store.save_task({'workflow_id': 'wf-1', 'task_id': 'T', 'stage': 'build', 'node': 'build',
                     'execution_id': 'exec-1', 'run_id': 'r', 'version': 1, 'status': 'integrated',
                     'stage_verdict': 'pass'})
    def external(action):
        store.record_event('test.transport_started', {}, workflow_id='wf-1', task_id='T')
        if failure == 'exit':
            raise subprocess.CalledProcessError(2, ['finalize', 'T'])
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(['finalize', 'T'], 60)
        task = store.get_task('T')
        store.save_task({**task, 'status': 'cleaned'})
        return {'ok': True, 'task_id': 'T'}
    action = next(a for a in automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions']
                  if a['action_id'] == 'T:finalize')
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']}, deps=deps_for(store, handlers={'finalize': external}))
    assert result['receipt']['status'] == ('accepted_waiting' if failure else 'applied')
    assert result['receipt']['outcome'] == ('unknown' if failure else 'success')
    assert store.get_task('T')['status'] == ('integrated' if failure else 'cleaned')
    assert len(store.list_events(workflow_id='wf-1', event_type='test.transport_started')) == 1


def test_readback_from_replacement_execution_cannot_report_old_action_applied(store):
    from services.controller import cockpit_automation as automation
    action = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    before = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})
    replacement = {**before, 'workflow': {**before['workflow'], 'status': 'running', 'execution_id': 'new-execution'}}
    receipt = automation.classify_receipt(action, before, replacement, {'ok': True})
    assert receipt['status'] == 'rejected'
    assert receipt['reason_code'] == 'readback_identity_changed'


def test_scheduler_queue_is_bounded_and_rotates_without_starving_workflows(store, monkeypatch):
    from services.controller import cockpit_automation as automation
    pending = []
    monkeypatch.setattr(automation, '_executor', SimpleNamespace(submit=pending.append))
    monkeypatch.setattr(automation, '_inflight', set())
    for i in range(2, 7):
        store.save_workflow({**store.get_workflow('wf-1'), 'workflow_id': f'wf-{i}'})
    workflow_ids = [f'wf-{i}' for i in range(1, 7)]
    automation.schedule_automatic_controller_actions(store, workflow_ids)
    assert len(pending) == 4
    automation.schedule_automatic_controller_actions(store, workflow_ids)
    assert len(pending) == 4
    for work in pending[:]:
        work()
    pending.clear()
    automation.schedule_automatic_controller_actions(store, workflow_ids)
    for work in pending:
        work()
    assert {store.get_workflow(wid)['status'] for wid in workflow_ids} == {'running'}


def test_review_external_cli_without_identity_cas_never_runs_automatically(store, monkeypatch):
    from services.controller import cockpit_automation as automation
    wf = store.get_workflow('wf-1')
    store.save_workflow({**wf, 'status': 'running'})
    store.save_task({'workflow_id': 'wf-1', 'task_id': 'T', 'stage': 'build', 'node': 'build',
                     'execution_id': 'exec-1', 'run_id': 'old-run', 'version': 1,
                     'status': 'integrated', 'stage_verdict': 'pass'})
    called = []
    def external(command, **kwargs):
        called.append(command)
        # The CLI sees a replacement Run after the final projection read.
        store.update_task_metadata('T', {'run_id': 'replacement-run'})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('subprocess.run', external)
    result = automation.run_automatic_controller_actions('wf-1', deps=deps_for(store))
    assert called == []
    assert result[0]['reason_code'] == 'unsupported_handler'
    assert store.get_task('T')['run_id'] == 'old-run'


def test_review_exact_readback_does_not_use_two_hundred_task_display_cap(store):
    from services.controller import cockpit_automation as automation
    store.save_workflow({**store.get_workflow('wf-1'), 'status': 'running'})
    for i in range(201):
        store.save_task({'workflow_id': 'wf-1', 'task_id': f'pending-{i:03}', 'node': 'build', 'status': 'pending'})
    store.save_task({'workflow_id': 'wf-1', 'task_id': 'target', 'node': 'build', 'stage': 'build',
        'execution_id': 'exec-1', 'run_id': 'target-run', 'status': 'integrated', 'version': 1})
    before = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})
    assert before['tasks_truncated']
    assert not any(t['task_id'] == 'target' for t in before['tasks'])
    action = next(a for a in before['actions'] if a['action_id'] == 'target:finalize')
    def effect(action):
        store.save_task({**store.get_task('target'), 'status': 'cleaned'})
        return {'ok': True, 'task_id': 'target'}
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']}, deps=deps_for(store, handlers={'finalize': effect}))
    assert result['receipt']['status'] == 'applied'
    assert result['receipt']['target']['run_id'] == 'target-run'


def test_review_unsupported_prefix_does_not_starve_registered_handler(store):
    from services.controller import cockpit_automation as automation
    from copy import deepcopy
    original = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})
    resume = original['actions'][0]
    unsupported = [{**deepcopy(resume), 'action_id': f'unsupported-{i}',
                    'api_payload': {'type': 'steer'}} for i in range(8)]
    def project(wid):
        return {**automation.read_cockpit(store, wid, controller={'status': 'running'}),
                'actions': [*unsupported, resume]}
    result = automation.run_automatic_controller_actions('wf-1', deps=deps_for(store, project=project))
    assert store.get_workflow('wf-1')['status'] == 'running'
    assert any(receipt['status'] == 'applied' for receipt in result)
    assert len(result) <= 4


def test_review_post_effect_readback_failure_stays_uncertain_then_reconciles(store):
    from services.controller import cockpit_automation as automation
    reads = 0
    def project(wid):
        nonlocal reads
        reads += 1
        if reads == 3:
            raise OSError('readback unavailable after committed effect')
        return automation.read_cockpit(store, wid, controller={'status': 'running'})
    action = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']}, deps=deps_for(store, project=project))
    assert store.get_workflow('wf-1')['status'] == 'running'
    assert result['receipt']['status'] == 'accepted_waiting'
    assert result['receipt']['outcome'] == 'unknown'
    assert result['cockpit']['read_state'] == 'unknown'
    assert result['cockpit']['workflow'].get('status') not in {'paused', 'running'}
    reopened = SQLiteStateStore(store.db_path)
    automation.run_automatic_controller_actions('wf-1', deps=deps_for(reopened))
    receipts = automation.read_cockpit(reopened, 'wf-1', controller={'status': 'running'})['receipts']
    assert receipts[0]['status'] == 'applied'
    assert len(reopened.list_events(workflow_id='wf-1', event_type='workflow_transition')) == 1


def test_review_completed_receipt_cannot_regress_to_stale_pending_write(store):
    from services.controller import cockpit_automation as automation
    pending = {'workflow_id': 'wf-1', 'action_id': 'a', 'status': 'accepted_waiting', 'reconciliation': {}}
    final = {'workflow_id': 'wf-1', 'action_id': 'a', 'status': 'applied', 'outcome': 'success'}
    automation.persist_receipt(store, pending, key='same-operation')
    automation.persist_receipt(store, final, key='same-operation')
    returned, _ = automation.persist_receipt(store, pending, key='same-operation')
    assert returned == final
    assert automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['receipts'][0] == final


def test_review_reconciliation_rotates_unresolved_receipts_so_later_evidence_progresses(store):
    from services.controller import cockpit_automation as automation
    from copy import deepcopy
    from herdr import kernel
    before = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})
    resume = before['actions'][0]
    for i in range(4):
        unknown = {**deepcopy(resume), 'action_id': f'ghost-{i}', 'api_payload': {'type': 'steer'},
                   'target': {'workflow_id': 'wf-1', 'task_id': f'ghost-{i}', 'run_id': 'missing'}}
        pending = automation.pending_action_receipt(unknown, before, {'instruction': 'unresolved'},
                                                   automation.read_action_state(store, unknown))
        automation.persist_receipt(store, pending, key=f'ghost-{i}')
    pending = automation.pending_action_receipt(resume, before, {}, automation.read_action_state(store, resume))
    automation.persist_receipt(store, pending, key='last-resume')
    kernel.resume_workflow('wf-1', store=store)
    automation.reconcile_pending_receipts(store, 'wf-1')
    automation.reconcile_pending_receipts(store, 'wf-1')
    receipts = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['receipts']
    assert next(r for r in receipts if r['action_id'] == resume['action_id'])['status'] == 'applied'


def test_review_reconciliation_uses_own_transition_event_after_later_pause(store):
    from services.controller import cockpit_automation as automation
    from herdr import kernel
    reads = 0
    def project(wid):
        nonlocal reads
        reads += 1
        if reads == 3:
            raise OSError('readback unavailable')
        return automation.read_cockpit(store, wid, controller={'status': 'running'})
    action = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['actions'][0]
    result = automation.execute_action({'workflow_id': 'wf-1', 'action_id': action['action_id'],
        'expected_identity': action['expected_identity']}, deps=deps_for(store, project=project))
    assert result['receipt']['status'] == 'accepted_waiting'
    kernel.pause_workflow('wf-1', store=store)
    automation.reconcile_pending_receipts(store, 'wf-1')
    receipts = automation.read_cockpit(store, 'wf-1', controller={'status': 'running'})['receipts']
    assert receipts[0]['status'] == 'applied'
    assert receipts[0]['event_id'] == result['event_id']
    assert store.get_workflow('wf-1')['status'] == 'paused'
