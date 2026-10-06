"""Regression contracts from wf-nexusarchive-1005-01; all state is temporary."""
import json

import pytest

from herdr import recovery_store, state_db
from herdr.state_store import get_state_store, reset_state_store
from herdr.workflow_progress import assess_workflow

SHA = 'a' * 40
CFG = {'nodes': [{'id': 'implementation'}, {'id': 'test'}, {'id': 'review'}]}


def workflow(**changes):
    return dict({'workflow_id': 'wf', 'status': 'running', 'startup_ready': True,
                 'execution_id': 'generation', 'candidate_sha': SHA, 'created_at': 100,
                 'config': CFG}, **changes)


def task(tid='test', node='test', **changes):
    return dict({'task_id': tid, 'workflow_id': 'wf', 'node': node, 'stage': node,
                 'run_id': 'run-' + tid, 'execution_id': 'generation', 'status': 'cleaned',
                 'candidate_sha': SHA, 'stage_verdict': 'blocked'}, **changes)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    reset_state_store()
    value = get_state_store(tmp_path / 'state.db')
    yield value
    reset_state_store()


def test_missing_workflow_generation_never_plans_effect():
    wf = workflow()
    wf.pop('execution_id')
    result = assess_workflow(wf, CFG, [task(), task('impl', 'implementation',
        status='committed', stage_verdict=None, commit=SHA)])
    assert result['obligations'][0]['status'] == 'waiting_human'
    assert result['obligations'][0]['reason'] == 'identity_unknown'


def test_missing_task_run_never_plans_effect():
    result = assess_workflow(workflow(), CFG, [task(run_id=None),
        task('impl', 'implementation', status='committed', stage_verdict=None, commit=SHA)])
    assert result['obligations'][0]['status'] == 'waiting_human'
    assert result['obligations'][0]['reason'] == 'identity_unknown'


def test_public_snapshot_uses_frozen_candidate_and_is_readonly(store):
    store.save_workflow(workflow(candidate_sha='b' * 40))
    store.save_task(task())
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf',
                       source='critical-path-scheduler', timestamp=101)
    reader = getattr(recovery_store, 'read_snapshot', None)
    assert callable(reader), 'public shared transaction snapshot is missing'
    before = store.list_events(workflow_id='wf')
    wf, config, tasks = reader(store.db_path, 'wf')
    assert wf['candidate_sha'] == SHA
    assert config == CFG
    assert tasks[0]['run_id'] == 'run-test'
    assert store.list_events(workflow_id='wf') == before
    conn = state_db.get_readonly_db_connection(store.db_path)
    assert json.loads(conn.execute('SELECT metadata_json FROM workflows').fetchone()[0])['candidate_sha'] == 'b' * 40
    conn.close()


def test_snapshot_missing_database_does_not_create_it(tmp_path):
    reader = getattr(recovery_store, 'read_snapshot', None)
    assert callable(reader), 'shared read-only snapshot is missing'
    path = tmp_path / 'absent.db'
    with pytest.raises(FileNotFoundError):
        reader(path, 'wf')
    assert not path.exists()


def legacy(store):
    wf = workflow(project_root=str(store.db_path.parent))
    wf.pop('execution_id')
    store.save_workflow(wf)
    store.save_task(task(execution_id=None, completion_protocol='receipt-v1',
        completion_epoch='epoch', completion_identity_path='/identity'))
    store.record_event('initial_dispatched', {'delivery_phase': 'dispatched',
        'completion_epoch': 'epoch', 'identity_path': '/identity', 'intervention_id': 'initial'},
        task_id='test', run_id='run-test', workflow_id='wf', node_id='test', source='herdr-task', timestamp=101)
    store.save_task(task('impl', 'implementation', status='committed', stage_verdict=None, commit=SHA))


def test_migration_requires_current_epoch_receipt_and_preserves_history(store):
    from herdr import workflow_repair_migration as migration
    legacy(store)
    plan = migration.plan(store.db_path, 'wf')
    assert plan['execution_id'] == 'generation'
    receipt = migration.apply(store.db_path, plan)
    assert store.get_workflow('wf')['execution_id'] == 'generation'
    assert store.get_task('test')['stage_verdict'] == 'blocked'
    assert store.get_task('test')['execution_id'] == 'generation'
    migration.rollback(store.db_path, receipt)
    assert not store.get_workflow('wf').get('execution_id')
    assert not store.get_task('test').get('execution_id')


def test_migration_rejects_old_receipt_and_stale_cas(store):
    from herdr import workflow_repair_migration as migration
    legacy(store)
    plan = migration.plan(store.db_path, 'wf')
    state_db.update_task_metadata('test', {'completion_epoch': 'new'}, db_path=store.db_path)
    with pytest.raises(ValueError, match='changed'):
        migration.apply(store.db_path, plan)
    with pytest.raises(ValueError, match='receipt'):
        migration.plan(store.db_path, 'wf')


