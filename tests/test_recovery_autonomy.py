"""Real Controller/store/checkpoint recovery, with only native boundaries replaced."""
import importlib
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from herdr import recovery_store as rs
from herdr.state_store import SQLiteStateStore
from herdr.task_checkpoint import publish_task_checkpoint, record_business_acceptance

A, B = 'a' * 40, 'b' * 40


@pytest.fixture
def scene(tmp_path, monkeypatch, request):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('HERDR_CONTROLLER_DIR', str(tmp_path / 'controller'))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'docs'))
    store = SQLiteStateStore(tmp_path / 'state.db')
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    clone = None
    if request.node.name == 'test_same_operation_end_to_end':
        import sys
        for key, value in [('user.name', 'Recovery test'), ('user.email', 'recovery@example.invalid')]:
            subprocess.run(['git', '-C', str(repo), 'config', key, value], check=True)
        (repo / 'seed.txt').write_text('original candidate')
        subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'seed', '--no-gpg-sign'], check=True)
        actual = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        monkeypatch.setattr(sys.modules[__name__], 'A', actual)
        clone = tmp_path / 'worker'
        subprocess.run(['git', 'clone', '-q', str(repo), str(clone)], check=True)
        subprocess.run(['git', '-C', str(clone), 'switch', '-qc', 'agent/test/fix-impl'], check=True)
        for key, value in [('user.name', 'Recovery test'), ('user.email', 'recovery@example.invalid')]:
            subprocess.run(['git', '-C', str(clone), 'config', key, value], check=True)
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    config = {'nodes': [{'id': 'implementation'}, {'id': 'test', 'gate': {'retry_node': 'implementation'}}]}
    store.save_workflow(dict(workflow_id='wf', status='running', startup_ready=True,
        execution_id='execution', project_root=str(repo), base_branch='main', created_at=1, config=config))
    store.record_event('candidate_frozen', {'candidate_sha': A}, workflow_id='wf',
        source='critical-path-scheduler', timestamp=2)
    store.save_task(dict(task_id='impl', workflow_id='wf', node='implementation', status='working',
        run_id='run', execution_id='execution', candidate_sha=A, commit=A, pane_id='p',
        completion_protocol='receipt-v1', completion_epoch='initial', completion_identity_path='/initial'))
    if clone:
        wf, _, _ = rs.read_snapshot(store.db_path, 'wf')
        store.update_task_metadata('impl', dict(clone_path=str(clone), branch='agent/test/fix-impl',
            source_repo=str(repo), base_branch='main', integration_mode='git', baseline_commit=A,
            baseline_fingerprint={'tracked': {}, 'untracked': {}}, candidate_episode_id=wf['candidate_episode_id']))
    store.save_task(dict(task_id='test-old', workflow_id='wf', node='test', status='cleaned',
        run_id='gate-old', execution_id='execution', candidate_sha=A, stage_verdict='blocked'))
    ctl = importlib.import_module('services.herdr-controller')
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(ctl, 'load_tasks', store.list_tasks)
    monkeypatch.setattr(ctl, 'TASK_MANAGER', str(Path(__file__).resolve().parents[1] / 'bin/herdr-task'))
    # Native probe only; ownership and receipt logic under test remain real.
    from herdr import task_resources
    monkeypatch.setattr(task_resources, 'owned_live_pane', lambda _: (True, 'controlled native instance'))
    monkeypatch.setattr(ctl, 'reconcile_node_dispatches', lambda *a, **k: None)
    monkeypatch.setattr(ctl, 'verify_auto_recover_redispatch', lambda *a: None)
    monkeypatch.setattr(ctl, 'converge_dangling_replacement_obligations', lambda *a: None)
    monkeypatch.setattr(ctl, 'notify_attention', lambda *a, **k: True)
    monkeypatch.setattr(ctl, 'clear_stage_advance', lambda *a: None)
    # Keep actual gate invalidation implementation, replacing only its transport if needed.
    dispatches = []
    real_run = subprocess.run
    faults = {'preflight': 0, 'after_delivery': False}
    from herdr.task_cli_rework import handle_rework
    def run(argv, **kwargs):
        if argv[0] == 'git':
            if 'status' in argv and faults['preflight']:
                faults['preflight'] -= 1
                raise subprocess.TimeoutExpired(argv, 10)
            return real_run(argv, **kwargs)
        if len(argv) > 1 and argv[1] == 'rework':
            request = argv[argv.index('--request-id') + 1]
            def send(*args):
                dispatches.append(request)
                return SimpleNamespace(returncode=0)
            handle_rework(SimpleNamespace(task_id=argv[2], request_id=request, prompt='repair', reason='repair'),
                get_store=lambda: store, herdr=send, is_gate_task=lambda _: False,
                recover_unconsumed_gate_cleanup_id=lambda *a: None,
                cleanup_gate_for_rework=lambda *a: None, now=lambda: clock[0])
            if faults['after_delivery']:
                raise subprocess.TimeoutExpired(argv, 30)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if len(argv) > 1 and argv[1] == 'supersede':
            return real_run(argv, **kwargs)
        raise AssertionError(f'unexpected native command {argv}')
    # Git hygiene imports subprocess itself. Patch only for this controlled fixture.
    monkeypatch.setattr(subprocess, 'run', run)
    return SimpleNamespace(store=store, ctl=ctl, clock=clock, faults=faults, dispatches=dispatches, clone=clone, real_run=real_run,
        tick=lambda: ctl.check_workflow_recovery('wf', now=clock[0]),
        op=lambda: next(o for o in rs.list_operations(store.db_path, 'wf') if o['payload']['kind'] == 'fix_loop'))


def checkpoint(s, step):
    task = s.store.get_task('impl')
    return publish_task_checkpoint('impl', task['run_id'], task['completion_epoch'],
        f'completed phase {step}', step, completed_steps=[f'phase-{step}'], store=s.store)


def test_safe_preflight_timeout_retries_real_controller(scene):
    s = scene
    s.faults['preflight'] = 1
    s.tick()
    op = s.op()
    assert op['status'] == 'pending', json.dumps(op['detail'])
    assert op['started'] == 0 and op['attempts'] == 1
    assert op['next_due_at'] > s.clock[0]
    s.tick()
    assert not s.dispatches
    s.clock[0] = op['next_due_at']
    s.tick()
    assert s.op()['status'] == 'awaiting_result', s.op()
    assert len(s.dispatches) == 1


def test_verified_progress_crosses_original_hour(scene):
    s = scene
    s.tick()
    op = s.op()
    assert op['status'] == 'awaiting_result', json.dumps(op['detail'])
    s.clock[0] += 3500
    checkpoint(s, 1)
    s.tick()
    s.clock[0] += 200
    s.tick()
    assert s.op()['status'] == 'awaiting_result', s.op()
    assert len(s.dispatches) == 1


