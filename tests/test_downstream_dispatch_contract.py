"""Downstream dispatch keeps ownership until a matching Task is persisted."""
from types import SimpleNamespace

import pytest

from tests.test_node_dispatch_contract import scene
from herdr import node_dispatch_store as nd, recovery_store

SHA = 'a' * 40


def downstream(scene):
    workflow = scene.store.get_workflow('wf')
    workflow['config']['nodes'].append({'id': 'review', 'depends_on': ['implementation']})
    scene.store.save_workflow(workflow)
    scene.store.save_task({'task_id': 'impl', 'workflow_id': 'wf', 'node': 'implementation',
        'execution_id': 'execution-1', 'run_id': 'impl-run', 'status': 'cleaned',
        'integration_mode': 'git'})
    scene.store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf',
        source='critical-path-scheduler', timestamp=1000)
    return {'kind': 'stage_advance', 'workflow_id': 'wf', 'stage': 'implementation',
            'node_id': 'test', 'next_stage': 'test', 'node': workflow['config']['nodes'][1]}


def op_for(scene, node='test'):
    return nd.operation_for_node(scene.store.db_path, 'wf', node)


def test_ready_parallel_downstream_nodes_have_distinct_durable_obligations(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    test, review = op_for(scene), op_for(scene, 'review')
    assert test is not None and review is not None
    assert test['id'] != review['id']
    assert test['status'] == review['status'] == 'pending'
    assert test['payload']['candidate_sha'] == review['payload']['candidate_sha'] == SHA
    assert test['payload']['candidate_episode_id'] > 0


def test_downstream_successful_message_without_task_never_claims_advance(scene, monkeypatch, capsys):
    item = downstream(scene)
    monkeypatch.setattr(scene.ctrl, 'try_direct_stage_advance', lambda _: False)
    scene.ctrl._handle_coordinator_item(item)
    assert '[STAGE ADVANCED]' not in capsys.readouterr().out
    op = op_for(scene)
    assert op is not None and op['status'] == 'awaiting_result'
    assert op['detail']['deadline_at'] == 1900
    for _ in range(3):
        scene.clock[0] += 30
        scene.ctrl.check_workflow_stage_advance('wf')
    assert len(scene.sent) == 1
    assert op_for(scene)['detail']['deadline_at'] == 1900
    scene.clock[0] = 1900
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1900)
    assert op_for(scene)['status'] == 'waiting_human'


def test_legacy_notified_empty_downstream_node_gets_bounded_verification(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['test'], now=1000)
    op = op_for(scene)
    assert op is not None and op['status'] == 'awaiting_result' and op['started']
    assert op['detail']['origin'] == 'legacy_notified'
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['test'], now=1900)
    assert op_for(scene)['status'] == 'waiting_human'
    assert nd.claim(scene.store.db_path, op['id'], 'retry', now=1900) is None


def test_abandoned_downstream_task_is_visible_and_never_automatically_replaced(scene):
    downstream(scene)
    scene.store.save_task({'task_id': 'cancelled-test', 'workflow_id': 'wf', 'node': 'test',
        'execution_id': 'execution-1', 'run_id': 'old-run', 'status': 'superseded',
        'replacement_pending': False})
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = op_for(scene)
    assert op is not None and op['status'] == 'waiting_human'
    assert op['detail']['reason'] == 'dispatch_scope_cancelled'
    assert nd.claim(scene.store.db_path, op['id'], 'retry', now=1000) is None
    projection = nd.read_wait_projection(scene.store.db_path, 'wf', 1000)
    assert projection['is_stalled']


def test_old_episode_cannot_be_claimed_or_used_for_late_launch(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    old = op_for(scene)
    assert old is not None
    nd.claim(scene.store.db_path, old['id'], 'owner', now=1000)
    nd.start(scene.store.db_path, old['id'], 'owner', now=1000)
    # Re-freezing the same SHA is a distinct authorization episode.
    scene.store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf',
        source='critical-path-scheduler', timestamp=1001)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1001)
    assert nd.claim(scene.store.db_path, old['id'], 'other', now=1001) is None
    new = op_for(scene)
    assert new is not None and new['id'] != old['id']
    # Unknown old transport must prevent overlapping fresh delivery.
    assert new['status'] == 'waiting_human'