def test_migration_rollback_rejects_progress(store):
    from herdr import workflow_repair_migration as migration
    legacy(store)
    receipt = migration.apply(store.db_path, migration.plan(store.db_path, 'wf'))
    state_db.update_task_metadata('impl', {'note': 'new progress'}, db_path=store.db_path)
    with pytest.raises(ValueError, match='changed'):
        migration.rollback(store.db_path, receipt)


def test_inflight_rework_does_not_create_another_repair():
    result = assess_workflow(workflow(), CFG, [task('impl', 'implementation',
        status='rework', stage_verdict=None, rework_delivery='delivered')])
    assert not result['can_advance']
    assert result['obligations'] == []


def test_old_gate_failure_is_not_current_rework_input():
    result = assess_workflow(workflow(), CFG, [task(candidate_sha='b' * 40)])
    assert result['obligations'] == []
    assert result['can_advance']


def test_integration_keeps_checked_out_source_branch_and_persists_rebased_sha(store, tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    def git(repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-b', 'main')
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.invalid')
    (source / 'base').write_text('base')
    git(source, 'add', '.'); git(source, 'commit', '-m', 'base')
    clone = tmp_path / 'clone'
    subprocess.run(['git', 'clone', str(source), str(clone)], check=True, capture_output=True)
    git(clone, 'config', 'user.name', 'Test'); git(clone, 'config', 'user.email', 'test@example.invalid')
    git(clone, 'switch', '-c', 'agent/test/fix-impl')
    (clone / 'change').write_text('fix')
    git(clone, 'add', '.'); git(clone, 'commit', '-m', 'fix')
    old = git(clone, 'rev-parse', 'HEAD')
    (source / 'advance').write_text('new base')
    git(source, 'add', '.'); git(source, 'commit', '-m', 'advance')
    git(source, 'switch', '-c', 'agent/test/fix-impl')
    source_head = git(source, 'rev-parse', 'HEAD')
    store.save_workflow(workflow(project_root=str(source), base_branch='main'))
    store.save_task(task('impl', 'implementation', status='committed', stage_verdict=None,
        commit=old, clone_path=str(clone), source_repo=str(source), branch='agent/test/fix-impl',
        base_branch='main', onto_branch='agent/test/fix-impl',
        baseline_fingerprint={'tracked': {}, 'untracked': {}}))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'bin/herdr-task'),
        'integrate', 'impl'], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    final = git(clone, 'rev-parse', 'HEAD')
    assert final != old
    assert store.get_task('impl')['commit'] == final == store.get_task('impl')['integrated_commit']
    assert git(source, 'rev-parse', 'refs/herdr/tasks/impl') == final
    assert git(source, 'rev-parse', 'HEAD') == source_head
    # Simulate the crash after integrated was durable but before escalation clear.
    state_db.update_task_metadata('impl', {'finalize_escalated': True, 'finalize_escalate_reason': 'retry_exhausted'}, db_path=store.db_path)
    retry = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'bin/herdr-task'),
        'integrate', 'impl'], capture_output=True, text=True, timeout=30, check=False)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert store.get_task('impl')['finalize_escalated'] is False


def test_late_candidate_publication_rejects_stale_episode(store):
    from herdr import scheduler_facts
    store.save_workflow(workflow())
    first = scheduler_facts.record_candidate_frozen('wf', SHA, db_path=store.db_path)
    episode = first['event']['id']
    scheduler_facts.record_candidate_frozen('wf', 'b' * 40, db_path=store.db_path,
        expected_episode_id=episode)
    with pytest.raises(ValueError, match='episode'):
        scheduler_facts.record_candidate_frozen('wf', 'c' * 40, db_path=store.db_path,
            expected_episode_id=episode)
    assert scheduler_facts.latest_frozen_candidate_sha('wf', db_path=store.db_path) == 'b' * 40


def test_gate_budget_rejects_repair_before_delivery():
    cfg = {'nodes': [{'id': 'implementation'}, {'id': 'test', 'max_tasks_per_node': 1}]}
    result = assess_workflow(workflow(), cfg, [task(), task('impl', 'implementation',
        status='agent_done', stage_verdict=None)])
    assert result['obligations'][0]['reason'] == 'verifier_budget_exhausted'
    assert result['obligations'][0]['status'] == 'waiting_human'


def test_generic_frontend_metrics_do_not_prove_business_acceptance():
    from herdr.evaluator import calculate_metrics
    metrics = calculate_metrics(test_output='4248 passed', test_exit_code=0)
    assert metrics.correctness == 100
    assert metrics.business_acceptance == 'unknown'


def test_budget_extension_is_bounded_and_cas(store):
    from herdr.node_config import extend_budget, read_configuration
    store.save_workflow(workflow(config={'nodes': [{'id': 'test', 'max_tasks_per_node': 4}]}))
    _, _, digest = read_configuration(store, 'wf')
    result = extend_budget(store, 'wf', 'test', expected_sha=digest, additional=2,
                           operator='tester', reason='one test and one retry')
    assert result['max_tasks_per_node'] == 6
    with pytest.raises(ValueError, match='changed'):
        extend_budget(store, 'wf', 'test', expected_sha=digest, additional=1,
                      operator='tester', reason='stale')
    _, _, digest = read_configuration(store, 'wf')
    with pytest.raises(ValueError, match='bounded'):
        extend_budget(store, 'wf', 'test', expected_sha=digest, additional=100,
                      operator='tester', reason='unbounded')


