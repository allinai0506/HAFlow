"""Real staged output cannot disappear through physical finalize."""
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path
import pytest

@pytest.fixture
def cli(tmp_path,monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB",str(tmp_path/"state.db"))
    path=Path(__file__).resolve().parents[1]/'bin/herdr-task'
    spec=importlib.util.spec_from_loader('bug1002_teardown',importlib.machinery.SourceFileLoader('bug1002_teardown',str(path)))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize('integration_mode,status', [('none','completed'),('git','integrated'),('git','completed')])
def test_staged_output_retains_pane_clone_and_status(tmp_path,monkeypatch,cli,integration_mode,status):
    repo=tmp_path/'clone';repo.mkdir()
    def git(*args): return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True)
    git('init','-b','task');git('config','user.name','Test');git('config','user.email','test@example.com')
    (repo/'output').write_text('base');git('add','.');git('commit','-m','base')
    (repo/'output').write_text('valuable staged result');git('add','output')
    task={'task_id':'t','status':status,'pane_id':'owned','clone_path':str(repo),'integration_mode':integration_mode}
    if status=='integrated':task['integration_ref']='refs/herdr/tasks/t'
    monkeypatch.setattr(cli,'dump_transcript',lambda _:pytest.fail('must not tear down pending output'))
    monkeypatch.setattr(cli,'close_pane',lambda _:pytest.fail('must not close pending output'))
    row=cli._finalize_one(task)
    assert row['action']=='retained'
    assert row['status']==status
    assert not row['pane_closed'] and not row['clone_deleted']
    assert git('diff','--cached','--name-only').stdout.strip()=='output'


@pytest.mark.parametrize('command', ['finalize','close-workflow'])
def test_real_cli_refuses_dirty_output_and_preserves_authoritative_state(tmp_path,monkeypatch,cli,command):
    import os,sys
    from herdr.state_store import get_state_store
    repo=tmp_path/'clone';repo.mkdir()
    def git(*args):return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True)
    git('init','-b','task');git('config','user.name','Test');git('config','user.email','test@example.com')
    (repo/'output').write_text('base');git('add','.');git('commit','-m','base')
    (repo/'output').write_text('staged valuable result');git('add','output')
    store=get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf','status':'running','project_id':'p'})
    store.save_task({'task_id':'t','workflow_id':'wf','node':'docs','status':'completed','clone_path':str(repo),'integration_mode':'none'})
    env={**os.environ,'HERDR_STATE_DB':str(tmp_path/'state.db'),'TASKS_FILE':str(tmp_path/'tasks.json')}
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'bin/herdr-task'),command,'t' if command=='finalize' else 'wf'],env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode==2,result.stdout+result.stderr
    assert 'unpreserved_worktree_output' in result.stdout
    assert store.get_task('t')['status']=='completed'
    assert store.get_workflow('wf')['status']=='running'
    assert store.list_events(event_type='teardown_output_retained')
    assert repo.exists() and git('diff','--cached','--name-only').stdout.strip()=='output'


@pytest.mark.parametrize('kind',['staged','unstaged','untracked'])
def test_integration_metadata_does_not_authorize_dirty_clone_delete(tmp_path,cli,kind):
    repo=tmp_path/'clone';repo.mkdir()
    def git(*args):return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True)
    git('init','-b','task');git('config','user.name','Test');git('config','user.email','test@example.com')
    (repo/'output').write_text('base');git('add','.');git('commit','-m','base')
    (repo/('new' if kind=='untracked' else 'output')).write_text('valuable')
    if kind=='staged':git('add','output')
    assert cli.clone_deletable({'task_id':'t','clone_path':str(repo),'status':'integrated','integration_ref':'ref'})==(False,'unpreserved_worktree_output')


def test_git_completed_clean_clone_still_requires_controller_finalize(tmp_path,cli,monkeypatch):
    monkeypatch.setattr(cli,'dump_transcript',lambda _:pytest.fail('pipeline not settled'))
    row=cli._finalize_one({'task_id':'t','status':'completed','integration_mode':'git'})
    assert row['action']=='retained' and row['clone_retained_reason']=='git_finalize_pending'