def test_upstream_regression_between_claim_and_send_is_rejected(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = op_for(scene)
    assert op is not None
    nd.claim(scene.store.db_path, op['id'], 'owner', now=1000)
    task = scene.store.get_task('impl')
    task['status'] = 'rework'
    scene.store.save_task(task)
    with pytest.raises(ValueError):
        nd.start(scene.store.db_path, op['id'], 'owner', now=1000)


def test_launch_intent_and_task_must_match_bound_candidate(scene):
    from herdr.task_resources import begin_launch_intent
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op['id'], 'owner', now=1000)
    nd.start(scene.store.db_path, op['id'], 'owner', now=1000)
    kwargs = dict(workflow_id='wf', node_id='test', task_id='tester',
        dispatch_operation_id=op['id'], run_id='test-run', execution_id='execution-1', now=1000)
    with pytest.raises(ValueError, match='candidate'):
        begin_launch_intent(scene.store, candidate_sha='b' * 40, **kwargs)
    intent = begin_launch_intent(scene.store, candidate_sha=SHA, **kwargs)['intent']
    task = {'task_id': 'tester', 'workflow_id': 'wf', 'node': 'test', 'status': 'pending',
        'execution_id': 'execution-1', 'run_id': 'test-run', 'dispatch_operation_id': op['id'],
        'launch_intent_id': intent['intent_id'], 'candidate_sha': 'b' * 40}
    with pytest.raises(ValueError, match='candidate'):
        scene.store.save_task(task)
    task['candidate_sha'] = SHA
    scene.store.save_task(task)
    result = nd.transport_finished(scene.store.db_path, op['id'], 'owner', reason='registered', now=1000)
    assert result['status'] == 'resolved'
    assert result['detail']['registered_runs'] == {'tester': 'test-run'}
    assert scene.store.get_task('tester')['candidate_sha'] == SHA


def direct_scene(scene, monkeypatch):
    item = downstream(scene)
    wf = scene.store.get_workflow('wf')
    wf.update(project_root='/controlled/project', coordinator_pane_id='external-coordinator',
              base_branch='main')
    wf['config']['nodes'][1].update(default_task_type='test', default_integration_mode='none',
                                   parallel=False, purpose='independent verification')
    scene.store.save_workflow(wf)
    item['node'] = wf['config']['nodes'][1]
    monkeypatch.setattr(scene.ctrl, '_scheduler_expected_candidate_sha', lambda *a: SHA)
    monkeypatch.setattr(scene.ctrl, '_dispatch_candidate_ready', lambda *a, **kw: True)
    return item


@pytest.mark.parametrize('outcome', ['empty_success', 'nonzero', 'timeout'])
def test_direct_transport_without_registration_does_not_fallback_or_claim_advance(scene, monkeypatch, capsys, outcome):
    import subprocess
    item = direct_scene(scene, monkeypatch)
    calls = []
    def external(command, **kw):
        calls.append(command)
        assert command[1] == 'launch'
        assert '--dispatch-operation-id' in command
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(command, kw['timeout'])
        return SimpleNamespace(returncode=1 if outcome == 'nonzero' else 0,
                               stdout='', stderr='launch failed' if outcome == 'nonzero' else '')
    monkeypatch.setattr(scene.ctrl.subprocess, 'run', external)
    scene.ctrl._handle_coordinator_item(item)
    output = capsys.readouterr().out
    assert '[STAGE ADVANCED' not in output
    op = op_for(scene)
    assert op is not None and op['status'] == 'awaiting_result' and op['started']
    scene.ctrl.check_workflow_stage_advance('wf')
    assert scene.ctrl.coordinator_queue.qsize() == 1  # only the parallel review is queued
    assert len(calls) == 1