def test_same_operation_end_to_end(scene, tmp_path):
    s = scene
    start = s.clock[0]
    timeline = []
    def tick(label):
        s.tick()
        op = s.op()
        timeline.append(dict(label=label, elapsed=s.clock[0] - start, id=op['id'],
            status=op['status'], attempts=op['attempts'], dispatches=len(s.dispatches),
            idle_remaining=op['detail'].get('result_deadline_at', 0) - s.clock[0],
            total_remaining=op['detail'].get('total_deadline_at', 0) - s.clock[0]))
        return op
    s.faults['preflight'] = 1
    first = tick('safe preflight failure')
    assert first['status'] == 'pending'
    s.clock[0] = first['next_due_at']
    assert tick('automatic retry and real rework receipt')['status'] == 'awaiting_result'
    s.clock[0] = start + 3500
    checkpoint(s, 1)
    tick('verified phase 1')
    s.clock[0] = start + 3800
    checkpoint(s, 2)
    assert tick('past original hour')['status'] == 'awaiting_result'
    # The controlled Agent writes real output, submits its actual completion credential,
    # and the normal CLI creates/exports a real Git commit. Never inject integrated state.
    from herdr.completion_receipt import report_completion, consume_completion_receipt, issue_completion_contract
    from herdr import kernel
    (s.clone / 'repair.txt').write_text('acceptance defect repaired')
    task = s.store.get_task('impl')
    identity = json.loads(Path(task['completion_identity_path']).read_text())
    report_completion('impl', identity, [checkpoint(s, 3)], s.store)
    assert consume_completion_receipt('impl', s.store, now=s.clock[0])['accepted']
    result = kernel.transition_task('impl', 'completed', reason='structured completion accepted',
        source='controller', store=s.store, expected_status='agent_done')
    assert result['accepted']
    cli = str(Path(__file__).resolve().parents[1] / 'bin/herdr-task')
    for argv in ([cli, 'commit', 'impl', '--message', 'repair acceptance defect'], [cli, 'integrate', 'impl']):
        delivered = s.real_run(argv, text=True, capture_output=True, timeout=30)
        assert delivered.returncode == 0, delivered.stdout + delivered.stderr
        print(delivered.stdout + delivered.stderr)
    task = s.store.get_task('impl')
    assert task['status'] == 'integrated' and task.get('integration_ref')
    candidate = task['integrated_commit']
    assert s.ctl._scheduler_freeze_candidate('wf', str(s.clone.parent / 'repo'), 'implementation', []) == candidate
    assert rs.read_snapshot(s.store.db_path, 'wf')[0]['candidate_sha'] == candidate
    assert (s.clone / 'repair.txt').read_text() == 'acceptance defect repaired'
    s.clock[0] += 40
    assert tick('delivery without verifier is insufficient')['status'] == 'awaiting_result'
    s.store.save_task(dict(task_id='test-new', workflow_id='wf', node='test', status='working',
        run_id='test-new-run', execution_id='execution', candidate_sha=candidate,
        completion_protocol='receipt-v1', completion_epoch='test-epoch',
        completion_identity_path='/test-identity', acceptance_criteria=['repair meets acceptance']))
    contract = issue_completion_contract('test-new', s.store)
    identity = json.loads(Path(contract['path']).read_text())
    # Contract renewal changes epoch, so publish the actual verifier report in that epoch.
    report = publish_task_checkpoint('test-new', 'test-new-run', identity['epoch'],
        'verified actual repair output and Git candidate ' + candidate, 1,
        store=s.store, completed_steps=['verified repair'])
    record_business_acceptance(s.store, 'test-new', 'test-new-run', identity['epoch'], candidate,
        'pass', [report], ['AC-1=pass'])
    report_completion('test-new', identity, [report], s.store)
    s.clock[0] += 60
    consumed = consume_completion_receipt('test-new', s.store, now=s.clock[0])
    assert consumed['accepted'], consumed
    result = kernel.transition_task('test-new', 'completed', reason='verified business acceptance',
        source='controller', store=s.store, expected_status='agent_done',
        metadata={'stage_verdict': 'pass', 'verified_candidate_sha': candidate})
    assert result['accepted']
    s.clock[0] += 40
    final = tick('fresh candidate and complete business gate')
    assert final['status'] == 'resolved', final['detail']
    assert final['detail']['reason'] == 'replacement_reverified'
    assert final['id'] == first['id'] and final['attempts'] == 2
    assert final['detail']['first_started_at'] == start
    assert len(s.dispatches) == len(set(s.dispatches)) == 1
    assert not any(t['status'] == 'waiting_human' for t in timeline)
    evidence = dict(timeline=timeline, dispatches=len(s.dispatches), retries=final['attempts'] - 1,
        final_reason=final['detail']['reason'], first_started_at=start,
        total_deadline_at=final['detail']['total_deadline_at'])
    (tmp_path / 'timeline.json').write_text(json.dumps(evidence, indent=2))
    print('RECOVERY_V1_TIMELINE=' + json.dumps(evidence))


def test_unknown_after_dispatch_verifies_without_resending(scene):
    s = scene
    s.faults['after_delivery'] = True
    s.tick()
    op = s.op()
    assert op['status'] == 'pending' and op['detail']['action'] == 'verify'
    s.faults['after_delivery'] = False
    s.clock[0] = op['next_due_at']
    s.tick()
    assert s.op()['status'] == 'awaiting_result', s.op()['detail']
    assert len(s.dispatches) == 1


def test_started_zero_without_specific_preflight_proof_does_not_retry(scene):
    from herdr.workflow_recovery import drive_recovery
    s = scene
    def ambiguous(*args):
        raise TimeoutError('request may have left process')
    drive_recovery(s.store, 'wf', ambiguous, s.clock[0])
    op = s.op()
    assert op['started'] == 0 and op['status'] == 'waiting_human'
    s.clock[0] += 600
    s.tick()
    assert s.op()['status'] == 'waiting_human' and not s.dispatches


@pytest.mark.parametrize('invalid', ['timestamp', 'repeat', 'wrong_run', 'wrong_epoch', 'wrong_workflow', 'tampered', 'same_phase'])
def test_untrusted_or_repeated_activity_does_not_extend(scene, invalid):
    s = scene
    s.tick()
    start = s.clock[0]
    if invalid in ('repeat', 'same_phase'):
        checkpoint(s, 1)
        s.clock[0] += 31
        s.tick()
    original = s.op()['detail']['result_deadline_at']
    s.clock[0] = start + 3500
    if invalid == 'timestamp':
        s.store.update_task_metadata('impl', {'note': 'still working'})
    elif invalid == 'repeat':
        checkpoint(s, 1)
    elif invalid == 'same_phase':
        t = s.store.get_task('impl')
        publish_task_checkpoint('impl', t['run_id'], t['completion_epoch'], 'another report', 2,
            store=s.store, completed_steps=['phase-1'])
    else:
        receipt = checkpoint(s, 1)
        if invalid == 'tampered':
            from herdr.observation import ObservationStore
            path = Path(ObservationStore(s.store.db_path).get(receipt['observation_id']).content_ref)
            path.chmod(0o600)
            path.write_text('corrupted')
        else:
            # Malformed external evidence fixture: keep current task identity unchanged.
            with rs._transaction(s.store.db_path) as conn:
                row = conn.execute("SELECT id,payload_json FROM events WHERE event_type='task_artifact_checkpoint'").fetchone()
                payload = json.loads(row['payload_json'])
                payload[{'wrong_run': 'run_id', 'wrong_epoch': 'epoch', 'wrong_workflow': 'task_id'}[invalid]] = 'foreign'
                conn.execute('UPDATE events SET payload_json=? WHERE id=?', (json.dumps(payload), row['id']))
    s.tick()
    assert s.op()['detail']['result_deadline_at'] == original
    s.clock[0] = original + 1
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_no_progress_timeout'


