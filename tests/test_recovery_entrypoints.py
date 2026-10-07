"""Cross-entry regressions: cleanup and escalation must not hide blocking facts."""
from herdr import kernel
from herdr.controller_actions import resolve_workflow_blockers
from herdr.state_store import get_state_store, reset_state_store
import importlib.util
from pathlib import Path
import json
import os
import subprocess
import sys


def test_cli_recovery_status_is_read_only_for_absent_database(tmp_path):
    path = tmp_path / 'absent.db'
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'bin/herdr-task'),
        'recovery-status', '--workflow-id', 'wf'], capture_output=True, text=True,
        env={**os.environ, 'HERDR_STATE_DB': str(path)}, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['operations'] == []
    assert not path.exists()


def test_controller_records_failure_even_without_coordinator_or_ready_join(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    reset_state_store()
    store = get_state_store(tmp_path / 'state.db')
    wf = {'workflow_id': 'wf', 'status': 'running', 'execution_id': 'gen', 'candidate_sha': 'a' * 40,
          'config': {'nodes': [{'id': 'implementation'}, {'id': 'test', 'depends_on': ['implementation']},
                               {'id': 'review', 'depends_on': ['implementation']},
                               {'id': 'wrapup', 'depends_on': ['test', 'review']}]}}
    store.save_workflow(wf)
    store.save_task({'task_id': 'test', 'workflow_id': 'wf', 'node': 'test', 'run_id': 'run',
                     'candidate_sha': 'a' * 40, 'status': 'cleaned', 'stage_verdict': 'blocked'})
    spec = importlib.util.spec_from_file_location('recovery_controller_test', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'workflow_closed', lambda _: False)
    monkeypatch.setattr(ctl, 'project_for_workflow', lambda _: wf)
    monkeypatch.setattr(ctl, 'workflow_config_for', lambda _: wf['config'])
    monkeypatch.setattr(ctl, 'coordinator_pane_for_workflow', lambda _: None)
    # Simulate legacy facts predating operation schema/registration.
    from herdr import state_db, recovery_store
    conn = state_db.get_db_connection(store.db_path)
    conn.execute('DELETE FROM workflow_recovery_operations')
    conn.close()
    ctl.check_workflow_stage_advance('wf')
    operations = recovery_store.list_operations(store.db_path, 'wf')
    assert len(operations) == 2
    failures = [op for op in operations if op['payload'].get('kind') == 'fix_loop']
    assert len(failures) == 1
    assert failures[0]['payload']['task_ids'] == ['test']
    assert failures[0]['status'] == 'waiting_human'
    dispatches = [op for op in operations if op['payload'].get('kind') == 'node_dispatch']
    assert len(dispatches) == 1
    assert dispatches[0]['payload']['node_id'] == 'implementation'
    assert dispatches[0]['status'] == 'pending'
    assert dispatches[0]['started'] == 0
    assert ctl.coordinator_queue.empty()  # blocked gate still prevents positive dispatch
    reset_state_store()


def test_cleaned_verdict_and_committed_escalation_visible_to_console():
    tasks = [
        {'task_id': 'test', 'workflow_id': 'wf', 'status': 'cleaned', 'stage_verdict': 'blocked'},
        {'task_id': 'impl', 'workflow_id': 'wf', 'status': 'committed', 'finalize_escalated': True},
        {'task_id': 'old', 'workflow_id': 'wf', 'status': 'cleaned', 'stage_verdict': 'blocked', 'superseded_by': 'new'},
        {'task_id': 'foreign', 'workflow_id': 'other', 'status': 'failed'},
    ]
    assert [t['task_id'] for t in resolve_workflow_blockers(tasks, {'workflow_id': 'wf'})] == ['test', 'impl']


def test_manual_step_cannot_bypass_blocked_parallel_gate(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    reset_state_store()
    store = get_state_store(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf', 'status': 'running', 'config': {'nodes': [
        {'id': 'implementation'}, {'id': 'test', 'depends_on': ['implementation']},
        {'id': 'review', 'depends_on': ['implementation']}]}})
    store.save_task({'task_id': 'impl', 'workflow_id': 'wf', 'node': 'implementation', 'status': 'cleaned'})
    store.save_task({'task_id': 'test', 'workflow_id': 'wf', 'node': 'test', 'status': 'cleaned', 'stage_verdict': 'blocked'})
    result = kernel.step_workflow('wf')
    assert result['ok'] is False
    assert result['reason'] == 'workflow_blocked'
    assert store.get_workflow('wf')['status'] == 'running'
    reset_state_store()