def test_direct_transport_registration_resolves_real_intent_and_persisted_task(scene, monkeypatch, capsys):
    from herdr.task_resources import begin_launch_intent
    item = direct_scene(scene, monkeypatch)
    def external(command, **kw):
        def arg(name):
            return command[command.index(name) + 1]
        assert command[1] == 'launch'
        op_id = int(arg('--dispatch-operation-id'))
        intent = begin_launch_intent(scene.store, workflow_id=arg('--workflow-id'),
            node_id=arg('--node'), task_id=arg('--task-id'), role=arg('--dispatch-role'),
            candidate_sha=arg('--candidate-sha'), dispatch_operation_id=op_id,
            execution_id='execution-1', run_id='test-run', now=1000)['intent']
        scene.store.save_task({'task_id': arg('--task-id'), 'workflow_id': 'wf', 'node': 'test',
            'status': 'pending', 'execution_id': 'execution-1', 'run_id': 'test-run',
            'dispatch_operation_id': op_id, 'launch_intent_id': intent['intent_id'], 'candidate_sha': SHA})
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(scene.ctrl.subprocess, 'run', external)
    scene.ctrl._handle_coordinator_item(item)
    assert '[STAGE ADVANCED DIRECT]' in capsys.readouterr().out
    assert op_for(scene)['status'] == 'resolved'
    assert scene.store.get_task('wf-test-auto')['dispatch_operation_id'] == op_for(scene)['id']


def test_parallel_claims_use_independent_connections_and_only_one_lease(scene):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = op_for(scene)
    barrier = Barrier(2)
    def claim(owner):
        barrier.wait(timeout=5)
        return nd.claim(scene.store.db_path, op['id'], owner, now=1000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['one', 'two']))
    assert sum(result is not None for result in results) == 1
    assert op_for(scene)['attempts'] == 1


def test_lost_downstream_queue_recovers_lease_without_notified_lock(scene):
    downstream(scene)
    scene.ctrl.check_workflow_stage_advance('wf')
    first = scene.ctrl.coordinator_queue.get_nowait()
    second = scene.ctrl.coordinator_queue.get_nowait()
    assert {first['node_id'], second['node_id']} == {'test', 'review'}
    scene.clock[0] = 1661
    scene.ctrl.check_workflow_stage_advance('wf')
    assert scene.ctrl.coordinator_queue.qsize() == 2
    assert op_for(scene)['attempts'] == 2


def test_upstream_blocked_cleaned_task_does_not_authorize_downstream(scene):
    downstream(scene)
    task = scene.store.get_task('impl')
    task['stage_verdict'] = 'blocked'
    scene.store.save_task(task)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    assert op_for(scene) is None


def test_cancelled_scope_cannot_be_reenabled_by_generic_retry(scene):
    test_abandoned_downstream_task_is_visible_and_never_automatically_replaced(scene)
    op = op_for(scene)
    # Generic retry is not authorization to reverse a cancellation decision.
    with pytest.raises(ValueError, match='cancelled'):
        recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
            'operator', 'retry', 'retry dispatch', now=1000)
    assert nd.claim(scene.store.db_path, op['id'], 'owner', now=1000) is None
    assert op_for(scene)['status'] == 'waiting_human'


def test_downstream_human_hold_uses_current_dependency_snapshot(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = op_for(scene)
    held = recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
        'operator', 'hold', 'wait for eligible agent', now=1000, until=1100)
    assert held['status'] == 'waiting'
    assert nd.claim(scene.store.db_path, held['id'], 'owner', now=1050) is None


def test_proven_resources_absent_direct_failure_retries_with_fixed_attempt_budget(scene, monkeypatch):
    from herdr.task_resources import begin_launch_intent, abort_launch_intent
    item = direct_scene(scene, monkeypatch)
    calls = []
    def external(command, **kw):
        def arg(name):
            return command[command.index(name) + 1]
        calls.append(command)
        intent = begin_launch_intent(scene.store, workflow_id='wf', node_id='test',
            task_id=arg('--task-id'), candidate_sha=SHA,
            dispatch_operation_id=int(arg('--dispatch-operation-id')),
            execution_id='execution-1', run_id='test-run', now=scene.clock[0])['intent']
        abort_launch_intent(scene.store, intent, reason='router_rejected', now=scene.clock[0])
        return SimpleNamespace(returncode=1, stdout='', stderr='router rejected before allocation')
    monkeypatch.setattr(scene.ctrl.subprocess, 'run', external)
    scene.ctrl._handle_coordinator_item(item)
    assert op_for(scene)['status'] == 'pending'
    assert not op_for(scene)['started']
    for _ in range(2):
        scene.clock[0] += 30
        scene.ctrl._handle_coordinator_item(item)
    assert len(calls) == 3
    assert op_for(scene)['status'] == 'waiting_human'
    assert op_for(scene)['detail']['reason'] == 'dispatch_resources_absent'
    assert nd.claim(scene.store.db_path, op_for(scene)['id'], 'fourth', now=1100) is None