def test_business_acceptance_requires_verified_checkpoint_on_current_candidate(store):
    from herdr.task_checkpoint import (
        has_business_acceptance,
        publish_task_checkpoint,
        record_business_acceptance,
    )
    store.save_workflow(workflow())
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf',
                       source='critical-path-scheduler', timestamp=101)
    store.save_task(task(status='working', stage_verdict=None, completion_epoch='epoch',
                         completion_protocol='receipt-v1', acceptance_criteria=['D1']))
    with pytest.raises(ValueError, match='checkpoint'):
        record_business_acceptance(store, 'test', 'run-test', 'epoch', SHA, 'pass', [], ['D1'])
    cp = publish_task_checkpoint('test', 'run-test', 'epoch', 'D1: real endpoint assertions pass', 1, store=store)
    ref = {'observation_id': cp['observation_id'], 'sha256': cp['sha256']}
    record_business_acceptance(store, 'test', 'run-test', 'epoch', SHA, 'pass', [ref], ['D1'])
    assert has_business_acceptance(store, store.get_task('test'), SHA)
    assert not has_business_acceptance(store, store.get_task('test'), 'b' * 40)
    state_db.update_task_metadata('test', {'completion_epoch': 'next'}, db_path=store.db_path)
    assert not has_business_acceptance(store, store.get_task('test'), SHA)


def test_metadata_only_candidate_is_not_published(store):
    store.save_workflow(workflow())
    assert recovery_store.read_snapshot(store.db_path, 'wf')[0].get('candidate_sha') is None


def test_migrated_started_receipt_is_consumed_without_resending(store, monkeypatch):
    import importlib.util
    import time
    from pathlib import Path

    from herdr import workflow_repair_migration as migration
    legacy(store)
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf',
                       source='critical-path-scheduler', timestamp=101)
    state_db.update_task_metadata('impl', {'rework_delivery': 'delivered', 'rework_request_id': 'delivered',
        'completion_protocol': 'receipt-v1', 'completion_epoch': 'repair-epoch',
        'completion_identity_path': '/repair-identity'}, db_path=store.db_path)
    store.record_event('rework_dispatched', {'delivery_phase': 'dispatched', 'intervention_id': 'delivered',
        'completion_epoch': 'repair-epoch', 'identity_path': '/repair-identity'},
        task_id='impl', run_id='run-impl', workflow_id='wf', node_id='implementation', source='herdr-task')
    op = recovery_store.list_operations(store.db_path, 'wf')[0]
    detail = {'execution_id': None, 'rework_ids': ['impl'], 'successor_ids': [],
        'target_runs': {'impl': 'run-impl'}, 'source_runs': {'impl': 'run-impl'},
        'rework_requests': {'impl': 'delivered'}, 'gate_nodes': ['test', 'review'],
        'repair_map': {'impl': {'kind': 'rework', 'task_id': 'impl', 'run_id': 'run-impl',
            'source_run_id': 'run-impl', 'request_id': 'delivered',
            'completion_epoch': 'repair-epoch', 'completion_identity_path': '/repair-identity'}}}
    conn = state_db.get_db_connection(store.db_path)
    conn.execute("UPDATE workflow_recovery_operations SET started=1,status='waiting_human',detail_json=? WHERE id=?", (json.dumps(detail), op['id']))
    conn.close()
    migration.apply(store.db_path, migration.plan(store.db_path, 'wf'))
    recovery_store.reconcile(store.db_path, 'wf')
    current = recovery_store.list_operations(store.db_path, 'wf')[0]
    assert current['id'] == op['id'] and current['started']
    assert current['detail']['rework_requests'] == {'impl': 'delivered'}
    recovery_store.decide_operation(store.db_path, current['id'], current['version'], 'tester', 'verify', 'consume receipt', time.time())
    claimed = recovery_store.claim_operation(store.db_path, current['id'], 'owner', time.time(), lease_seconds=180)
    spec = importlib.util.spec_from_file_location('contract_controller', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    def forbid(*args, **kwargs):
        raise AssertionError('a delivered repair must never be resent')
    monkeypatch.setattr(ctl, '_rework_retry_task', forbid)
    monkeypatch.setattr(ctl.subprocess, 'run', forbid)
    cleared = []
    monkeypatch.setattr(ctl, 'clear_stage_advance', lambda *args: cleared.append(args))
    def invalidate(wid, gate, config, retry_node):
        assert retry_node is None
        t = store.get_task(gate)
        if t:
            store.save_task(dict(t, status='superseded'))
    monkeypatch.setattr(ctl, 'invalidate_for_fix_loop', invalidate)
    status, receipt = ctl.execute_workflow_recovery(claimed, 'owner')
    assert status == 'awaiting_result', receipt
    assert receipt['invalidated_gate_nodes'] == ['test']
    assert cleared == [('wf', 'implementation')]


def test_snapshot_keeps_one_generation_across_concurrent_writer(store, monkeypatch):
    store.save_workflow(workflow())
    store.save_task(task())
    conn = state_db.get_readonly_db_connection(store.db_path)
    changed = []
    def interleave(sql):
        if 'FROM tasks' in sql and not changed:
            changed.append(True)
            writer = state_db.get_db_connection(store.db_path)
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE workflows SET status='paused' WHERE workflow_id='wf'")
            row = state_db._decode_task_row(writer.execute("SELECT * FROM tasks WHERE task_id='test'").fetchone())
            state_db.save_task(dict(row, run_id='next-run'), conn=writer)
            writer.commit(); writer.close()
            changed.append('committed')
    conn.set_trace_callback(interleave)
    monkeypatch.setattr(state_db, 'get_readonly_db_connection', lambda _: conn)
    wf, _, tasks = recovery_store.read_snapshot(store.db_path, 'wf')
    assert changed == [True, 'committed']
    assert wf['status'] == 'running'
    assert tasks[0]['run_id'] == 'run-test'


def test_migration_rejects_ambiguous_generation_and_all_unknown(store):
    from herdr import workflow_repair_migration as migration
    legacy(store)
    store.save_task(task('other', execution_id='another'))
    with pytest.raises(ValueError, match='generation'):
        migration.plan(store.db_path, 'wf')
    state_db.update_task_metadata('other', {'execution_id': None}, db_path=store.db_path)
    state_db.update_task_metadata('impl', {'execution_id': None}, db_path=store.db_path)
    with pytest.raises(ValueError, match='generation'):
        migration.plan(store.db_path, 'wf')


def test_migration_rechecks_external_configuration_bytes(store, tmp_path):
    from herdr import workflow_repair_migration as migration
    legacy(store)
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps(CFG))
    wf = store.get_workflow('wf')
    store.save_workflow(dict(wf, config={}, workflow_file=str(path)))
    plan = migration.plan(store.db_path, 'wf')
    path.write_text(json.dumps({'nodes': [{'id': 'other'}]}))
    with pytest.raises(ValueError, match='changed'):
        migration.apply(store.db_path, plan)
    assert not store.get_workflow('wf').get('execution_id')