def test_new_staged_output_during_transcript_blocks_teardown(tmp_path,cli,monkeypatch):
    repo=tmp_path/'clone';repo.mkdir()
    def git(*args):return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True)
    git('init','-b','task');git('config','user.name','Test');git('config','user.email','test@example.com')
    (repo/'output').write_text('base');git('add','.');git('commit','-m','base')
    def transcript(task):
        (repo/'output').write_text('new result after initial check');git('add','output')
        return 'transcript'
    monkeypatch.setattr(cli,'dump_transcript',transcript)
    monkeypatch.setattr(cli,'close_pane',lambda _:pytest.fail('new output must retain pane'))
    monkeypatch.setattr(cli,'delete_clone_safely',lambda _:pytest.fail('new output must retain clone'))
    row=cli._finalize_one({'task_id':'t','status':'integrated','integration_mode':'git','clone_path':str(repo),'pane_id':'p','integration_ref':'ref'})
    assert row['action']=='retained'
    assert row['status']=='integrated' and not row['clone_deleted'] and not row['pane_closed']


def test_late_output_retention_prevents_workflow_completed(tmp_path,cli,monkeypatch):
    from herdr.state_store import get_state_store
    repo=tmp_path/'clone';repo.mkdir()
    def git(*args):return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True)
    git('init','-b','task');git('config','user.name','Test');git('config','user.email','test@example.com')
    (repo/'output').write_text('base');git('add','.');git('commit','-m','base')
    store=get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf','status':'running','project_id':'p'})
    store.save_task({'task_id':'t','workflow_id':'wf','status':'integrated','node':'docs','clone_path':str(repo),'pane_id':'p','integration_mode':'git','integration_ref':'ref'})
    monkeypatch.setattr(cli,'load_tasks',lambda:{'tasks':store.list_tasks()})
    finalize=cli._finalize_one
    def interleaved(*a,**kw):
        (repo/'output').write_text('late result');git('add','output')
        return finalize(*a,**kw)
    monkeypatch.setattr(cli,'_finalize_one',interleaved)
    monkeypatch.setattr(cli,'close_pane',lambda _:pytest.fail('must retain pane'))
    with pytest.raises(SystemExit) as exc:
        cli.close_workflow('wf')
    assert exc.value.code==2
    assert store.get_workflow('wf')['status']!='completed'
    assert store.get_task('t')['status']=='integrated'
    assert repo.exists() and store.list_events(event_type='teardown_output_retained')

@pytest.mark.parametrize('command', ['finalize', 'close-workflow'])
@pytest.mark.parametrize('has_output', [False, True])
def test_context_workspace_real_cli_retains_artifacts(tmp_path, monkeypatch, cli, command, has_output):
    import os, sys
    from herdr.state_store import get_state_store
    spec = importlib.util.spec_from_file_location('context_worker', Path(__file__).resolve().parents[1]/'services/herdr-worker.py')
    worker = importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
    monkeypatch.setattr(worker, 'CLONE_ROOT', tmp_path/'clones')
    repo = worker.create_context_task_workspace('context-task')
    if has_output:
        (repo/'artifact.txt').write_text('context artifact must survive')
    store = get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf','status':'running','project_id':'p'})
    store.save_task({'task_id':'context-task','workflow_id':'wf','node':'docs','status':'completed',
                    'clone_path':str(repo),'execution_mode':'context','integration_mode':'none',
                    'integration_ref':'untrusted-ref','integration_sha':'a'*40})
    env = {**os.environ,'HERDR_STATE_DB':str(tmp_path/'state.db'),'TASKS_FILE':str(tmp_path/'tasks.json')}
    result = subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'bin/herdr-task'),command,
                             'context-task' if command=='finalize' else 'wf'],env=env,text=True,capture_output=True,timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
    assert repo.exists() and not (repo/'.git').exists()
    if has_output:
        assert (repo/'artifact.txt').read_text() == 'context artifact must survive'
    assert store.get_task('context-task')['status'] == 'cleaned'
    assert store.get_task('context-task')['clone_retained'] is True
    if command == 'close-workflow':
        assert store.get_workflow('wf')['status'] == 'completed'


