"""Cumulative launch overflow is explicit, audited, and visible across history."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_fix_loop_pr1 import _load_module
from herdr.state_store import get_state_store
from herdr.workflow_graph import workflow_graph_projection

ROOT = Path(__file__).resolve().parents[1]


class Routed(Exception):
    pass


@pytest.fixture
def scene(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    monkeypatch.setenv('TASKS_FILE', str(tmp_path / 'tasks.json'))
    store = get_state_store(db)
    config = {'workflow_id': 'wf', 'nodes': [
        {'id': 'plan', 'agent_policy': {'max_agents': 2}}]}
    config_file = tmp_path / 'workflow.json'
    config_file.write_text(json.dumps(config))
    project = {'workflow_id': 'wf', 'project_id': 'p', 'status': 'running',
               'workflow_file': str(config_file), 'project_root': str(tmp_path)}
    store.save_workflow(project)
    for index, status in enumerate(['superseded', 'failed']):
        store.save_task({'task_id': f'old-{index}', 'workflow_id': 'wf',
                         'node': 'plan', 'status': status,
                         'runtime': {'pane_id': f'pane-{index}'}})
    store.save_task({'task_id': 'unrelated', 'workflow_id': 'other',
                     'node': 'plan', 'status': 'working', 'pane_id': 'other-pane'})
    cli = _load_module('capacity_cli', ROOT / 'bin/herdr-task')
    monkeypatch.setattr(cli, 'project_for_workflow', lambda wid: project if wid == 'wf' else None)
    def route(*args, **kwargs):
        raise Routed()
    monkeypatch.setattr(cli, 'choose_agent', route)
    args = SimpleNamespace(task_id='new', workflow_id='wf', node='plan',
        source=str(tmp_path), task_type='fix', agent='auto', integration_mode='none',
        ack_overflow=False, supersedes=None, goal='fix', acceptance=[], prompt='fix')
    return cli, store, config, config_file, args


def test_overflow_refused_before_routing_or_supersede(scene, capsys):
    cli, store, _, _, args = scene
    args.supersedes = 'old-1'
    args.supersede_reason = 'fixture replacement'
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert '--ack-overflow' in out and 'old-0' in out and 'old-1' in out
    assert 'unrelated' not in out
    assert store.get_task('old-1')['status'] == 'failed'
    assert store.get_task('new') is None
    assert store.list_events(event_type='node_overflow_acknowledged') == []


def test_ack_is_durable_before_external_routing(scene):
    cli, store, _, _, args = scene
    args.ack_overflow = True
    with pytest.raises(Routed):
        cli._launch_task(args)
    events = store.list_events(event_type='node_overflow_acknowledged')
    assert len(events) == 1
    assert events[0]['task_id'] == 'new'
    assert events[0]['node_id'] == 'plan'
    assert events[0]['payload']['task_ids'] == ['old-0', 'old-1']
    assert events[0]['payload']['proposed_task_count'] == 3


def test_failed_audit_refuses_launch(scene, monkeypatch):
    cli, store, _, _, args = scene
    args.ack_overflow = True
    monkeypatch.setattr(cli, '_get_store', lambda: store)
    def fail(*a, **kw):
        raise OSError('disk unavailable')
    monkeypatch.setattr(store, 'record_event', fail)
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2
    assert store.get_task('new') is None


@pytest.mark.parametrize('policy', [{}, {'max_agents': 3}])
def test_unlimited_or_at_limit_keeps_existing_launch(scene, policy):
    cli, store, config, file, args = scene
    config['nodes'][0]['agent_policy'] = policy
    file.write_text(json.dumps(config))
    with pytest.raises(Routed):
        cli._launch_task(args)
    assert store.list_events(event_type='node_overflow_acknowledged') == []


@pytest.mark.parametrize('limit', [0, -1, True, '2'])
def test_invalid_threshold_refused_before_routing(scene, limit):
    cli, _, config, file, args = scene
    config['nodes'][0]['agent_policy']['max_agents'] = limit
    file.write_text(json.dumps(config))
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2


def test_panes_cli_reads_archived_tasks_and_deduplicates_pane_refs(scene):
    cli, store, _, _, args = scene
    store.save_task({'task_id': 'shared', 'workflow_id': 'wf', 'stage': 'plan',
                     'status': 'working', 'pane_id': 'pane-1'})
    result = subprocess.run([sys.executable, str(ROOT / 'bin/herdr-task'),
        'panes', '--workflow-id', 'wf', '--json'], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    row = json.loads(result.stdout)['nodes'][0]
    assert row['task_count'] == 3 and row['pane_count'] == 2
    assert row['overflow'] is True and row['confirmation_threshold'] == 2
    assert row['pane_count_source'] == 'persisted_references'
    assert 'unrelated' not in row['task_ids']


def test_graph_exposes_history_without_changing_completion_counts(scene):
    _, store, config, _, _ = scene
    row = workflow_graph_projection(config, store.list_tasks(workflow_id='wf'))['nodes'][0]
    assert row['resource_usage']['task_count'] == 2
    assert row['resource_usage']['pane_count'] == 2
    assert row['task_count'] == 1  # superseded excluded from existing completion view


def test_runtime_released_pane_does_not_resurrect_legacy_reference(scene):
    _, store, config, _, _ = scene
    store.save_task({'task_id': 'old-0', 'workflow_id': 'wf', 'node': 'plan',
                    'status': 'superseded', 'pane_id': 'stale-pane',
                    'runtime': {'pane_id': None}})
    row = workflow_graph_projection(config, store.list_tasks(workflow_id='wf'))['nodes'][0]
    assert row['resource_usage']['pane_count'] == 1


@pytest.mark.parametrize('ack', [False, True])
def test_hard_task_budget_cannot_be_acknowledged_away(scene, ack, capsys):
    cli, store, config, file, args = scene
    config['nodes'][0]['max_tasks_per_node'] = 2
    config['nodes'][0]['agent_policy'] = {'max_concurrency': 2}
    file.write_text(json.dumps(config))
    for i in range(2):
        store.save_task({'task_id': f'active-{i}', 'workflow_id': 'wf', 'node': 'plan', 'status': 'working'})
    store.save_task({'task_id': 'old-2', 'workflow_id': 'wf', 'node': 'plan', 'status': 'superseded'})
    args.ack_overflow = ack
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2
    assert 'max_tasks_per_node' in capsys.readouterr().out


def test_concurrency_counts_active_tasks_only_and_refuses_third(scene):
    cli, store, config, file, args = scene
    config['nodes'][0]['agent_policy'] = {'max_concurrency': 2}
    file.write_text(json.dumps(config))
    for i in range(2):
        store.save_task({'task_id': f'active-{i}', 'workflow_id': 'wf',
                         'node': 'plan', 'status': 'working'})
    args.ack_overflow = True
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2


def test_replacement_requires_explicit_reason_before_any_mutation(scene):
    cli, store, _, _, args = scene
    args.supersedes = 'old-1'
    args.ack_overflow = True
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2
    assert store.get_task('old-1')['status'] == 'failed'


def test_dispatch_uses_renamed_field_with_legacy_compatibility():
    from herdr.direct_dispatch import classify_dispatch
    assert classify_dispatch({'agent_policy': {'max_concurrency': 1}}) == 'static_single'
    assert classify_dispatch({'agent_policy': {'max_concurrency': 2}}) == 'dynamic'
    assert classify_dispatch({'agent_policy': {'max_agents': 2}}) == 'dynamic'
    assert classify_dispatch({'agent_policy': {'max_agents': 2, 'max_concurrency': 1}}) == 'static_single'


def test_independent_launchers_with_distinct_sources_serialize_budget(scene, tmp_path):
    """Pause process A after precheck; process B cannot precheck before A registers."""
    import os
    import time
    cli, store, config, file, args = scene
    config['nodes'][0]['max_tasks_per_node'] = 3
    config['nodes'][0]['agent_policy'] = {'max_concurrency': 3}
    file.write_text(json.dumps(config))
    store.save_task({'task_id': 'active-1', 'workflow_id': 'wf', 'node': 'plan', 'status': 'working'})
    script = tmp_path / 'launcher.py'
    script.write_text('''
import importlib.machinery, importlib.util, sys, time
from pathlib import Path
from types import SimpleNamespace
root, scene, tid = sys.argv[1:]
spec=importlib.util.spec_from_loader('cli', importlib.machinery.SourceFileLoader('cli', str(Path(root)/'bin/herdr-task')))
cli=importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
from herdr.state_store import get_state_store
store=get_state_store(Path(scene)/'state.db')
# External Git serialization is irrelevant: callers deliberately use distinct sources.
from contextlib import nullcontext
cli.GitOperationLock=lambda source: nullcontext()
cli.ensure_no_git_processes=lambda source: None
args=SimpleNamespace(task_id=tid,workflow_id='wf',node='plan',source=str(Path(scene)/tid),run_id='run-'+tid)
def launch(args):
    tasks=store.list_tasks()
    cli._check_launch_capacity(args,store.get_workflow('wf'),tasks,'plan')
    (Path(scene)/(tid+'.checked')).touch()
    if tid=='first':
        deadline=time.monotonic()+5
        while not (Path(scene)/'release').exists():
            if time.monotonic()>deadline: raise RuntimeError('test release missing')
            time.sleep(.02)
    store.save_task({'task_id':tid,'workflow_id':'wf','node':'plan','status':'pending'})
cli._launch_task=launch
(Path(scene)/(tid+'.ready')).touch()
cli.launch_task(args)
''')
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    a = subprocess.Popen([sys.executable, str(script), str(ROOT), str(tmp_path), 'first'], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    b = None
    def await_file(name):
        deadline = time.monotonic() + 5
        while not (tmp_path / name).exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
    try:
        await_file('first.checked')
        b = subprocess.Popen([sys.executable, str(script), str(ROOT), str(tmp_path), 'second'], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        await_file('second.ready')
        time.sleep(.15)
        assert not (tmp_path / 'second.checked').exists()
        (tmp_path / 'release').touch()
        a.communicate(timeout=8)
        bout, berr = b.communicate(timeout=8)
        assert a.returncode == 0
        assert b.returncode == 2, (bout, berr)
        assert 'max_tasks_per_node=3' in bout
        assert store.get_task('first') is not None and store.get_task('second') is None
    finally:
        (tmp_path / 'release').touch()
        for child in [a, b]:
            if child and child.poll() is None:
                child.terminate(); child.communicate(timeout=5)


def test_active_replacement_requires_real_free_slot(scene):
    cli, store, config, file, args = scene
    config['nodes'][0]['agent_policy'] = {'max_concurrency': 1}
    file.write_text(json.dumps(config))
    store.save_task({'task_id': 'active', 'workflow_id': 'wf', 'node': 'plan',
                     'status': 'working', 'run_id': 'old-run'})
    args.supersedes = 'active'
    args.supersede_reason = 'needs a new execution'
    with pytest.raises(SystemExit) as exc:
        cli._launch_task(args)
    assert exc.value.code == 2
    assert store.get_task('active')['status'] == 'working'


def test_failed_route_does_not_retire_replacement_source(scene):
    cli, store, _, _, args = scene
    args.supersedes = 'old-1'
    args.supersede_reason = 'explicit replacement'
    args.ack_overflow = True
    with pytest.raises(Routed):
        cli._launch_task(args)
    assert store.get_task('old-1')['status'] == 'failed'


def test_legacy_overflow_ack_preserves_dispatch_semantics(scene):
    cli, store, _, _, args = scene
    for i in range(3):
        store.save_task({'task_id': f'active-{i}', 'workflow_id': 'wf', 'node': 'plan',
                         'status': 'working'})
    args.ack_overflow = True
    with pytest.raises(Routed):
        cli._launch_task(args)
    assert len(store.list_events(event_type='node_overflow_acknowledged')) == 1


def test_budget_diagnostics_name_cumulative_superseded_and_active_counts(scene):
    from herdr.node_capacity import node_usage, launch_capacity_error
    _,store,config,_,_=scene
    node=config['nodes'][0];node['max_tasks_per_node']=2
    usage=node_usage(node,store.list_tasks(),'wf')
    assert usage['registered_task_count']==usage['task_count']==2
    assert usage['superseded_task_count']==1
    assert usage['budget_task_count']==1
    assert usage['retired_task_count']==1
    assert usage['task_count_source']=='all_registered_including_superseded'
    assert usage['active_task_count_source']=='concurrent_statuses'
    assert usage['active_task_count']==0
    assert usage['max_tasks_per_node']==2
    node['max_tasks_per_node']=1
    usage=node_usage(node,store.list_tasks(),'wf')
    error=launch_capacity_error(usage)
    assert error is not None and 'budget=1' in error and 'including 1 superseded' in error


def test_retired_superseded_by_row_does_not_consume_budget(scene):
    from herdr.node_capacity import node_usage, launch_capacity_error
    _,store,config,_,_=scene
    node=config['nodes'][0];node['max_tasks_per_node']=2
    node['agent_policy']={'max_concurrency': 2}
    store.save_task({'task_id': 'retired', 'workflow_id': 'wf', 'node': 'plan',
                     'status': 'committed', 'superseded_by': 'successor'})
    usage=node_usage(node,store.list_tasks(),'wf')
    assert usage['retired_task_count']==2
    assert usage['budget_task_count']==1
    assert launch_capacity_error(usage) is None
    store.save_task({'task_id': 'live-committed', 'workflow_id': 'wf', 'node': 'plan',
                     'status': 'committed'})
    usage=node_usage(node,store.list_tasks(),'wf')
    assert usage['budget_task_count']==2
    assert launch_capacity_error(usage) is not None


@pytest.mark.parametrize('limit', [2])
def test_superseded_tasks_do_not_consume_hard_budget(scene, limit):
    from herdr.node_capacity import node_usage, launch_capacity_error
    cli, store, config, file, args = scene
    config['nodes'][0]['max_tasks_per_node'] = limit
    config['nodes'][0]['agent_policy'] = {'max_concurrency': 2}
    file.write_text(json.dumps(config))
    with pytest.raises(Routed):
        cli._launch_task(args)
    usage = node_usage(config['nodes'][0], store.list_tasks(), 'wf')
    assert usage['registered_task_count'] == 2
    assert usage['budget_task_count'] == 1
    assert usage['superseded_task_count'] == 1
    assert launch_capacity_error(usage) is None