def test_continuous_progress_still_stops_at_total(scene):
    s = scene
    s.tick()
    start = s.clock[0]
    for step in range(1, 4):
        s.clock[0] = start + step * 3000
        checkpoint(s, step)
        s.tick()
        assert s.op()['status'] == 'awaiting_result'
    total = s.op()['detail']['total_deadline_at']
    assert total == start + 10800
    s.clock[0] = total
    checkpoint(s, 4)
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_total_budget_exhausted'


def test_reopen_store_keeps_backoff_budget_and_consumption(scene):
    s = scene
    s.faults['preflight'] = 1
    s.tick()
    first = s.op()
    reopened = SQLiteStateStore(s.store.db_path)
    assert rs.list_operations(reopened.db_path, 'wf')[0] == first
    s.tick()
    assert s.op()['attempts'] == 1 and not s.dispatches
    s.clock[0] = first['next_due_at']
    s.tick()
    checkpoint(s, 1)
    s.clock[0] += 31
    s.tick()
    before = s.op()
    assert before['detail']['progress_cursors']
    assert rs.list_operations(SQLiteStateStore(s.store.db_path).db_path, 'wf')[0] == before
    s.clock[0] += 31
    s.tick()
    assert s.op()['detail'] == before['detail']


def test_safe_failure_exhaustion_is_persistent(scene):
    s = scene
    s.faults['preflight'] = 3
    for attempt in range(1, 4):
        s.tick()
        op = s.op()
        assert op['attempts'] == attempt
        s.clock[0] = op['next_due_at']
    assert op['status'] == 'waiting_human'
    assert op['detail']['reason'] == 'recovery_retry_budget_exhausted'
    s.tick()
    assert s.op()['attempts'] == 3 and not s.dispatches


def test_unknown_transport_without_receipt_is_bounded_and_never_resent(scene, monkeypatch):
    s = scene
    original = s.ctl._rework_retry_task
    calls = []
    def unknown(*args, **kwargs):
        calls.append(kwargs['request_id'])
        raise subprocess.TimeoutExpired('subprocess already started', 30)
    monkeypatch.setattr(s.ctl, '_rework_retry_task', unknown)
    s.tick()
    op = s.op()
    assert op['status'] == 'pending' and op['started']
    deadline = op['detail']['verify_deadline_at']
    monkeypatch.setattr(s.ctl, '_rework_retry_task', original)
    for offset in (30, 90, 150, 300):
        s.clock[0] = op['detail']['first_started_at'] + offset
        s.tick()
        assert s.op()['status'] == 'pending'
        assert s.op()['attempts'] == 1
    s.clock[0] = deadline
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_delivery_unknown'
    assert len(calls) == 1 and not s.dispatches


def test_restart_after_delivery_before_operation_receipt_uses_same_target(scene, monkeypatch):
    s = scene
    class Crash(BaseException):
        pass
    original = s.ctl._rework_retry_task
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()
    monkeypatch.setattr(s.ctl, '_rework_retry_task', crash)
    with pytest.raises(Crash):
        s.tick()
    running = s.op()
    assert running['status'] == 'running' and running['started']
    monkeypatch.setattr(s.ctl, '_rework_retry_task', original)
    s.clock[0] = running['lease_until'] + 1
    s.tick()
    assert s.op()['status'] == 'awaiting_result', s.op()['detail']
    assert len(s.dispatches) == 1
    assert s.op()['detail']['first_started_at'] == running['detail']['first_started_at']
    with pytest.raises(ValueError, match='stale'):
        rs.finish_operation(s.store.db_path, running['id'], running['owner'], 'resolved', {}, s.clock[0])


@pytest.mark.parametrize('legacy', ['awaiting_deadline', 'awaiting_unknown', 'waiting_human', 'pending_attempted'])
def test_legacy_records_do_not_get_new_budget(scene, legacy):
    s = scene
    if legacy == 'pending_attempted':
        rs.reconcile(s.store.db_path, 'wf', s.clock[0])
        op = s.op()
        with rs._transaction(s.store.db_path) as conn:
            conn.execute('UPDATE workflow_recovery_operations SET attempts=1 WHERE id=?', (op['id'],))
        s.tick()
        assert s.op()['status'] == 'waiting_human'
        assert s.op()['detail']['reason'] == 'recovery_legacy_budget_unknown'
        assert 'first_started_at' not in s.op()['detail'] and not s.dispatches
        return
    s.tick()
    op = s.op()
    detail = {k: v for k, v in op['detail'].items() if k not in {
        'first_started_at', 'total_deadline_at', 'idle_seconds', 'progress_cursors', 'progress_event_floor', 'effect_phase'}}
    status = 'awaiting_result'
    started = 1
    if legacy == 'awaiting_unknown':
        detail.pop('result_deadline_at')
    if legacy == 'waiting_human':
        status = 'waiting_human'
        detail.update(operator='human', reason='explicit_scope_decision')
    with rs._transaction(s.store.db_path) as conn:
        conn.execute('UPDATE workflow_recovery_operations SET status=?,started=?,detail_json=?,next_due_at=? WHERE id=?',
            (status, started, json.dumps(detail), s.clock[0], op['id']))
    s.clock[0] += 3500
    checkpoint(s, 1)
    s.tick()
    if legacy == 'awaiting_deadline':
        assert s.op()['status'] == 'awaiting_result'
        assert s.op()['detail']['result_deadline_at'] == detail['result_deadline_at']
        s.clock[0] += 200
        s.tick()
        assert s.op()['detail']['reason'] == 'recovery_result_timeout'
    elif legacy == 'waiting_human':
        assert s.op()['detail']['reason'] == 'explicit_scope_decision'
        assert s.op()['detail']['operator'] == 'human'
    else:
        assert s.op()['detail']['reason'] == 'recovery_legacy_budget_unknown'
    assert 'first_started_at' not in s.op()['detail']
    assert len(s.dispatches) == 1


