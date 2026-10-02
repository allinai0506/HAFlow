import multiprocessing as mp
import pytest
from herdr import workflow, task_resources as resources
from herdr.state_store import get_state_store


def test_shared_artifact_contract_rejects_git_but_docs_git_remains_legal():
    invalid = {'nodes': [{'id': 'wrapup', 'artifact_mode': 'shared_artifacts', 'default_task_type': 'docs', 'default_integration_mode': 'git'}]}
    with pytest.raises(ValueError, match='shared_artifacts'):
        workflow.normalize_workflow(invalid)
    invalid['nodes'][0]['artifact_mode'] = 'repository_changes'
    assert workflow.normalize_workflow(invalid)['nodes'][0]['artifact_mode'] == 'repository_changes'


def test_shared_mode_survives_normalization():
    result = workflow.normalize_workflow({'nodes': [{'id': 'report', 'artifact_mode': 'shared_artifacts', 'default_integration_mode': 'none'}]})
    assert result['nodes'][0].get('artifact_mode') == 'shared_artifacts'


def api():
    assert callable(getattr(resources, 'begin_launch_intent', None)), 'durable launch intent API missing'
    return resources.begin_launch_intent


def claim(store, task_id='t', role='worker', round=1, supersedes=None, now=100):
    return api()(store, workflow_id='wf', node_id='n', role=role, candidate_sha='abc', dispatch_round=round, task_id=task_id, supersedes=supersedes, now=now)


def test_expired_intent_requires_reconciliation_without_new_launch(tmp_path):
    store = get_state_store(tmp_path / 'state.db')
    with resources.workflow_launch_lock(store.db_path, 'wf'):
        first = claim(store)
        assert first['status'] == 'claimed'
        second = claim(store, task_id='other', now=10000)
        assert second['status'] == 'recovery_required'
        assert second['intent']['task_id'] == 't'
        assert len(store.list_events(event_type='launch_intent')) == 1


def test_roles_and_explicit_rework_are_distinct(tmp_path):
    store = get_state_store(tmp_path / 'state.db')
    with resources.workflow_launch_lock(store.db_path, 'wf'):
        assert claim(store)['status'] == 'claimed'
        assert claim(store, task_id='review', role='review')['status'] == 'claimed'
        with pytest.raises(ValueError, match='supersedes'):
            claim(store, task_id='t-r2', round=2)
        store.save_task({'task_id': 't', 'workflow_id': 'wf', 'node': 'n', 'dispatch_role': 'worker', 'candidate_sha': 'abc', 'dispatch_round': 1, 'status': 'superseded'})
        assert claim(store, task_id='t-r2', round=2, supersedes='t')['status'] == 'claimed'


def _racer(db, barrier, queue, task_id):
    store = get_state_store(db)
    barrier.wait(timeout=10)
    with resources.workflow_launch_lock(db, 'wf'):
        result = claim(store, task_id=task_id)
        if result['status'] == 'claimed':
            store.save_task({'task_id': task_id, 'workflow_id': 'wf', 'node': 'n', 'dispatch_role': 'worker', 'candidate_sha': 'abc', 'dispatch_round': 1, 'status': 'pending'})
            resources.finish_launch_intent(store, result['intent'], now=101)
        queue.put(result['status'])


def test_cross_process_auto_manual_one_task(tmp_path):
    api()
    db = tmp_path / 'state.db'
    store = get_state_store(db)
    ctx = mp.get_context('spawn')
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_racer, args=(db, barrier, queue, name)) for name in ('auto', 'manual')]
    for process in processes: process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    assert sorted([queue.get(timeout=2), queue.get(timeout=2)]) == ['claimed', 'duplicate']
    assert len(store.list_tasks()) == 1


@pytest.mark.parametrize('verdict', ['owned', 'foreign', 'unknown'])
def test_recovery_never_reclaims_unproven_resources(tmp_path, verdict):
    store = get_state_store(tmp_path / 'state.db')
    with resources.workflow_launch_lock(store.db_path, 'wf'):
        intent = claim(store)['intent']
        intent = resources.record_launch_resources(store, intent, {'pane_id': 'p', 'run_id': 'run'}, now=101)
        result = resources.reconcile_launch_intent(store, intent, lambda actual: verdict, now=1000)
        assert result['status'] == 'recovery_required'
        assert claim(store, task_id='other', now=1001)['status'] == 'recovery_required'
        assert len(store.list_tasks()) == 0