def test_direct_success_requires_all_planned_roles_not_only_first_task(scene, monkeypatch, capsys):
    from herdr.task_resources import begin_launch_intent
    item = direct_scene(scene, monkeypatch)
    wf = scene.store.get_workflow('wf')
    node = wf['config']['nodes'][1]
    node.update(agent_policy={'roles': [{'name': 'executor', 'goal': 'test'},
                                      {'name': 'challenger', 'goal': 'challenge'}]})
    scene.store.save_workflow(wf)
    item['node'] = node
    calls = []
    def external(command, **kw):
        def arg(name):
            return command[command.index(name) + 1]
        calls.append(command)
        if len(calls) == 1:
            op_id = int(arg('--dispatch-operation-id'))
            intent = begin_launch_intent(scene.store, workflow_id='wf', node_id='test',
                role=arg('--dispatch-role'), task_id=arg('--task-id'), candidate_sha=SHA,
                dispatch_operation_id=op_id, execution_id='execution-1', run_id='test-run', now=1000)['intent']
            scene.store.save_task({'task_id': arg('--task-id'), 'workflow_id': 'wf', 'node': 'test',
                'status': 'pending', 'execution_id': 'execution-1', 'run_id': 'test-run',
                'dispatch_operation_id': op_id, 'launch_intent_id': intent['intent_id'], 'candidate_sha': SHA})
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(scene.ctrl.subprocess, 'run', external)
    scene.ctrl._handle_coordinator_item(item)
    assert len(calls) == 2
    assert '[STAGE ADVANCED' not in capsys.readouterr().out
    assert op_for(scene)['status'] == 'awaiting_result'


def test_hold_cannot_erase_cancellation_before_retry(scene):
    test_abandoned_downstream_task_is_visible_and_never_automatically_replaced(scene)
    op = op_for(scene)
    held = recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
        'operator', 'hold', 'wait for owner', now=1000, until=1100)
    with pytest.raises(ValueError, match='cancelled'):
        recovery_store.decide_operation(scene.store.db_path, held['id'], held['version'],
            'operator', 'retry', 'try again', now=1001)


def test_hold_cannot_erase_unknown_prior_transport_before_retry(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    old = op_for(scene)
    nd.claim(scene.store.db_path, old['id'], 'old', now=1000)
    nd.start(scene.store.db_path, old['id'], 'old', now=1000)
    scene.store.record_event('candidate_frozen', {'candidate_sha': 'b' * 40}, workflow_id='wf',
        source='critical-path-scheduler', timestamp=1001)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1001)
    op = op_for(scene)
    held = recovery_store.decide_operation(scene.store.db_path, op['id'], op['version'],
        'operator', 'hold', 'wait for owner', now=1001, until=1100)
    with pytest.raises(ValueError, match='prior delivery unknown'):
        recovery_store.decide_operation(scene.store.db_path, held['id'], held['version'],
            'operator', 'retry', 'try again', now=1002)


def record_test_reuse(scene):
    from herdr import reverification
    wf = scene.store.get_workflow('wf')
    if not any(n['id'] == 'join' for n in wf['config']['nodes']):
        wf['config']['nodes'].append({'id': 'join', 'node_type': 'gate', 'depends_on': ['test', 'review']})
        scene.store.save_workflow(wf)
    scene.store.record_event('reverification_decision', {
        'decision': 'reuse', 'verifier': 'test', 'to_candidate_sha': SHA,
        'candidate_frozen_event_id': scene.store.get_workflow('wf')['candidate_episode_id'],
        'policy_identity': reverification.policy_identity(reverification.policy_from_workflow(wf['config'])),
        'source_verdict': 'pass', 'source_verified_candidate_sha': 'b' * 40,
        'source_candidate_sha': 'b' * 40, 'source_task_id': 'previous-test'}, workflow_id='wf', timestamp=1000)