def test_independent_connections_claim_once_and_old_owner_cannot_finish(scene):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    s = scene
    rs.reconcile(s.store.db_path, 'wf', s.clock[0])
    op = s.op()
    barrier = threading.Barrier(2)
    def claim(owner):
        barrier.wait()
        return rs.claim_operation(s.store.db_path, op['id'], owner, s.clock[0], lease_seconds=180)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['one', 'two']))
    assert sum(r is not None for r in results) == 1
    winner = next(r for r in results if r)
    loser = 'two' if winner['owner'] == 'one' else 'one'
    with pytest.raises(ValueError, match='stale'):
        rs.finish_operation(s.store.db_path, op['id'], loser, 'resolved', {}, s.clock[0])
    status, detail = s.ctl.execute_workflow_recovery(winner, winner['owner'])
    rs.finish_operation(s.store.db_path, winner['id'], winner['owner'], status, detail, s.clock[0])
    s.tick()
    assert len(s.dispatches) == 1


@pytest.mark.parametrize('mismatch', ['candidate', 'missing_business', 'wrong_epoch'])
def test_delivery_never_bypasses_mismatched_gate(scene, mismatch):
    s = scene
    s.tick()
    s.store.save_task(dict(s.store.get_task('impl'), status='integrated', commit=B, integrated_commit=B))
    from herdr.scheduler_facts import record_candidate_frozen
    record_candidate_frozen('wf', B, db_path=s.store.db_path)
    s.store.save_task(dict(task_id='test-new', workflow_id='wf', node='test', status='working',
        run_id='gate-new', execution_id='execution', candidate_sha=B,
        completion_protocol='receipt-v1', completion_epoch='epoch', completion_identity_path='/identity',
        acceptance_criteria=['correct result']))
    report = publish_task_checkpoint('test-new', 'gate-new', 'epoch', 'gate report', 1,
        store=s.store, completed_steps=['verify'])
    if mismatch != 'missing_business':
        record_business_acceptance(s.store, 'test-new', 'gate-new', 'epoch', B, 'pass', [report], ['AC-1=pass'])
    s.store.save_task(dict(s.store.get_task('test-new'), status='completed', stage_verdict='pass',
        candidate_sha=A if mismatch == 'candidate' else B,
        verified_candidate_sha=A if mismatch == 'candidate' else B,
        completion_epoch='other' if mismatch == 'wrong_epoch' else 'epoch'))
    s.clock[0] += 31
    s.tick()
    assert s.op()['status'] == 'awaiting_result'


@pytest.mark.parametrize('settings', [dict(idle_seconds=0), dict(total_seconds=999999), dict(idle_seconds=True), []])
def test_invalid_budget_refuses_before_dispatch(scene, settings):
    s = scene
    wf = s.store.get_workflow('wf')
    config = dict(wf['config'], recovery_budget=settings)
    s.store.save_workflow(dict(wf, config=config))
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_budget_invalid'
    assert not s.dispatches and s.op()['attempts'] == 0


def test_delivery_receipt_before_task_projection_is_recovered_without_resend(scene, monkeypatch):
    s = scene
    original = s.store.update_task_metadata
    def crash(task_id, metadata, **kwargs):
        if metadata.get('rework_delivery') == 'delivered':
            raise OSError('crash after durable dispatch receipt before projection')
        return original(task_id, metadata, **kwargs)
    monkeypatch.setattr(s.store, 'update_task_metadata', crash)
    s.tick()
    op = s.op()
    assert s.store.get_task('impl')['rework_delivery'] == 'pending'
    assert len(s.store.list_events(event_type='rework_dispatched')) == 1
    monkeypatch.setattr(s.store, 'update_task_metadata', original)
    s.clock[0] = op['next_due_at']
    s.tick()
    assert s.op()['status'] == 'awaiting_result', s.op()['detail']
    assert s.store.get_task('impl')['rework_delivery'] == 'delivered'
    assert len(s.dispatches) == 1


@pytest.mark.parametrize('rotation', ['candidate', 'epoch', 'execution'])
def test_changed_identity_cannot_renew_original_operation(scene, rotation):
    s = scene
    s.tick()
    deadline = s.op()['detail']['result_deadline_at']
    s.clock[0] += 3500
    if rotation == 'execution':
        s.store.save_workflow(dict(s.store.get_workflow('wf'), execution_id='new-execution'))
    else:
        s.store.update_task_metadata('impl', {('candidate_sha' if rotation == 'candidate' else 'completion_epoch'): 'foreign'})
    checkpoint(s, 1)
    s.tick()
    assert s.op()['detail']['result_deadline_at'] == deadline
    s.clock[0] = deadline + 1
    s.tick()
    assert s.op()['status'] == 'waiting_human'


def test_config_edits_and_human_verify_do_not_replenish_budget(scene):
    s = scene
    s.faults['after_delivery'] = True
    s.tick()
    op = s.op()
    wf = s.store.get_workflow('wf')
    s.store.save_workflow(dict(wf, config=dict(wf['config'], recovery_budget={'idle_seconds': 3600, 'total_seconds': 86400})))
    s.faults['after_delivery'] = False
    rs.reconcile(s.store.db_path, 'wf', s.clock[0])
    rs.decide_operation(s.store.db_path, op['id'], s.op()['version'], 'human', 'verify', 'check existing receipt', s.clock[0])
    assert s.op()['attempts'] == 1
    s.tick()
    assert s.op()['status'] == 'awaiting_result'
    assert s.op()['detail']['total_deadline_at'] == op['detail']['total_deadline_at']
    assert s.op()['detail']['first_started_at'] == op['detail']['first_started_at']


def test_permanent_exception_after_barrier_stays_explicitly_blocked(scene, monkeypatch):
    s = scene
    def permanent(*args, **kwargs):
        raise ValueError('identity conflict')
    monkeypatch.setattr(s.ctl, '_rework_retry_task', permanent)
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_execution_failed'
    s.clock[0] += 600
    s.tick()
    assert s.op()['status'] == 'waiting_human' and not s.dispatches


def test_reopened_generation_cannot_use_prior_progress(scene):
    s = scene
    s.tick()
    deadline = s.op()['detail']['result_deadline_at']
    s.clock[0] += 3500
    # Reopen epoch changes even if a legacy caller retains execution_id.
    s.store.save_workflow(dict(s.store.get_workflow('wf'), reopened_at=s.clock[0]))
    checkpoint(s, 1)
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_generation_changed'
    assert s.op()['detail']['result_deadline_at'] == deadline


def test_legacy_reset_attempt_field_cannot_hide_prior_claim(scene):
    s = scene
    rs.reconcile(s.store.db_path, 'wf', s.clock[0])
    op = s.op()
    s.store.record_event('recovery.claimed', {'operation_id': op['id']},
        workflow_id='wf', source='recovery_store', timestamp=s.clock[0] - 100)
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_legacy_budget_unknown'
    assert not s.dispatches