def test_context_metadata_cannot_bypass_git_or_identity_guard(tmp_path,cli):
    repo = tmp_path/'repo'; repo.mkdir()
    task = {'task_id':'t','clone_path':str(repo),'execution_mode':'context','integration_mode':'none',
            'status':'completed','integration_ref':'fake'}
    (repo/'.herdr-launch-identity.json').write_text('{"task_id":"other"}')
    assert cli._teardown_output_guard(task)['reason'] == 'clone_identity_changed'
    (repo/'.herdr-launch-identity.json').unlink()
    subprocess.run(['git','-C',str(repo),'init'],check=True,capture_output=True)
    (repo/'output').write_text('valuable')
    assert cli._teardown_output_guard(task)['reason'] == 'unpreserved_worktree_output'
    assert cli.clone_deletable(task)[0] is False

@pytest.mark.parametrize('mode', [{}, {'execution_mode':'context','integration_mode':'git'},
                                  {'execution_mode':'git','integration_mode':'none'}])
def test_unknown_plain_workspace_still_refused(tmp_path,cli,mode):
    repo = tmp_path/'plain'; repo.mkdir()
    assert cli._teardown_output_guard({'task_id':'t','clone_path':str(repo),**mode})['reason'] == 'clone_git_inventory_unknown'


def test_context_clone_requires_explicit_purge_even_if_superseded(tmp_path,cli,monkeypatch):
    repo = tmp_path/'plain'; repo.mkdir(); (repo/'artifact').write_text('valuable')
    task = {'task_id':'t','clone_path':str(repo),'execution_mode':'context','integration_mode':'none',
            'status':'superseded','integration_ref':'fake','integration_sha':'a'*40}
    assert cli.clone_deletable(task)[0] is False
    monkeypatch.setattr(cli,'dump_transcript',lambda _:None)
    monkeypatch.setattr(cli,'close_pane',lambda _:False)
    calls=[]
    monkeypatch.setattr(cli,'delete_clone_safely',lambda path:calls.append(path) or True)
    assert not cli._finalize_one(task)['clone_deleted']
    assert not calls
    assert cli._finalize_one(task,purge_clones=True)['clone_deleted']
    assert calls == [str(repo)]


@pytest.mark.parametrize('output', ['internal_only', 'user_directory', 'tracked_internal'])
def test_large_internal_evidence_does_not_exhaust_inventory_before_filtering(tmp_path, cli, output):
    repo = tmp_path / 'large-evidence'; repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
    git('init', '-b', 'task'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
    infra = repo / '.herdr-loop'; infra.mkdir()
    tracked = infra / 'tracked.txt'; tracked.write_text('base')
    git('add', '.'); git('commit', '-m', 'base')
    evidence = infra / 'evidence'; evidence.mkdir()
    for index in range(900):
        (evidence / (str(index) + '-' + 'x' * 80 + '.txt')).write_text('test evidence')
    if output == 'user_directory':
        docs = repo / 'docs'; docs.mkdir(); (docs / 'valuable.md').write_text('user deliverable')
    if output == 'tracked_internal':
        tracked.write_text('tracked change must remain protected')
    result = cli._teardown_output_guard({'task_id': 't', 'clone_path': str(repo), 'integration_mode': 'none'})
    assert result['reason'] == ('' if output == 'internal_only' else 'unpreserved_worktree_output')
    if output == 'tracked_internal':
        assert '.herdr-loop/tracked.txt' in result['blocked_files']


@pytest.mark.parametrize('probe', ['root', 'status'])
def test_inventory_os_error_is_unknown_before_teardown(tmp_path, cli, monkeypatch, probe):
    from herdr import bounded_tools
    repo = tmp_path / 'clone'; repo.mkdir()
    calls = 0
    def denied(argv, **kwargs):
        nonlocal calls
        calls += 1
        if probe == 'root' or calls == 2:
            raise PermissionError('probe process termination not permitted')
        return {'status': 'completed', 'exit_code': 0, 'stdout': str(repo) + '\n', 'stderr': ''}
    monkeypatch.setattr(bounded_tools, 'run_bounded', denied)
    monkeypatch.setattr(cli, 'close_pane', lambda _: pytest.fail('unknown inventory cannot tear down'))
    result = cli._teardown_output_guard({'task_id': 't', 'clone_path': str(repo), 'integration_mode': 'none'})
    assert result == {'reason': 'clone_git_inventory_unknown'}
    assert repo.exists()