def test_absent_resources_release_intent_and_old_owner_cannot_update(tmp_path):
    store = get_state_store(tmp_path / 'state.db')
    with resources.workflow_launch_lock(store.db_path, 'wf'):
        first = claim(store)['intent']
        assert resources.reconcile_launch_intent(store, first, lambda actual: 'absent', now=200)['status'] == 'resources_absent'
        second = claim(store, task_id='retry', now=201)
        assert second['status'] == 'claimed'
        assert second['intent']['intent_id'] != first['intent_id']
        with pytest.raises(ValueError, match='no longer owns'):
            resources.record_launch_resources(store, first, {'pane_id': 'foreign'}, now=202)


def test_dispatch_rework_preserves_role_and_reports_supersedes():
    from herdr.direct_dispatch import plan_stage_dispatch
    task = {'task_id': 'wf-n-review', 'workflow_id': 'wf', 'node': 'n', 'status': 'superseded', 'dispatch_role': 'review', 'dispatch_round': 3}
    result = plan_stage_dispatch('wf', {'id': 'n', 'purpose': 'Review', 'agent_policy': {'max_concurrency': 1}}, [task], 'fix')
    assert result['specs'][0]['dispatch_role'] == 'review'
    assert result['specs'][0]['dispatch_round'] == 4
    assert result['specs'][0]['supersedes'] == task['task_id']


def test_direct_invalid_mode_rejected_even_when_node_has_active_task():
    from herdr.direct_dispatch import plan_stage_dispatch
    node = {'id': 'n', 'purpose': 'report', 'artifact_mode': 'shared_artifacts', 'default_integration_mode': 'git'}
    with pytest.raises(ValueError, match='shared_artifacts'):
        plan_stage_dispatch('wf', node, [{'task_id': 'old', 'workflow_id': 'wf', 'node': 'n', 'status': 'working'}], 'report')


def _crash_after_allocation(db, resource_file):
    import os
    from pathlib import Path
    store = get_state_store(db)
    with resources.workflow_launch_lock(db, 'wf'):
        intent = claim(store)['intent']
        # Simulate the physical transport's resource tagging before its receipt.
        Path(resource_file).write_text(intent['intent_id'])
        os._exit(0)


def test_crash_between_allocation_and_resource_record_is_not_relaunched(tmp_path):
    db = tmp_path / 'state.db'
    store = get_state_store(db)
    ctx = mp.get_context('spawn')
    resource_file = tmp_path / 'pane-identity'
    process = ctx.Process(target=_crash_after_allocation, args=(db, resource_file))
    process.start()
    process.join(15)
    assert process.exitcode == 0
    with resources.workflow_launch_lock(db, 'wf'):
        retry = claim(store, task_id='retry', now=10000)
        assert retry['status'] == 'recovery_required'
        assert retry['intent']['resources'] == {}
        assert resource_file.read_text() == retry['intent']['intent_id']
        recovered = resources.reconcile_launch_intent(store, retry['intent'], lambda intent: 'owned', now=10001)
        assert recovered['status'] == 'recovery_required'
        assert len(store.list_events(event_type='launch_intent')) == 1
        assert store.list_tasks() == []