def test_failed_candidate_never_finalizes_via_legacy_watcher(tmp_path, monkeypatch):
    store = get_state_store(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf', 'status': 'running'})
    store.save_task({'task_id': 'impl', 'workflow_id': 'wf', 'node': 'implementation',
                    'status': 'committed', 'integration_mode': 'git', 'commit': 'a' * 40})
    store.save_task({'task_id': 'test', 'workflow_id': 'wf', 'node': 'test',
                    'status': 'cleaned', 'stage_verdict': 'blocked', 'candidate_sha': 'a' * 40})
    spec = importlib.util.spec_from_file_location('finalize_recovery_test', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'get_task', store.get_task)
    monkeypatch.setattr(ctl, 'maybe_complete_on_task_done', lambda _: [])
    def forbid(*args, **kwargs):
        raise AssertionError('failed candidate must not reach git transport')
    monkeypatch.setattr(ctl.subprocess, 'run', forbid)
    result = ctl.finalize_completed_task('impl')
    assert result == {'retryable': False, 'kind': 'candidate_blocked'}
    assert store.get_task('impl')['status'] == 'committed'
    reset_state_store()


def _real_committed_launch_scene(tmp_path, monkeypatch):
    """Keep CLI, intent, delivery receipt and lineage real; replace native resources."""
    import argparse
    import importlib.machinery
    from herdr import task_resources

    repo = tmp_path / 'repo'
    repo.mkdir()
    for argv in (["init", "-q", "-b", "main"], ["config", "user.name", "test"],
                 ["config", "user.email", "test@example.invalid"]):
        subprocess.run(['git', '-C', str(repo), *argv], check=True, capture_output=True)
    (repo / 'seed').write_text('failed candidate')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'seed', '--no-gpg-sign'],
                   check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path / 'state.db'))
    monkeypatch.setenv('HERDR_WORKFLOW_DOCS_DIR', str(tmp_path / 'docs'))
    monkeypatch.setenv('HERDR_CLONES_DIR', str(tmp_path / 'clones'))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    reset_state_store()
    store = get_state_store(tmp_path / 'state.db')
    config = {'nodes': [{'id': 'implementation', 'max_tasks_per_node': 10}]}
    project = dict(workflow_id='wf', status='running', execution_id='execution',
                   project_root=str(repo), base_branch='main', config=config,
                   candidate_sha=sha, startup_ready=True)
    store.save_workflow(project)
    store.record_event('candidate_frozen', {'candidate_sha': sha}, workflow_id='wf', source='critical-path-scheduler')
    store.save_task(dict(task_id='old', workflow_id='wf', node='implementation',
                        status='committed', run_id='old-run', execution_id='execution',
                        commit=sha, branch='old-branch', dispatch_role='worker', dispatch_round=1))
    loader = importlib.machinery.SourceFileLoader('entrypoint_real_recovery_cli',
        str(Path(__file__).resolve().parents[1] / 'bin/herdr-task'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    monkeypatch.setattr(cli, '_get_store', lambda: store)
    monkeypatch.setattr(cli, 'project_for_workflow', lambda _: store.get_workflow('wf'))
    monkeypatch.setattr(cli, 'choose_agent', lambda *a, **kw: 'opencode')
    monkeypatch.setattr(cli, 'release_agent_reservation', lambda *a, **kw: None)
    monkeypatch.setattr(cli, 'ensure_stage_topology', lambda *a: dict(
        workspace_id='ws', anchor_pane_id='anchor', tab_id='tab'))
    monkeypatch.setattr(cli, 'acquire_pane_for_task', lambda *a: None)
    monkeypatch.setattr(cli, 'auto_init_task_loop', lambda *a, **kw: None)
    # Native instance probe is an external dependency, not the receipt verifier.
    monkeypatch.setattr(task_resources, 'owned_live_pane', lambda _: (True, 'test native instance'))
    calls = {'worker': 0, 'prompt': 0, 'fail_prompt': False}
    real_run = subprocess.run
    clone = tmp_path / 'clones' / 'new'
    clone.mkdir(parents=True)
    def transport(cmd, **kwargs):
        argv = list(map(str, cmd))
        if argv[0] == 'git':
            return real_run(cmd, **kwargs)
        if argv[0].endswith('herdr-worker.py'):
            calls['worker'] += 1
            body = dict(clone=str(clone), branch='new-branch', pane_id='pane',
                        pane_source='dynamic', baseline_commit=sha,
                        agent_session_id='session', agent_name='opencode')
            return subprocess.CompletedProcess(cmd, 0, 'HERDR_WORKER_RESULT='+json.dumps(body), '')
        if argv[:3] == ['herdr', 'agent', 'prompt']:
            calls['prompt'] += 1
            return subprocess.CompletedProcess(cmd, 1 if calls['fail_prompt'] else 0, '', '')
        raise AssertionError('Unexpected external transport: '+repr(argv))
    monkeypatch.setattr(cli.subprocess, 'run', transport)
    def args():
        return argparse.Namespace(task_id='new', workflow_id='wf', run_id='new-run',
            node='implementation', stage='implementation', agent='auto', source=str(repo),
            integration_mode='git', task_type='fix', goal='repair candidate', acceptance=['AC'],
            prompt='repair', supersedes='old', supersede_reason='failed candidate',
            candidate_sha=sha, onto=None, execution_id='execution', dispatch_role='worker',
            dispatch_round=2)
    return cli, store, args, calls, sha


def test_real_launch_registered_but_transport_unknown_cannot_link(tmp_path, monkeypatch):
    import pytest
    from herdr.supervisor_delivery import DeliveryUnknown
    from herdr.recovery_successor import has_confirmed_delivery
    cli, store, args, calls, sha = _real_committed_launch_scene(tmp_path, monkeypatch)
    calls['fail_prompt'] = True
    with pytest.raises(DeliveryUnknown):
        cli._launch_task(args())
    task = store.get_task('new')
    assert task['recovery_predecessor'] == 'old'
    assert task['candidate_sha'] == sha
    assert not has_confirmed_delivery(store, task)
    assert not store.get_task('old').get('superseded_by')
    assert store.list_events(event_type='committed_successor_linked') == []
    assert store.list_events(task_id='new', event_type='initial_dispatched') == []
    assert calls['worker'] == calls['prompt'] == 1
    # Duplicate discovery must verify actual receipt, never count registration
    # as delivery or create another Worker/transport after ambiguous native I/O.
    with pytest.raises(SystemExit) as error:
        cli._launch_task(args())
    assert error.value.code == 2
    assert calls['worker'] == calls['prompt'] == 1
    assert not store.get_task('old').get('superseded_by')
    reset_state_store()


def test_real_launch_receipt_survives_prelink_crash_and_retry_does_not_dispatch(tmp_path, monkeypatch):
    import pytest
    from herdr import state_db
    from herdr.recovery_successor import has_confirmed_delivery
    cli, store, args, calls, sha = _real_committed_launch_scene(tmp_path, monkeypatch)
    old_before = store.get_task('old')
    real_record = state_db.record_event
    def fail_link(event, **kwargs):
        if event['event_type'] == 'committed_successor_linked':
            raise RuntimeError('injected interruption before lineage transaction commits')
        return real_record(event, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(state_db, 'record_event', fail_link)
        with pytest.raises(RuntimeError, match='injected interruption'):
            cli._launch_task(args())
    assert has_confirmed_delivery(store, store.get_task('new'))
    assert not store.get_task('old').get('superseded_by')
    assert not store.get_task('new').get('supersedes')
    assert store.list_events(event_type='committed_successor_linked') == []
    cli._launch_task(args())
    cli._launch_task(args())
    old, new = store.get_task('old'), store.get_task('new')
    assert old['status'] == 'committed' and old['commit'] == sha
    assert old['run_id'] == old_before['run_id']
    assert old['superseded_by'] == 'new' and new['supersedes'] == 'old'
    assert new['run_id'] == 'new-run' and new['run_id'] != old['run_id']
    assert new['recovery_lineage']['candidate_sha'] == sha
    assert calls['worker'] == calls['prompt'] == 1
    assert len(store.list_events(task_id='new', event_type='initial_dispatched')) == 1
    assert len(store.list_events(event_type='committed_successor_linked')) == 1
    reset_state_store()


def test_retired_committed_history_never_finalizes_after_gate_invalidation(tmp_path, monkeypatch):
    store = get_state_store(tmp_path / 'state.db')
    store.save_workflow({'workflow_id': 'wf', 'status': 'running'})
    store.save_task({'task_id': 'old', 'workflow_id': 'wf', 'status': 'committed',
                    'integration_mode': 'git', 'commit': 'a' * 40, 'superseded_by': 'new'})
    store.save_task({'task_id': 'test', 'workflow_id': 'wf', 'node': 'test',
                    'status': 'superseded', 'stage_verdict': 'blocked', 'candidate_sha': 'a' * 40})
    spec = importlib.util.spec_from_file_location('retired_recovery_test', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'get_task', store.get_task)
    monkeypatch.setattr(ctl, 'maybe_complete_on_task_done', lambda _: [])
    def forbid(*args, **kwargs):
        raise AssertionError('retired committed history must not reach git transport')
    monkeypatch.setattr(ctl.subprocess, 'run', forbid)
    assert ctl.finalize_completed_task('old')['kind'] == 'retired_lineage'
    assert store.get_task('old')['status'] == 'committed'
    reset_state_store()


def test_partial_repair_verification_preserves_all_failed_gates(tmp_path, monkeypatch):
    from herdr import recovery_store
    cli, store, args, calls, sha = _real_committed_launch_scene(tmp_path, monkeypatch)
    workflow = store.get_workflow('wf')
    config = {'nodes': [{'id': 'implementation', 'max_tasks_per_node': 10},
                        {'id': 'test', 'gate': {'retry_node': 'implementation'}}]}
    store.save_workflow(dict(workflow, config=config))
    store.save_task(dict(task_id='old2', workflow_id='wf', node='implementation',
                        status='cleaned', run_id='old2-run', execution_id='execution', commit=sha))
    store.save_task(dict(task_id='test', workflow_id='wf', node='test', status='cleaned',
                        run_id='test-run', execution_id='execution', candidate_sha=sha,
                        stage_verdict='blocked', stage_verdict_affected_task_ids=['old','old2']))
    op = recovery_store.list_operations(store.db_path, 'wf')[0]
    now = op['next_due_at'] + 1
    claimed = recovery_store.claim_operation(store.db_path, op['id'], 'owner', now)
    recovery_store.record_step(store.db_path, op['id'], 'owner', 'successor_launch_started',
        {'successor_ids':['new'], 'source_runs':{'old':'old-run','old2':'old2-run'}}, now, validate=True)
    cli._launch_task(args())
    new = store.get_task('new')
    details = dict(action='verify', successor_ids=['new'], rework_ids=[],
                   target_runs={'new': new['run_id']}, source_runs={'old':'old-run','old2':'old2-run'},
                   execution_id='execution', gate_nodes=['test'],
                   repair_map={'old': {'kind':'successor', 'task_id':'new',
                                        'run_id':new['run_id'], 'source_run_id':'old-run'}})
    recovery_store.record_step(store.db_path, op['id'], 'owner', 'delivery_confirmed', details, now + 1, validate=False)
    spec = importlib.util.spec_from_file_location('partial_repair_controller_test', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'workflow_config_for', lambda _: config)
    def forbid(*args, **kwargs):
        raise AssertionError('partial repair must not invalidate any failed gate')
    monkeypatch.setattr(ctl, 'invalidate_for_fix_loop', forbid)
    status, result = ctl.execute_workflow_recovery(dict(claimed, detail=details), 'owner')
    assert status == 'waiting_human'
    assert result['reason'] == 'repair_coverage_incomplete'
    assert result['missing_task_ids'] == ['old2']
    assert store.get_task('test')['stage_verdict'] == 'blocked'
    assert calls['worker'] == calls['prompt'] == 1
    reset_state_store()


def test_previous_rework_delivery_never_authorizes_new_recovery(tmp_path, monkeypatch):
    from herdr import recovery_store
    cli, store, args, calls, sha = _real_committed_launch_scene(tmp_path, monkeypatch)
    config = {'nodes': [{'id':'implementation'}, {'id':'test','gate':{'retry_node':'implementation'}}]}
    store.save_workflow(dict(store.get_workflow('wf'), config=config))
    store.save_task(dict(store.get_task('old'), status='working', candidate_sha=sha,
                        rework_delivery='delivered', rework_request_id='prior-request'))
    store.save_task(dict(task_id='test', workflow_id='wf', node='test', status='cleaned',
                        run_id='test-run', execution_id='execution', candidate_sha=sha,
                        stage_verdict='blocked', stage_verdict_affected_task_ids=['old']))
    op = recovery_store.list_operations(store.db_path, 'wf')[0]
    now = op['next_due_at'] + 1
    claimed = recovery_store.claim_operation(store.db_path, op['id'], 'owner', now)
    details = dict(action='verify', execution_id='execution', successor_ids=[], rework_ids=['old'],
                   target_runs={'old':'old-run'}, source_runs={'old':'old-run'},
                   rework_requests={'old':'new-request'}, repair_map={}, gate_nodes=['test'])
    recovery_store.record_step(store.db_path, op['id'], 'owner', 'rework_started', details, now)
    spec = importlib.util.spec_from_file_location('stale_rework_controller_test', Path(__file__).resolve().parents[1] / 'services/herdr-controller.py')
    ctl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ctl)
    monkeypatch.setattr(ctl, '_get_store', lambda: store)
    monkeypatch.setattr(ctl, 'workflow_config_for', lambda _: config)
    def forbid(*args, **kwargs):
        raise AssertionError('old request receipt must never invalidate new failed gates')
    monkeypatch.setattr(ctl, 'invalidate_for_fix_loop', forbid)
    status, result = ctl.execute_workflow_recovery(dict(claimed, detail=details), 'owner')
    assert status == 'waiting_human'
    assert result['reason'] == 'rework_delivery_unconfirmed'
    assert store.get_task('test')['stage_verdict'] == 'blocked'
    assert calls['worker'] == calls['prompt'] == 0
    reset_state_store()