def test_progress_backlog_after_restart_does_not_false_timeout(scene):
    s = scene
    s.tick()
    start = s.clock[0]
    for step in range(1, 65):
        s.clock[0] = start + step * 10
        checkpoint(s, step)
    for step, at in [(65, 3500), (66, 6500)]:
        s.clock[0] = start + at
        checkpoint(s, step)
    s.clock[0] = start + 6600
    for _ in range(3):
        s.tick()
        assert s.op()['status'] == 'awaiting_result', s.op()['detail']
        s.clock[0] += 31
    assert s.op()['detail']['result_deadline_at'] == start + 10100
    assert len(s.dispatches) == 1


def test_total_deadline_blocks_subsequent_effects(scene, monkeypatch):
    s = scene
    wf = s.store.get_workflow('wf')
    s.store.save_workflow(dict(wf, config=dict(wf['config'], recovery_budget={'idle_seconds': 60, 'total_seconds': 60})))
    original = s.ctl._rework_retry_task
    def elapsed(*args, **kwargs):
        result = original(*args, **kwargs)
        s.clock[0] += 70
        return result
    monkeypatch.setattr(s.ctl, '_rework_retry_task', elapsed)
    s.tick()
    assert s.store.get_task('test-old')['status'] == 'cleaned'  # no gate invalidation after cutoff
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_total_budget_exhausted'
    assert len(s.dispatches) == 1


def test_legacy_manual_verify_does_not_invent_result_window(scene):
    s = scene
    s.tick()
    op = s.op()
    detail = {k: v for k, v in op['detail'].items() if k not in {
        'first_started_at', 'total_deadline_at', 'result_deadline_at', 'idle_seconds',
        'progress_cursors', 'progress_event_floor', 'effect_phase'}}
    detail['reason'] = 'legacy_delivery_unknown'
    with rs._transaction(s.store.db_path) as conn:
        conn.execute("UPDATE workflow_recovery_operations SET status='waiting_human',detail_json=? WHERE id=?",
            (json.dumps(detail), op['id']))
    rs.reconcile(s.store.db_path, 'wf', s.clock[0])
    rs.decide_operation(s.store.db_path, op['id'], s.op()['version'], 'human', 'verify', 'inspect receipt only', s.clock[0])
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_legacy_budget_unknown'
    assert not s.op()['detail'].get('result_deadline_at')
    assert len(s.dispatches) == 1


def test_legacy_empty_human_detail_stays_human_on_fact_refresh(scene):
    s = scene
    rs.reconcile(s.store.db_path, 'wf', s.clock[0])
    op = s.op()
    with rs._transaction(s.store.db_path) as conn:
        conn.execute("UPDATE workflow_recovery_operations SET status='waiting_human',detail_json='{}' WHERE id=?", (op['id'],))
    s.store.update_task_metadata('test-old', {'stage_verdict_note': 'refreshed diagnosis'})
    s.tick()
    assert s.op()['status'] == 'waiting_human'
    assert 'first_started_at' not in s.op()['detail'] and not s.dispatches


def test_artifact_mutation_after_hash_cannot_renew(scene, monkeypatch):
    from herdr import task_checkpoint
    s = scene
    s.tick()
    deadline = s.op()['detail']['result_deadline_at']
    s.clock[0] += 3500
    checkpoint(s, 1)
    original = task_checkpoint._verify_receipt
    def mutate(*args, **kwargs):
        observation = original(*args, **kwargs)
        path = Path(observation.content_ref)
        path.chmod(0o600)
        path.write_text('corrupted after hash')
        return observation
    monkeypatch.setattr(task_checkpoint, '_verify_receipt', mutate)
    s.tick()
    assert s.op()['detail']['result_deadline_at'] == deadline
    assert not s.op()['detail']['progress_cursors']


@pytest.mark.parametrize('kind', ['successor', 'delivered_successor'])
def test_successor_epoch_rotation_cannot_reconsume_phase(scene, kind):
    from copy import deepcopy
    from herdr.recovery_policy import verified_progress, consume_progress
    from herdr.observation import ObservationStore
    from tests.test_workflow_recovery import OP, facts
    s = scene
    op = deepcopy(OP)
    op['detail'].update(first_started_at=s.clock[0], total_deadline_at=s.clock[0] + 10800,
        result_deadline_at=s.clock[0] + 3600, idle_seconds=3600, progress_event_floor=0, progress_cursors={})
    op['detail']['repair_map']['impl'].update(completion_epoch='epoch1', completion_identity_path='/epoch1')
    tasks = facts()
    task = tasks[0]
    task.update(candidate_sha='b' * 40, completion_protocol='receipt-v1', completion_epoch='epoch1',
        completion_identity_path='/epoch1')
    if kind == 'delivered_successor':
        op['payload']['affected_task_ids'] = ['impl-r2']
        op['detail']['source_runs'] = {'impl-r2': 'new'}
        op['detail']['repair_map'] = {'impl-r2': dict(kind=kind, task_id='impl-r2', run_id='new',
            source_run_id='new', completion_epoch='epoch1', completion_identity_path='/epoch1')}
    s.store.save_task(task)
    s.clock[0] += 3500
    publish_task_checkpoint('impl-r2', 'new', 'epoch1', 'same output', 1, store=s.store, completed_steps=['phase1'])
    wf = dict(workflow_id='wf', execution_id='gen', candidate_sha='b' * 40)
    def read():
        from herdr import state_db
        conn = state_db.get_readonly_db_connection(s.store.db_path)
        try:
            return verified_progress(conn, op, wf, tasks, ObservationStore(s.store.db_path))
        finally:
            conn.close()
    batch = read()
    progress = batch['progress']
    op['detail'] = consume_progress(op['detail'], progress, s.clock[0])
    deadline = op['detail']['result_deadline_at']
    task.update(completion_epoch='epoch2', completion_identity_path='/epoch2')
    s.store.save_task(task)
    s.clock[0] += 100
    publish_task_checkpoint('impl-r2', 'new', 'epoch2', 'same output', 1, store=s.store, completed_steps=['phase1'])
    batch = read()
    progress = batch['progress']
    assert consume_progress(op['detail'], progress, s.clock[0])['result_deadline_at'] == deadline


def test_artifact_mutation_between_preparation_and_writer_rejects_cas(scene, monkeypatch):
    from herdr import recovery_policy
    from herdr.observation import ObservationStore
    s = scene
    s.tick()
    deadline = s.op()['detail']['result_deadline_at']
    s.clock[0] += 3500
    receipt = checkpoint(s, 1)
    original = recovery_policy.verified_progress
    def mutate(*args, **kwargs):
        batch = original(*args, **kwargs)
        path = Path(ObservationStore(s.store.db_path).get(receipt['observation_id']).content_ref)
        path.chmod(0o600)
        path.write_text('mutated between prepare and write')
        return batch
    monkeypatch.setattr(recovery_policy, 'verified_progress', mutate)
    with pytest.raises(ValueError, match='artifact changed'):
        s.tick()
    assert s.op()['detail']['result_deadline_at'] == deadline
    assert not s.op()['detail']['progress_cursors']