def test_current_reuse_does_not_create_phantom_dispatch_responsibility(scene):
    downstream(scene)
    record_test_reuse(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    assert scene.ctrl._reverification_reused_node('wf', 'test')
    assert op_for(scene) is None
    assert op_for(scene, 'review') is not None


@pytest.mark.parametrize('started', [False, True])
def test_reuse_transfers_unsent_responsibility_but_not_started_unknown_delivery(scene, started):
    downstream(scene)
    # Configure the join before claiming, so reuse alone is the changed input.
    wf = scene.store.get_workflow('wf')
    wf['config']['nodes'].append({'id': 'join', 'node_type': 'gate', 'depends_on': ['test', 'review']})
    scene.store.save_workflow(wf)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = op_for(scene)
    nd.claim(scene.store.db_path, op['id'], 'owner', now=1000)
    if started:
        nd.start(scene.store.db_path, op['id'], 'owner', now=1000)
    record_test_reuse(scene)
    if started:
        nd.transport_finished(scene.store.db_path, op['id'], 'owner', reason='unknown', now=1000)
        assert op_for(scene)['status'] == 'awaiting_result'
        nd.reconcile_workflow(scene.store.db_path, 'wf', now=1900)
        assert op_for(scene)['status'] == 'waiting_human'
        return
    assert nd.start(scene.store.db_path, op['id'], 'owner', now=1000) is None
    assert op_for(scene)['status'] == 'superseded'
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=100000)
    assert op_for(scene)['status'] == 'superseded'


def test_real_cli_downstream_launch_binds_candidate_intent_task_and_readback(tmp_path, monkeypatch):
    from tests.test_recovery_entrypoints import _real_committed_launch_scene
    from herdr.state_store import reset_state_store
    import subprocess
    real_run = subprocess.run
    cli, store, make_args, calls, sha = _real_committed_launch_scene(tmp_path, monkeypatch)
    native_transport = cli.subprocess.run
    monkeypatch.setattr(cli.subprocess, 'run', lambda cmd, **kw:
        real_run(cmd, **kw) if cmd[0] == 'ps' else native_transport(cmd, **kw))
    wf = store.get_workflow('wf')
    wf['config']['nodes'].append({'id': 'test', 'depends_on': ['implementation'], 'max_tasks_per_node': 4})
    store.save_workflow(wf)
    impl = store.get_task('old')
    impl['status'] = 'cleaned'
    store.save_task(impl)
    nd.reconcile_workflow(store.db_path, 'wf')
    op = nd.operation_for_node(store.db_path, 'wf', 'test')
    assert op is not None
    nd.claim(store.db_path, op['id'], 'controller')
    nd.start(store.db_path, op['id'], 'controller')
    args = make_args()
    args.node = args.stage = 'test'
    args.task_type = 'test'
    args.integration_mode = 'none'
    args.supersedes = None
    args.dispatch_round = 1
    args.dispatch_operation_id = op['id']
    cli.launch_task(args)
    task = store.get_task('new')
    assert task['candidate_sha'] == task['baseline_commit'] == sha
    assert task['dispatch_operation_id'] == op['id']
    assert task['execution_id'] == 'execution' and task['run_id'] == 'new-run'
    receipt = nd.transport_finished(store.db_path, op['id'], 'controller', reason='registered')
    assert receipt['status'] == 'resolved'
    assert receipt['detail']['registered_runs'] == {'new': 'new-run'}
    intents = store.list_events(event_type='launch_intent', desc=True, limit=1)
    assert intents[0]['payload']['intent_id'] == task['launch_intent_id']
    assert calls['worker'] == calls['prompt'] == 1
    reset_state_store()


def test_cancelled_latest_lineage_head_is_visible_with_linked_history(scene):
    downstream(scene)
    scene.store.save_task({'task_id': 'test-r1', 'workflow_id': 'wf', 'node': 'test',
        'execution_id': 'execution-1', 'run_id': 'run-r1', 'status': 'superseded',
        'replacement_pending': True, 'superseded_by': 'test-r2'})
    scene.store.save_task({'task_id': 'test-r2', 'workflow_id': 'wf', 'node': 'test',
        'execution_id': 'execution-1', 'run_id': 'run-r2', 'status': 'superseded',
        'replacement_pending': False, 'supersedes': 'test-r1'})
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['test'], now=1000)
    op = op_for(scene)
    assert op is not None and op['status'] == 'waiting_human'
    assert op['detail']['reason'] == 'dispatch_scope_cancelled'
    assert nd.claim(scene.store.db_path, op['id'], 'retry', now=1000) is None