def test_worker_new_task_owns_branch_separately_from_onto(tmp_path, monkeypatch):
    from tests.test_fix_loop_pr1 import _worker
    from tests.test_pinned_local_onto import git, repository
    source, sha = repository(tmp_path)
    monkeypatch.setattr(_worker, '_registered_tasks', lambda: [
        {'task_id': 'original-owner', 'branch': 'candidate/local', 'status': 'working'}])
    branch = _worker.checkout_onto_branch(source, 'candidate/local', candidate_sha=sha,
        task_id='new', agent='test', task_type='fix')
    assert branch == 'agent/test/fix-new'
    assert git(source, 'rev-parse', 'HEAD') == sha
    assert git(source, 'rev-parse', 'candidate/local') == sha


def test_cleanup_and_report_activity_cannot_rotate_candidate(store, monkeypatch):
    import importlib.util
    from pathlib import Path
    store.save_workflow(workflow())
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf',
                       source='critical-path-scheduler', timestamp=101)
    store.save_task(task('old', 'implementation', status='cleaned', stage_verdict=None,
                         commit='b' * 40, candidate_sha='b' * 40, branch='old-branch'))
    spec = importlib.util.spec_from_file_location('publication_controller', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl.scheduler_core, 'resolve_candidate_sha_for_branch', lambda *args: SHA)
    assert ctl._scheduler_freeze_candidate('wf', str(store.db_path.parent), 'implementation', []) == SHA
    state_db.update_task_metadata('old', {'note': 'late appended report'}, db_path=store.db_path)
    assert ctl._scheduler_freeze_candidate('wf', str(store.db_path.parent), 'implementation', []) == SHA
    assert len(store.list_events(workflow_id='wf', event_type='candidate_frozen')) == 1