def test_byte_budget_backlog_resumes_without_false_timeout(scene):
    s = scene
    s.tick()
    start = s.clock[0]
    task = s.store.get_task('impl')
    for step in range(1, 11):
        s.clock[0] = start + (step * 10 if step <= 8 else 3500 if step == 9 else 6500)
        publish_task_checkpoint('impl', 'run', task['completion_epoch'],
            str(step) + 'x' * (1024 * 1024 - len(str(step))), step, store=s.store,
            completed_steps=[f'phase-{step}'])
    s.clock[0] = start + 6600
    for _ in range(2):
        s.tick()
        assert s.op()['status'] == 'awaiting_result', s.op()['detail']
        s.clock[0] += 31
    assert s.op()['detail']['result_deadline_at'] == start + 10100


def test_exact_scene_b_first_fresh_checkpoint_after_old_deadline(scene):
    from herdr.task_checkpoint import read_task_checkpoints
    s = scene
    s.tick()
    s.clock[0] += 3700
    checkpoint(s, 1)
    task = s.store.get_task('impl')
    assert len(read_task_checkpoints('impl', 'run', task['completion_epoch'], store=s.store)['segments']) == 1
    s.tick()
    assert s.op()['status'] == 'awaiting_result'
    assert s.op()['detail']['result_deadline_at'] == s.clock[0] + 3600
    assert len(s.dispatches) == 1


def short_budget(s):
    wf = s.store.get_workflow('wf')
    s.store.save_workflow(dict(wf, config=dict(wf['config'], recovery_budget={'idle_seconds': 60, 'total_seconds': 60})))


def test_each_gate_invalidation_rechecks_total_budget(scene, monkeypatch):
    s = scene
    wf = s.store.get_workflow('wf')
    s.store.save_workflow(dict(wf, config=dict(wf['config'], nodes=wf['config']['nodes'] + [{'id': 'review'}])))
    s.store.save_task(dict(task_id='review-old', workflow_id='wf', node='review', status='cleaned',
        run_id='review-old', execution_id='execution', candidate_sha=A, stage_verdict='blocked'))
    short_budget(s)
    original = s.ctl.invalidate_for_fix_loop
    calls = []
    def elapsed(*args, **kwargs):
        calls.append(args[1])
        result = original(*args, **kwargs)
        s.clock[0] += 70
        return result
    monkeypatch.setattr(s.ctl, 'invalidate_for_fix_loop', elapsed)
    s.tick()
    assert len(calls) == 1
    assert s.op()['status'] == 'waiting_human'
    assert s.op()['detail']['reason'] == 'recovery_total_budget_exhausted'


def test_nested_gate_commands_recheck_total_budget(scene, monkeypatch):
    s = scene
    short_budget(s)
    s.store.save_task(dict(task_id='test-old2', workflow_id='wf', node='test', status='cleaned',
        run_id='test-old2', execution_id='execution', candidate_sha=A, stage_verdict='blocked'))
    original = s.ctl.subprocess.run
    calls = []
    def elapsed(argv, **kwargs):
        result = original(argv, **kwargs)
        if len(argv) > 1 and argv[1] == 'supersede':
            calls.append(argv[2])
            s.clock[0] += 70
        return result
    monkeypatch.setattr(s.ctl.subprocess, 'run', elapsed)
    s.tick()
    assert len(calls) == 1
    assert s.op()['detail']['reason'] == 'recovery_total_budget_exhausted'


def test_stage_advance_clear_rechecks_budget_after_last_gate(scene, monkeypatch):
    s = scene
    short_budget(s)
    original = s.ctl.invalidate_for_fix_loop
    cleared = []
    def elapsed(*args, **kwargs):
        result = original(*args, **kwargs)
        s.clock[0] += 70
        return result
    monkeypatch.setattr(s.ctl, 'invalidate_for_fix_loop', elapsed)
    monkeypatch.setattr(s.ctl, 'clear_stage_advance', lambda *a: cleared.append(a))
    s.tick()
    assert not cleared
    assert s.op()['detail']['reason'] == 'recovery_total_budget_exhausted'


def test_finalize_transition_rechecks_budget_after_escalation_clear(scene, monkeypatch):
    s = scene
    s.store.save_task(dict(s.store.get_task('test-old'), status='superseded'))
    s.store.save_task(dict(s.store.get_task('impl'), status='committed', finalize_escalated=True))
    short_budget(s)
    rs.reconcile(s.store.db_path, 'wf', s.clock[0])
    op = next(o for o in rs.list_operations(s.store.db_path, 'wf') if o['payload']['kind'] == 'finalize')
    rs.decide_operation(s.store.db_path, op['id'], op['version'], 'human', 'retry', 'integration prerequisites resolved', s.clock[0])
    finalizes = []
    def clear(task_id):
        s.store.update_task_metadata(task_id, {'finalize_escalated': False})
        s.clock[0] += 70
        return True
    monkeypatch.setattr(s.ctl, 'clear_finalize_escalation', clear)
    monkeypatch.setattr(s.ctl, 'finalize_completed_task', lambda *a: finalizes.append(a))
    s.tick()
    actual = next(o for o in rs.list_operations(s.store.db_path, 'wf') if o['id'] == op['id'])
    assert not finalizes
    assert actual['detail']['reason'] == 'recovery_total_budget_exhausted'


def test_changed_gate_facts_between_effects_require_replanning(scene, monkeypatch):
    s = scene
    wf = s.store.get_workflow('wf')
    s.store.save_workflow(dict(wf, config=dict(wf['config'], nodes=wf['config']['nodes'] + [{'id': 'review'}])))
    s.store.save_task(dict(task_id='review-old', workflow_id='wf', node='review', status='cleaned',
        run_id='review-old', execution_id='execution', candidate_sha=A, stage_verdict='blocked'))
    original = s.ctl.invalidate_for_fix_loop
    calls = []
    def change(*args, **kwargs):
        calls.append(args[1])
        result = original(*args, **kwargs)
        s.store.update_task_metadata('test-old', {'stage_verdict_note': 'new business blocker scope'})
        return result
    monkeypatch.setattr(s.ctl, 'invalidate_for_fix_loop', change)
    s.tick()
    assert calls == ['review']
    assert s.op()['status'] == 'waiting_human'