def test_real_cli_duplicate_returns_authoritative_task_without_router(tmp_path):
    import os, subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    db = tmp_path / 'state.db'
    store = get_state_store(db)
    store.save_workflow({'workflow_id': 'wf', 'project_id': 'p', 'status': 'running', 'project_root': str(tmp_path), 'config': {'nodes': [{'id': 'n', 'max_tasks_per_node': 1, 'artifact_mode': 'repository_changes', 'default_integration_mode': 'none'}]}})
    store.save_task({'task_id': 'authoritative', 'workflow_id': 'wf', 'node': 'n', 'dispatch_role': 'worker', 'dispatch_round': 1, 'candidate_sha': 'abc', 'status': 'pending'})
    env = {**os.environ, 'HOME': str(tmp_path), 'HERDR_STATE_DB': str(db), 'TASKS_FILE': str(tmp_path / 'tasks.json'), 'WORKFLOWS_FILE': str(tmp_path / 'workflows.json')}
    result = subprocess.run([sys.executable, str(root / 'bin/herdr-task'), 'launch', '--task-id', 'manual', '--workflow-id', 'wf', '--node', 'n', '--source', str(tmp_path), '--prompt', 'same task', '--goal', 'same task', '--candidate-sha', 'abc', '--dispatch-role', 'worker', '--dispatch-round', '1'], env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'authoritative' in result.stdout
    assert len(store.list_tasks()) == 1
    assert store.list_events(event_type='launch_intent') == []


def test_cli_artifact_override_cannot_bypass_node_contract(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from tests.test_fix_loop_pr1 import _load_module
    store = get_state_store(tmp_path / 'state.db')
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    project = {'workflow_id': 'wf', 'project_id': 'p', 'status': 'running', 'project_root': str(tmp_path), 'config': {'nodes': [{'id': 'n', 'artifact_mode': 'shared_artifacts', 'default_integration_mode': 'none'}]}}
    store.save_workflow(project)
    cli = _load_module('dispatch_config_cli', Path(__file__).resolve().parents[1] / 'bin/herdr-task')
    monkeypatch.setattr(cli, 'choose_agent', lambda *a, **k: pytest.fail('invalid config reached Router'))
    args = SimpleNamespace(task_id='manual', workflow_id='wf', node='n', source=None, agent='auto', task_type='docs', integration_mode='git', goal='report', artifact_mode='repository_changes', acceptance=[], prompt='report', supersedes=None)
    with pytest.raises(SystemExit) as exc:
        cli.launch_task(args)
    assert exc.value.code == 2
    assert store.list_tasks() == []
    assert store.list_events(event_type='launch_intent') == []


def test_launch_entry_registers_identity_and_receipt_before_duplicate(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from tests.test_fix_loop_pr1 import _load_module
    store = get_state_store(tmp_path / 'state.db')
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    store.save_workflow({'workflow_id': 'wf', 'project_id': 'p', 'status': 'running', 'project_root': str(tmp_path), 'config': {'nodes': [{'id': 'n', 'artifact_mode': 'shared_artifacts', 'default_integration_mode': 'none'}]}})
    cli = _load_module('dispatch_registration_cli', Path(__file__).resolve().parents[1] / 'bin/herdr-task')
    calls = []
    monkeypatch.setattr(cli, 'choose_agent', lambda *a, **k: calls.append('route') or 'codex')
    monkeypatch.setattr(cli, '_preflight_delivery_identity', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'ensure_stage_topology', lambda *a: {'workspace_id': 'w', 'anchor_pane_id': 'anchor', 'tab_id': 'tab'})
    monkeypatch.setattr(cli, 'acquire_pane_for_task', lambda *a: None)
    result = {'clone': str(tmp_path), 'branch': 'agent/task', 'pane_id': 'owned', 'agent_session_id': 'session'}
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: calls.append('worker') or SimpleNamespace(returncode=0, stdout='HERDR_WORKER_RESULT=' + json.dumps(result), stderr=''))
    monkeypatch.setattr(cli, 'auto_init_task_loop', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'release_agent_reservation', lambda *a: None)
    monkeypatch.setattr(cli, 'dispatch_task', lambda *a: calls.append('dispatch'))
    args = SimpleNamespace(task_id='auto', workflow_id='wf', node='n', source=None, agent='auto', task_type='docs', integration_mode='none', goal='report', acceptance=[], prompt='report', supersedes=None)
    cli.launch_task(args)
    task = store.get_task('auto')
    assert task['dispatch_role'] == 'worker' and task['dispatch_round'] == 1
    assert task['artifact_mode'] == 'shared_artifacts'
    latest = store.list_events(event_type='launch_intent', limit=1, desc=True)[0]['payload']
    assert latest['phase'] == 'registered' and latest['resources']['pane_id'] == 'owned'
    cli.launch_task(args)
    args.task_id = 'manual'
    cli.launch_task(args)
    assert calls == ['route', 'worker', 'dispatch']
    assert len(store.list_tasks()) == 1


@pytest.fixture
def launch_transport_scene(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from tests.test_fix_loop_pr1 import _load_module
    store = get_state_store(tmp_path / 'state.db')
    monkeypatch.setenv('HERDR_STATE_DB', str(store.db_path))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE', str(tmp_path / 'workflows.json'))
    store.save_workflow({'workflow_id': 'wf', 'project_id': 'p', 'status': 'running', 'project_root': str(tmp_path), 'config': {'nodes': [{'id': 'n', 'default_integration_mode': 'none'}]}})
    cli = _load_module('launch_failure_cli', Path(__file__).resolve().parents[1] / 'bin/herdr-task')
    monkeypatch.setattr(cli, 'choose_agent', lambda *a, **k: 'codex')
    monkeypatch.setattr(cli, '_preflight_delivery_identity', lambda *a, **k: None)
    monkeypatch.setattr(cli, 'ensure_stage_topology', lambda *a: {'workspace_id': 'w', 'anchor_pane_id': 'anchor', 'tab_id': 'tab'})
    monkeypatch.setattr(cli, 'acquire_pane_for_task', lambda *a: None)
    args = SimpleNamespace(task_id='failed', workflow_id='wf', node='n', run_id='run-launch', source=None, agent='auto', task_type='docs', integration_mode='none', goal='report', acceptance=[], prompt='report', supersedes=None)
    return cli, store, args


def test_failed_worker_known_resources_preserved_in_intent(launch_transport_scene, monkeypatch):
    import json
    from types import SimpleNamespace
    cli, store, args = launch_transport_scene
    failure = {'task_id': args.task_id, 'run_id': args.run_id, 'pane_id': 'owned', 'clone': '/temporary/owned', 'agent_session_id': 'session', 'agent_name': 'worker', 'agent_start_attempted': True, 'disposition': 'unknown'}
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='HERDR_WORKER_FAILURE=' + json.dumps(failure)))
    with pytest.raises(SystemExit) as exc: cli.launch_task(args)
    assert exc.value.code == 1
    intent = store.list_events(event_type='launch_intent', desc=True, limit=1)[0]['payload']
    assert intent['resources']['pane_id'] == 'owned'
    assert intent['resources']['clone_path'] == '/temporary/owned'
    assert intent['resources']['ownership'] == 'unknown'
    assert store.list_events(event_type='launch_recovery_required')[0]['node_id'] == 'n'
    assert store.list_tasks() == []


def test_worker_timeout_keeps_unknown_intent_and_persists_recovery(launch_transport_scene, monkeypatch):
    import subprocess
    cli, store, args = launch_transport_scene
    def timeout(argv, **kwargs):
        assert kwargs['timeout'] == 300
        assert argv[argv.index('--run-id') + 1] == args.run_id
        assert argv[argv.index('--launch-intent-id') + 1]
        raise subprocess.TimeoutExpired(argv, 300)
    monkeypatch.setattr(cli.subprocess, 'run', timeout)
    with pytest.raises(SystemExit) as exc: cli.launch_task(args)
    assert exc.value.code == 75
    assert store.list_events(event_type='launch_recovery_required')[0]['payload']['side_effects'] == 'unknown'
    intent = store.list_events(event_type='launch_intent', desc=True, limit=1)[0]['payload']
    assert intent['phase'] == 'allocating'
    assert store.list_tasks() == []


def test_real_cli_add_duplicate_never_overwrites_live_task(tmp_path):
    import os, subprocess, sys
    from pathlib import Path
    store = get_state_store(tmp_path / 'state.db')
    original = {'task_id': 'existing', 'workflow_id': 'wf', 'node': 'n', 'run_id': 'original-run', 'status': 'working', 'pane_id': 'owned-pane', 'goal': 'original-goal'}
    store.save_task(original)
    before = store.get_task('existing')
    events_before = store.list_events()
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'HOME': str(tmp_path), 'HERDR_STATE_DB': str(store.db_path), 'TASKS_FILE': str(tmp_path / 'tasks.json'), 'WORKFLOWS_FILE': str(tmp_path / 'workflows.json')}
    result = subprocess.run([sys.executable, str(root / 'bin/herdr-task'), 'add', '--task-id', 'existing', '--workflow-id', 'wf', '--node', 'n', '--workspace', 'foreign', '--pane', 'foreign', '--agent', 'codex', '--clone', str(tmp_path), '--goal', 'overwritten'], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode != 0, result.stdout + result.stderr
    assert store.get_task('existing') == before
    assert store.list_events() == events_before


def test_new_round_cannot_overwrite_previous_task_id(launch_transport_scene, monkeypatch):
    cli, store, args = launch_transport_scene
    store.save_task({'task_id': args.task_id, 'workflow_id': 'wf', 'node': 'n', 'run_id': 'original',
                     'dispatch_role': 'worker', 'dispatch_round': 1, 'status': 'working'})
    before = store.get_task(args.task_id)
    args.supersedes = args.task_id
    args.supersede_reason = 'explicit rework'
    args.dispatch_round = 2
    monkeypatch.setattr(cli, 'choose_agent', lambda *a, **k: pytest.fail('task-id collision reached router'))
    with pytest.raises(SystemExit) as exc:
        cli.launch_task(args)
    assert exc.value.code == 2
    assert store.get_task(args.task_id) == before
    assert store.list_events(event_type='launch_intent') == []


def test_ambiguous_legacy_wrapup_requires_explicit_delivery_mode():
    node={'id':'wrapup','default_task_type':'docs','default_integration_mode':'git'}
    with pytest.raises(ValueError,match='artifact_mode.*explicit'):
        workflow.normalize_workflow({'nodes':[node]})
    node['artifact_mode']='repository_changes'
    assert workflow.normalize_workflow({'nodes':[node]})['nodes'][0]['artifact_mode']=='repository_changes'
    assert workflow.normalize_workflow({'nodes':[{'id':'documentation','default_task_type':'docs','default_integration_mode':'git'}]})['nodes'][0]['id']=='documentation'


def test_legacy_stage_wrapup_requires_explicit_delivery_mode():
    with pytest.raises(ValueError,match='artifact_mode.*explicit'):
        workflow.normalize_workflow({'stages':[{'id':'wrapup'}],'stage_policies':{'wrapup':{'default_task_type':'docs','default_integration_mode':'git'}}})


def test_real_cli_ambiguous_wrapup_fails_before_launch(tmp_path):
    import os,sys,subprocess
    from pathlib import Path
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    store=get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf','status':'running','project_root':str(tmp_path),'config':{'nodes':[{'id':'wrapup','default_task_type':'docs','default_integration_mode':'git'}]}})
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'bin/herdr-task'),'launch','--task-id','wrap','--workflow-id','wf','--node','wrapup','--source',str(tmp_path),'--task-type','docs','--integration-mode','git','--prompt','report','--goal','report'],env={**os.environ,'HOME':str(tmp_path),'HERDR_STATE_DB':str(store.db_path),'TASKS_FILE':str(tmp_path/'tasks.json'),'WORKFLOWS_FILE':str(tmp_path/'workflows.json')},text=True,capture_output=True,timeout=15)
    assert result.returncode==2,result.stdout+result.stderr
    assert 'artifact_mode' in result.stdout+result.stderr
    assert store.list_tasks()==[] and store.list_events(event_type='launch_intent')==[]


def test_auto_dispatch_ambiguous_wrapup_rejects_before_plan():
    from herdr.direct_dispatch import plan_stage_dispatch
    node={'id':'wrapup','default_task_type':'docs','default_integration_mode':'git'}
    with pytest.raises(ValueError,match='artifact_mode.*explicit'):
        plan_stage_dispatch('wf',node,[],'report')