def test_empty_operation_consumes_task_current_delivered_rework(store, monkeypatch):
    import importlib.util
    import time
    from pathlib import Path
    store.save_workflow(workflow(project_root=str(store.db_path.parent)))
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf', source='critical-path-scheduler', timestamp=101)
    store.save_task(task('impl', 'implementation', status='rework', stage_verdict=None,
        rework_delivery='delivered', rework_request_id='ALREADY_SENT', completion_protocol='receipt-v1',
        completion_epoch='epoch', completion_identity_path='/identity'))
    store.record_event('rework_dispatched', {'delivery_phase': 'dispatched', 'intervention_id': 'ALREADY_SENT',
        'completion_epoch': 'epoch', 'identity_path': '/identity'}, task_id='impl', run_id='run-impl',
        workflow_id='wf', node_id='implementation', source='herdr-task')
    store.save_task(task())
    op = recovery_store.list_operations(store.db_path, 'wf')[0]
    assert op['detail'] == {}
    claimed = recovery_store.claim_operation(store.db_path, op['id'], 'owner', time.time(), lease_seconds=180)
    spec = importlib.util.spec_from_file_location('existing_task_delivery_controller', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    def forbid(*args, **kwargs):
        raise AssertionError('existing current Run delivery must not be resent')
    monkeypatch.setattr(ctl, '_rework_retry_task', forbid)
    monkeypatch.setattr(ctl, 'clear_stage_advance', lambda *args: None)
    monkeypatch.setattr(ctl, 'invalidate_for_fix_loop', lambda *args, **kwargs: store.save_task(dict(store.get_task('test'), status='superseded')))
    status, receipt = ctl.execute_workflow_recovery(claimed, 'owner')
    assert status == 'awaiting_result', receipt
    assert receipt['rework_requests'] == {'impl': 'ALREADY_SENT'}
    assert store.get_task('impl')['rework_request_id'] == 'ALREADY_SENT'


def test_late_integrated_task_cannot_publish_against_newer_episode(store):
    from herdr import scheduler_facts
    store.save_workflow(workflow())
    a = scheduler_facts.record_candidate_frozen('wf', SHA, db_path=store.db_path)['event']['id']
    scheduler_facts.record_candidate_frozen('wf', 'b' * 40, db_path=store.db_path)
    store.save_task(task('old', 'implementation', status='integrated', stage_verdict=None,
        commit=SHA, integrated_commit=SHA, integration_ref='refs/herdr/tasks/old',
        integration_publication_episode_id=a))
    with pytest.raises(ValueError, match='episode'):
        scheduler_facts.publish_integrated_candidate(store, 'old')
    assert scheduler_facts.latest_frozen_candidate_sha('wf', db_path=store.db_path) == 'b' * 40
    assert not store.list_events(workflow_id='wf', event_type='candidate_published')


def test_partial_frontend_criterion_cannot_cover_required_java(store):
    from herdr.task_checkpoint import (
        publish_task_checkpoint,
        record_business_acceptance,
    )
    store.save_workflow(workflow())
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf', source='critical-path-scheduler', timestamp=101)
    store.save_task(task(status='working', stage_verdict=None, completion_epoch='epoch',
        acceptance_criteria=['frontend', 'Java authorization']))
    cp = publish_task_checkpoint('test', 'run-test', 'epoch', 'frontend PASS; Java FAIL', 1, store=store)
    ref = {'observation_id': cp['observation_id'], 'sha256': cp['sha256']}
    with pytest.raises(ValueError, match='criteria'):
        record_business_acceptance(store, 'test', 'run-test', 'epoch', SHA, 'pass', [ref], ['frontend'])


def test_reopened_generation_cannot_read_old_controller_candidate(store):
    import importlib.util
    from pathlib import Path

    from herdr import scheduler_facts
    store.save_workflow(workflow(reopened_at=300))
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf', source='critical-path-scheduler', timestamp=200)
    assert recovery_store.read_snapshot(store.db_path, 'wf')[0]['candidate_sha'] is None
    spec = importlib.util.spec_from_file_location('reopened_controller', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    assert ctl._scheduler_current_frozen_candidate_sha('wf') == ''
    assert scheduler_facts.list_candidate_frozen_events('wf', db_path=store.db_path) == []


def test_migration_audit_does_not_duplicate_sensitive_history(store):
    from herdr import workflow_repair_migration as migration
    legacy(store)
    secret = 'FAKE_MIGRATION_SECRET_1006'
    state_db.update_task_metadata('impl', {'note': 'api_key=' + secret}, db_path=store.db_path)
    receipt = migration.apply(store.db_path, migration.plan(store.db_path, 'wf'))
    assert secret not in json.dumps(store.list_events(workflow_id='wf', event_type='workflow_identity_migrated'))
    migration.rollback(store.db_path, receipt)
    assert store.get_task('impl')['note'] == 'api_key=' + secret


def test_business_external_validation_does_not_hold_sqlite_write_lock(store, monkeypatch):
    import sqlite3

    from herdr import task_checkpoint as cp
    store.save_workflow(workflow())
    store.save_workflow({'workflow_id': 'other', 'status': 'running'})
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf', source='critical-path-scheduler', timestamp=101)
    store.save_task(task(status='working', stage_verdict=None, completion_epoch='epoch', acceptance_criteria=['D1']))
    segment = cp.publish_task_checkpoint('test', 'run-test', 'epoch', 'D1 pass', 1, store=store)
    ref = {'observation_id': segment['observation_id'], 'sha256': segment['sha256'], 'secret': 'FAKE_EXTRA_ARTIFACT_SECRET'}
    real = cp.validate_checkpoint_artifact
    writes = []
    def validate(*args, **kwargs):
        writer = sqlite3.connect(store.db_path, timeout=0.05)
        try:
            writer.execute("UPDATE workflows SET title='progress' WHERE workflow_id='other'")
            writer.commit(); writes.append(True)
        finally:
            writer.close()
        return real(*args, **kwargs)
    monkeypatch.setattr(cp, 'validate_checkpoint_artifact', validate)
    result = cp.record_business_acceptance(store, 'test', 'run-test', 'epoch', SHA, 'pass', [ref], ['D1'])
    assert writes
    assert 'FAKE_EXTRA_ARTIFACT_SECRET' not in json.dumps(result)


def test_active_delivered_successor_is_consumed_as_current_target(store):
    from herdr.workflow_recovery import existing_delivery_details, repair_coverage
    store.save_workflow(workflow())
    old = task('old', 'implementation', status='committed', stage_verdict=None, commit=SHA, superseded_by='new')
    lineage = {'predecessor_id': 'old', 'successor_id': 'new', 'predecessor_run_id': 'run-old',
               'successor_run_id': 'run-new', 'candidate_sha': SHA}
    new = task('new', 'implementation', status='working', stage_verdict=None, supersedes='old',
               recovery_lineage=lineage, completion_protocol='receipt-v1', completion_epoch='epoch',
               completion_identity_path='/identity')
    store.save_task(old); store.save_task(new)
    store.record_event('initial_dispatched', {'delivery_phase': 'dispatched', 'intervention_id': 'initial',
        'completion_epoch': 'epoch', 'identity_path': '/identity'}, workflow_id='wf', task_id='new',
        run_id='run-new', node_id='implementation', source='herdr-task')
    operation = {'workflow_id': 'wf', 'payload': {'kind': 'fix_loop', 'candidate_sha': SHA,
        'affected_task_ids': ['new']}, 'detail': {}}
    detail = existing_delivery_details(operation, workflow(), [old, new], store)
    assert detail is not None, 'in-flight INITIAL successor must not be dispatched again'
    operation['detail'] = detail
    assert repair_coverage(operation, [old, new]) == []


def test_generic_success_report_does_not_certify_business_or_completion():
    from herdr.evaluator import (
        calculate_metrics,
        render_evaluation_markdown,
        render_metrics_markdown,
    )
    metrics = calculate_metrics(test_output='Tests  4248 passed (4248)', test_exit_code=0,
                                lint_output='', lint_exit_code=0)
    assert metrics.composite_score == 100
    for report in (render_metrics_markdown(metrics, 1, 3), render_evaluation_markdown(metrics, 1, 3)):
        assert '业务验收：unknown' in report
        assert 'DoD)：完全满足' not in report
        assert '安全提交并完成当前工单' not in report
        assert '单元/集成测试：全部通过' not in report


def business_forward_fixture(store):
    from herdr.task_checkpoint import (
        publish_task_checkpoint,
        record_business_acceptance,
    )
    cfg = {'nodes': [{'id': 'implementation'}, {'id': 'test', 'depends_on': ['implementation']},
                    {'id': 'review', 'depends_on': ['implementation']},
                    {'id': 'wrapup', 'depends_on': ['test', 'review']}]}
    store.save_workflow(workflow(config=cfg))
    store.record_event('candidate_frozen', {'candidate_sha': SHA}, workflow_id='wf',
                       source='critical-path-scheduler', timestamp=101)
    store.save_task(task('impl', 'implementation', status='cleaned', stage_verdict=None, commit=SHA))
    for tid in ['test', 'review']:
        store.save_task(task(tid, tid, stage_verdict='pass', verified_candidate_sha=SHA,
                            completion_protocol='receipt-v1', completion_epoch='epoch', acceptance_criteria=['D1']))
        ref = publish_task_checkpoint(tid, 'run-' + tid, 'epoch', 'D1 endpoint proof', 1, store=store)
        record_business_acceptance(store, tid, 'run-' + tid, 'epoch', SHA,
                                  'pass' if tid == 'test' else 'blocked',
                                  [{'observation_id': ref['observation_id'], 'sha256': ref['sha256']}],
                                  ['AC-1=pass' if tid == 'test' else 'AC-1=blocked'])
    return cfg


def test_kernel_forward_does_not_use_stagepass_over_business_blocked(store):
    from herdr import kernel
    business_forward_fixture(store)
    before = store.list_events(workflow_id='wf')
    result = kernel.step_workflow('wf')
    assert result['ok'] is False, 'business blocked must reject before forward transition'
    assert result['reason'] == 'business_acceptance_unavailable'
    assert store.get_workflow('wf')['status'] == 'running'
    assert store.list_events(workflow_id='wf') == before


def test_controller_join_rejects_business_blocked(store):
    import importlib.util
    from pathlib import Path
    cfg = business_forward_fixture(store)
    spec = importlib.util.spec_from_file_location('business_join_controller', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    assert ctl._scheduler_join_gate_allows('wf', cfg['nodes'][-1], store.list_tasks(workflow_id='wf')) is False


def business_review_pass(store):
    from herdr.task_checkpoint import read_task_checkpoints, record_business_acceptance
    t = store.get_task('review')
    report = read_task_checkpoints('review', t['run_id'], t['completion_epoch'], store=store)
    x = report['segments'][0]
    record_business_acceptance(store, 'review', t['run_id'], t['completion_epoch'], SHA, 'pass',
                              [{'observation_id': x['observation_id'], 'sha256': x['sha256']}], ['AC-1=pass'])


def test_business_forward_current_complete_proof_allows_step(store):
    from herdr import kernel
    business_forward_fixture(store)
    business_review_pass(store)
    result = kernel.step_workflow('wf')
    assert result['ok'] is True and result['stepped_node'] == 'wrapup'


@pytest.mark.parametrize('change', ['epoch', 'run', 'generation', 'episode', 'artifact'])
def test_business_forward_rejects_stale_or_changed_proof(store, change):
    from pathlib import Path

    from herdr.observation import ObservationStore
    from herdr.business_gate import business_gate_blockers
    from herdr.task_checkpoint import read_task_checkpoints
    business_forward_fixture(store); business_review_pass(store)
    if change == 'artifact':
        t = store.get_task('review')
        report = read_task_checkpoints('review', t['run_id'], t['completion_epoch'], store=store)
        obs = ObservationStore(store.db_path).get(report['segments'][0]['observation_id'])
        Path(obs.content_ref).chmod(0o600)
        Path(obs.content_ref).write_text('changed')
    else:
        field, value = {'epoch': ('completion_epoch', 'new'), 'run': ('run_id', 'new'),
                        'generation': ('execution_id', 'new'), 'episode': ('candidate_episode_id', -1)}[change]
        if field == 'run_id':
            conn = state_db.get_db_connection(store.db_path)
            conn.execute("UPDATE tasks SET payload_json=json_set(payload_json,'$.run_id',?),version=version+1 WHERE task_id='review'", (value,))
            conn.commit(); conn.close()
        else:
            store.update_task_metadata('review', {field: value})
    wf, cfg, tasks = recovery_store.read_snapshot(store.db_path, 'wf')
    assert business_gate_blockers(store, wf, cfg, tasks, 'wrapup') == ['review']
    assert not recovery_store.list_operations(store.db_path, 'wf'), 'unknown proof is not a new repair failure'


def test_business_forward_rechecks_newer_blocked_during_artifact_validation(store, monkeypatch):
    import herdr.task_checkpoint as cp
    business_forward_fixture(store); business_review_pass(store)
    original = cp.validate_checkpoint_artifact
    injected = False
    def changed(task_record, reference, **kwargs):
        nonlocal injected
        result = original(task_record, reference, **kwargs)
        if task_record['task_id'] == 'review' and not injected:
            injected = True
            store.record_event('business_acceptance_recorded', {'epoch': 'epoch', 'candidate_sha': SHA,
                'execution_id': 'generation', 'verdict': 'blocked'}, workflow_id='wf', task_id='review',
                run_id='run-review', source='checkpoint')
        return result
    monkeypatch.setattr(cp, 'validate_checkpoint_artifact', changed)
    wf, cfg, tasks = recovery_store.read_snapshot(store.db_path, 'wf')
    assert __import__('herdr.business_gate', fromlist=['business_gate_blockers']).business_gate_blockers(store, wf, cfg, tasks, 'wrapup') == ['review']


def task_cli_module():
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    loader = importlib.machinery.SourceFileLoader('business_forward_task_cli', str(Path(__file__).resolve().parents[1] / 'bin/herdr-task'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec); loader.exec_module(module)
    return module


def test_cli_launch_business_rejects_before_intent_or_routing(store, monkeypatch):
    import argparse
    business_forward_fixture(store); cli = task_cli_module()
    monkeypatch.setattr(cli, 'project_for_workflow', lambda _: store.get_workflow('wf'))
    monkeypatch.setattr(cli, 'load_tasks', lambda: store.export_tasks_json())
    args = argparse.Namespace(task_id='wrapup-new', workflow_id='wf', node='wrapup', stage=None,
        run_id=None, execution_id=None, agent_policy=None, artifact_mode=None, integration_mode='none',
        task_type='docs', supersedes=None, dispatch_role='worker', dispatch_round=1)
    before = store.list_events(workflow_id='wf')
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2
    assert store.list_events(workflow_id='wf') == before
    assert store.get_task('wrapup-new') is None


def test_close_business_rejects_before_claim_or_teardown(store):
    business_forward_fixture(store); cli = task_cli_module()
    before = store.list_events(workflow_id='wf')
    with pytest.raises(SystemExit) as exc:
        cli.close_workflow('wf')
    assert exc.value.code == 2
    assert store.list_events(workflow_id='wf') == before
    assert store.get_workflow('wf')['status'] == 'running'


def test_pr_publication_business_rejects_exact_delivery_gate(store):
    from herdr.pr_delivery import _publication
    business_forward_fixture(store)
    impl = dict(store.get_task('impl'), status='integrated', integrated_commit=SHA,
                integration_branch='herdr/integration-impl', integration_ref='refs/herdr/tasks/impl')
    note = {'kind': 'delivery', 'candidate_sha': SHA, 'delivery_branch': 'herdr/integration-impl',
            'review_task': 'review', 'test_gate': 'test', 'body': 'explicit delivery'}
    with pytest.raises(ValueError, match='business acceptance'):
        _publication(impl, [note], store)


def test_launch_capacity_prefers_pinned_config_over_mutable_file(tmp_path):
    cli = task_cli_module()
    path = tmp_path / 'old-workflow.json'
    path.write_text('invalid stale file must not be read')
    cfg = {'nodes': [{'id': 'review', 'max_tasks_per_node': 6}]}
    result = cli._capacity_definition({'config': cfg, 'workflow_file': str(path)})
    assert result['nodes'][0]['max_tasks_per_node'] == 6


def test_business_forward_single_dependency_is_also_checked(store):
    from herdr.business_gate import business_gate_blockers
    cfg = business_forward_fixture(store)
    cfg['nodes'].append({'id': 'postreview', 'depends_on': ['review']})
    store.save_workflow(dict(store.get_workflow('wf'), config=cfg))
    wf, config, tasks = recovery_store.read_snapshot(store.db_path, 'wf')
    assert business_gate_blockers(store, wf, config, tasks, 'postreview') == ['review']


def test_controller_business_guard_uses_primary_when_task_projection_is_empty(store):
    import importlib.util
    from pathlib import Path
    cfg = business_forward_fixture(store)
    spec = importlib.util.spec_from_file_location('primary_business_join_controller', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)
    assert ctl._scheduler_join_gate_allows('wf', cfg['nodes'][-1], []) is False


def test_business_forward_rechecks_entire_group_after_later_gate_hashing(store, monkeypatch):
    import herdr.task_checkpoint as cp
    from herdr import kernel
    business_forward_fixture(store); business_review_pass(store)
    original = cp.validate_checkpoint_artifact
    injected = False
    def changed(task_record, reference, **kwargs):
        nonlocal injected
        result = original(task_record, reference, **kwargs)
        if task_record['task_id'] == 'test' and not injected:
            injected = True
            store.record_event('business_acceptance_recorded', {'epoch': 'epoch', 'candidate_sha': SHA,
                'execution_id': 'generation', 'verdict': 'blocked'}, workflow_id='wf', task_id='review',
                run_id='run-review', source='checkpoint')
        return result
    monkeypatch.setattr(cp, 'validate_checkpoint_artifact', changed)
    result = kernel.step_workflow('wf')
    assert result['ok'] is False, 'earlier review proof changed while later test was hashing'
    assert result['reason'] == 'business_acceptance_unavailable'
    assert store.get_workflow('wf')['status'] == 'running'


@pytest.mark.parametrize('mutation', ['configuration', 'new_head', 'review_blocked'])
def test_business_publication_rechecks_cohort_after_external_validation(store, monkeypatch, mutation):
    import herdr.task_checkpoint as cp
    from herdr.business_gate import business_gate_blockers
    from herdr.pr_delivery import _publication
    cfg = business_forward_fixture(store); business_review_pass(store)
    original = cp.validate_checkpoint_artifact
    injected = False
    def changed(task_record, reference, **kwargs):
        nonlocal injected
        result = original(task_record, reference, **kwargs)
        if task_record['task_id'] == 'test' and not injected:
            injected = True
            if mutation == 'configuration':
                cfg['nodes'][-1]['depends_on'].append('new-gate')
                store.save_workflow(dict(store.get_workflow('wf'), config=cfg))
            elif mutation == 'new_head':
                new = dict(store.get_task('review'), task_id='new-review', created_at=200,
                           run_id='run-new-review', superseded_by=None)
                store.save_task(new)
            else:
                store.record_event('business_acceptance_recorded', {'epoch': 'epoch', 'candidate_sha': SHA,
                    'execution_id': 'generation', 'verdict': 'blocked'}, workflow_id='wf', task_id='review',
                    run_id='run-review', source='checkpoint')
        return result
    monkeypatch.setattr(cp, 'validate_checkpoint_artifact', changed)
    wf, config, tasks = recovery_store.read_snapshot(store.db_path, 'wf')
    if mutation == 'review_blocked':
        impl = dict(store.get_task('impl'), status='integrated', integrated_commit=SHA,
            integration_branch='herdr/integration-impl', integration_ref='refs/herdr/tasks/impl')
        note = {'kind': 'delivery', 'candidate_sha': SHA, 'delivery_branch': 'herdr/integration-impl',
            'review_task': 'review', 'test_gate': 'test', 'body': 'explicit delivery'}
        with pytest.raises(ValueError, match='business acceptance'):
            _publication(impl, [note], store)
    else:
        assert business_gate_blockers(store, wf, config, tasks, 'wrapup')
    assert injected