@pytest.fixture
def finalize_scene(tmp_path, monkeypatch):
    """Real finalize recovery over a real Git repository and clone.

    The controlled clock, claim, budget, guard and ownership machinery stay
    real; only the top-level ``herdr-task`` command transport is wrapped so
    each step really executes, is recorded, and may spend controlled time.
    """
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('HERDR_CONTROLLER_DIR', str(tmp_path / 'controller'))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'docs'))
    store = SQLiteStateStore(tmp_path / 'state.db')
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    for key, value in [('user.name', 'Recovery test'), ('user.email', 'recovery@example.invalid')]:
        subprocess.run(['git', '-C', str(repo), 'config', key, value], check=True)
    (repo / 'seed.txt').write_text('original candidate\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'seed', '--no-gpg-sign'], check=True)
    baseline = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    clone = tmp_path / 'worker'
    subprocess.run(['git', 'clone', '-q', str(repo), str(clone)], check=True)
    subprocess.run(['git', '-C', str(clone), 'switch', '-qc', 'agent/test/fix-impl'], check=True)
    for key, value in [('user.name', 'Recovery test'), ('user.email', 'recovery@example.invalid')]:
        subprocess.run(['git', '-C', str(clone), 'config', key, value], check=True)
    (clone / 'seed.txt').write_text('agent deliverable awaiting commit\n')
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    config = {'nodes': [{'id': 'implementation'}, {'id': 'test', 'gate': {'retry_node': 'implementation'}}]}
    store.save_workflow(dict(workflow_id='wf', status='running', startup_ready=True,
        execution_id='execution', project_root=str(repo), base_branch='main', created_at=1, config=config))
    store.record_event('candidate_frozen', {'candidate_sha': baseline}, workflow_id='wf',
        source='critical-path-scheduler', timestamp=2)
    store.save_task(dict(task_id='impl', workflow_id='wf', node='implementation', status='completed',
        run_id='run', execution_id='execution', candidate_sha=baseline,
        completion_protocol='receipt-v1', completion_epoch='initial', completion_identity_path='/initial'))
    wf, _, _ = rs.read_snapshot(store.db_path, 'wf')
    store.update_task_metadata('impl', dict(clone_path=str(clone), branch='agent/test/fix-impl',
        source_repo=str(repo), base_branch='main', integration_mode='git', baseline_commit=baseline,
        baseline_fingerprint={'tracked': {}, 'untracked': {}}, candidate_episode_id=wf['candidate_episode_id']))
    ctl = importlib.import_module('services.herdr-controller')
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(ctl, 'load_tasks', store.list_tasks)
    monkeypatch.setattr(ctl, 'TASK_MANAGER', str(Path(__file__).resolve().parents[1] / 'bin/herdr-task'))
    from herdr import task_resources
    monkeypatch.setattr(task_resources, 'owned_live_pane', lambda _: (True, 'controlled native instance'))
    monkeypatch.setattr(ctl, 'reconcile_node_dispatches', lambda *a, **k: None)
    monkeypatch.setattr(ctl, 'verify_auto_recover_redispatch', lambda *a: None)
    monkeypatch.setattr(ctl, 'converge_dangling_replacement_obligations', lambda *a: None)
    monkeypatch.setattr(ctl, 'notify_attention', lambda *a, **k: True)
    advances = []
    monkeypatch.setattr(ctl, 'enqueue_stage_advance', lambda task: advances.append(task['task_id']))
    real_run = subprocess.run
    calls, faults, delays, after = [], {'preflight': 0}, {}, {}
    def run(argv, **kwargs):
        entry = [str(item) for item in argv]
        calls.append(entry)
        if entry[0] in ('git', 'ps'):
            if entry[0] == 'git' and 'status' in entry and faults['preflight']:
                faults['preflight'] -= 1
                raise subprocess.TimeoutExpired(argv, 10)
            return real_run(argv, **kwargs)
        if Path(entry[0]).name == 'herdr-task':
            result = real_run(argv, **kwargs)
            hook = after.get(entry[1])
            if hook:
                hook(result)
            clock[0] += delays.get(entry[1], 0)
            return result
        raise AssertionError(f'unexpected native command {argv}')
    monkeypatch.setattr(subprocess, 'run', run)
    def finalize_op():
        return next(o for o in rs.list_operations(store.db_path, 'wf')
                    if o['payload']['kind'] == 'finalize')
    return SimpleNamespace(store=store, ctl=ctl, clock=clock, faults=faults, delays=delays,
        after=after, calls=calls, advances=advances, real_run=real_run, clone=clone, repo=repo,
        baseline=baseline, finalize_op=finalize_op,
        tick=lambda: ctl.check_workflow_recovery('wf', now=clock[0]))


def authorize_finalize_recovery(s):
    """Register the finalize obligation for the escalated task, then retry it."""
    s.store.save_task(dict(s.store.get_task('impl'), finalize_escalated=True))
    rs.reconcile(s.store.db_path, 'wf', s.clock[0])
    op = s.finalize_op()
    rs.decide_operation(s.store.db_path, op['id'], op['version'], 'human',
                        'retry', 'integration prerequisites resolved', s.clock[0])
    return op


def retry_after_preflight_failure(s, expected_total=None):
    """First claim dies on a typed transient preflight fault, exactly as in production."""
    s.faults['preflight'] = 1
    s.tick()
    op = s.finalize_op()
    assert op['status'] == 'pending', json.dumps(op['detail'])
    assert op['attempts'] == 1 and op['started'] == 0
    if expected_total is not None:
        assert op['detail']['total_deadline_at'] == expected_total
    return op


def integrate_calls(s):
    return [c for c in s.calls if Path(c[0]).name == 'herdr-task' and len(c) > 1 and c[1] == 'integrate']


def test_finalize_commit_crossing_total_deadline_never_starts_integrate(finalize_scene):
    """Default budget: preflight retry lands 60s before the total deadline, the
    real commit then runs 70s and crosses it. The already-produced commit is
    kept, and integrate must not start past the deadline."""
    s = finalize_scene
    start = s.clock[0]
    authorize_finalize_recovery(s)
    op = retry_after_preflight_failure(s)
    s.delays['commit'] = 70
    s.clock[0] = op['detail']['total_deadline_at'] - 60
    s.tick()
    assert integrate_calls(s) == [], s.calls
    assert [c[1] for c in s.calls if Path(c[0]).name == 'herdr-task'] == ['commit'], s.calls
    final = s.finalize_op()
    assert final['status'] == 'waiting_human', json.dumps(final['detail'])
    assert final['detail']['reason'] == 'recovery_total_budget_exhausted'
    assert final['detail']['total_deadline_at'] == start + 10800
    assert final['detail']['first_started_at'] == start
    task = s.store.get_task('impl')
    head = subprocess.check_output(['git', '-C', str(s.clone), 'rev-parse', 'HEAD'], text=True).strip()
    assert task['status'] == 'committed' and task['commit'] == head and head != s.baseline
    # A rescan neither resets the budget nor repeats the confirmed commit.
    s.clock[0] += 30
    s.tick()
    final = s.finalize_op()
    assert final['status'] == 'waiting_human'
    assert final['detail']['reason'] == 'recovery_total_budget_exhausted'
    assert final['detail']['total_deadline_at'] == start + 10800
    assert [c[1] for c in s.calls if Path(c[0]).name == 'herdr-task'] == ['commit'], s.calls


def test_finalize_next_action_refused_once_total_deadline_passed(finalize_scene):
    """Short budget: integrate starts inside the authorization window and ends
    past the total deadline; its real result is kept and every later new action
    (cleanup_ready transition, cleanup, business advance) is refused."""
    s = finalize_scene
    start = s.clock[0]
    authorize_finalize_recovery(s)
    wf = s.store.get_workflow('wf')
    s.store.save_workflow(dict(wf, config=dict(wf['config'],
        recovery_budget={'idle_seconds': 60, 'total_seconds': 120})))
    retry_after_preflight_failure(s, expected_total=start + 120)
    s.delays['commit'] = 30
    s.delays['integrate'] = 70
    s.clock[0] = start + 30
    s.tick()
    assert len(integrate_calls(s)) == 1, s.calls
    final = s.finalize_op()
    assert final['status'] == 'waiting_human', json.dumps(final['detail'])
    assert final['detail']['reason'] == 'recovery_total_budget_exhausted'
    assert final['detail']['total_deadline_at'] == start + 120
    task = s.store.get_task('impl')
    integrated = task['integrated_commit']
    assert task['status'] == 'integrated' and integrated and integrated != s.baseline
    subprocess.run(['git', '-C', str(s.repo), 'rev-parse', '--quiet', '--verify',
                    integrated + '^{commit}'], check=True)
    assert not s.advances
    assert [c[1] for c in s.calls if Path(c[0]).name == 'herdr-task'] == ['commit', 'integrate'], s.calls
    s.clock[0] += 30
    s.tick()
    assert s.finalize_op()['detail']['reason'] == 'recovery_total_budget_exhausted'
    assert [c[1] for c in s.calls if Path(c[0]).name == 'herdr-task'] == ['commit', 'integrate'], s.calls


def test_finalize_lease_expiry_during_commit_hands_over_without_integrate(finalize_scene):
    """When the lease lapses while the commit subprocess runs, the old owner
    must neither start integrate nor overwrite the operation state; the lease
    machinery owns recovery from there."""
    s = finalize_scene
    op = authorize_finalize_recovery(s)
    op = retry_after_preflight_failure(s)
    owner = op['owner']
    s.delays['commit'] = 200
    s.clock[0] = op['detail']['total_deadline_at'] - 60
    s.tick()
    assert integrate_calls(s) == [], s.calls
    task = s.store.get_task('impl')
    head = subprocess.check_output(['git', '-C', str(s.clone), 'rev-parse', 'HEAD'], text=True).strip()
    assert task['status'] == 'committed' and task['commit'] == head
    with pytest.raises(ValueError, match='stale'):
        rs.finish_operation(s.store.db_path, op['id'], owner, 'waiting_human',
                            {'reason': 'late'}, s.clock[0])
    s.clock[0] += 30
    s.tick()
    handed = s.finalize_op()
    assert handed['owner'] != owner or handed['status'] != 'running'
    assert handed['status'] in ('pending', 'awaiting_result', 'waiting_human')
    assert integrate_calls(s) == [], s.calls


@pytest.mark.parametrize('drift', ['execution', 'commit', 'run'])
def test_finalize_foreign_drift_after_own_commit_refuses_integrate(finalize_scene, drift):
    """A change the operation cannot explain (other execution, foreign SHA, or
    foreign run) blocks the next action even though our own commit succeeded."""
    s = finalize_scene
    op = authorize_finalize_recovery(s)
    op = retry_after_preflight_failure(s)
    def inject_foreign(result):
        if drift == 'execution':
            s.store.update_task_metadata('impl', {'execution_id': 'foreign-execution'})
        elif drift == 'commit':
            s.store.update_task_metadata('impl', {'commit': 'c' * 40})
        else:
            s.store.update_task_metadata('impl', {'run_id': 'foreign-run'})
    s.after['commit'] = inject_foreign
    s.clock[0] = op['detail']['total_deadline_at'] - 60
    s.tick()
    assert integrate_calls(s) == [], s.calls
    final = s.finalize_op()
    assert final['status'] == 'waiting_human', json.dumps(final['detail'])
    assert final['detail']['reason'] == 'recovery_execution_failed'
    task = s.store.get_task('impl')
    assert task['status'] == 'committed'


def test_finalize_own_commit_is_accepted_and_chain_completes(finalize_scene):
    """Happy path: the operation's own real commit produces a brand-new SHA and
    the guard accepts it; integrate, cleanup and business advance all run."""
    s = finalize_scene
    authorize_finalize_recovery(s)
    op = retry_after_preflight_failure(s)
    s.clock[0] = op['detail']['total_deadline_at'] - 60
    s.tick()
    assert len(integrate_calls(s)) == 1, s.calls
    task = s.store.get_task('impl')
    head = subprocess.check_output(['git', '-C', str(s.clone), 'rev-parse', 'HEAD'], text=True).strip()
    assert task['commit'] == head and head != s.baseline
    op = s.finalize_op()
    assert op['status'] == 'awaiting_result', json.dumps(op['detail'])
    assert op['detail']['finalize_commits'] == {'impl': head}
    s.clock[0] += 30
    s.tick()
    final = s.finalize_op()
    assert final['status'] == 'resolved', json.dumps(final['detail'])
    assert final['detail']['reason'] == 'delivery_finalized'
    assert s.advances == ['impl']
    assert [c[1] for c in s.calls if Path(c[0]).name == 'herdr-task'] == ['commit', 'integrate', 'set', 'cleanup'], s.calls


def test_finalize_none_mode_preservation_commit_keeps_chain(finalize_scene):
    """None-integration recovery finalize adopts its own preservation commit
    SHA and still reaches the normal cleaned terminal state."""
    s = finalize_scene
    s.store.update_task_metadata('impl', {'integration_mode': 'none'})
    (s.clone / 'seed.txt').write_text('worktree output to preserve\n')
    authorize_finalize_recovery(s)
    op = retry_after_preflight_failure(s)
    s.clock[0] = op['detail']['total_deadline_at'] - 60
    s.tick()
    task = s.store.get_task('impl')
    head = subprocess.check_output(['git', '-C', str(s.clone), 'rev-parse', 'HEAD'], text=True).strip()
    assert task['commit'] == head and head != s.baseline
    op = s.finalize_op()
    assert op['status'] == 'awaiting_result', json.dumps(op['detail'])
    assert op['detail']['finalize_commits'] == {'impl': head}
    s.clock[0] += 30
    s.tick()
    final = s.finalize_op()
    assert final['status'] == 'resolved', json.dumps(final['detail'])
    assert final['detail']['reason'] == 'delivery_finalized'
    assert [c[1] for c in s.calls if Path(c[0]).name == 'herdr-task'] == ['commit', 'set', 'cleanup'], s.calls
