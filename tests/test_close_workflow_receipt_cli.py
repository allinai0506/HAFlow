"""Real close CLI with isolated SQLite/HOME and process coordination."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest

from herdr.state_store import SQLiteStateStore

ROOT=Path(__file__).resolve().parents[1]


def env(tmp_path):
    return dict(os.environ,HOME=str(tmp_path),HERDR_STATE_DB=str(tmp_path/'state.db'),TASKS_FILE=str(tmp_path/'tasks.json'),WORKFLOWS_FILE=str(tmp_path/'workflows.json'),STAGE_STATE_FILE=str(tmp_path/'stage-state.json'))


def cli(tmp_path,*args):
    return subprocess.run([sys.executable,str(ROOT/'bin/herdr-task'),'close-workflow','wf',*args],env=env(tmp_path),text=True,capture_output=True,timeout=20)


def seed(tmp_path):
    store=SQLiteStateStore(tmp_path/'state.db'); store.save_workflow({'workflow_id':'wf','status':'running','project_id':'p'});return store


def test_real_cli_completed_receipt_retry(tmp_path):
    seed(tmp_path)
    first=cli(tmp_path); second=cli(tmp_path)
    assert first.returncode==second.returncode==0,(first.stderr,second.stderr)
    with __import__('sqlite3').connect(tmp_path/'state.db') as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_close_operations'").fetchone(), 'close CLI did not persist operation receipt'
        rows=c.execute('SELECT receipt FROM workflow_close_operations WHERE workflow_id=?',('wf',)).fetchall()
    assert len(rows)==1 and json.loads(rows[0][0])['close_state']=='completed'


def hold(db,ready,release):
    from herdr.workflow_close import workflow_close_claim
    with workflow_close_claim(SimpleNamespace(db_path=db),'wf'):
        ready.set(); assert release.wait(15)


def test_real_cli_competes_with_process_owner(tmp_path):
    seed(tmp_path); ctx=mp.get_context('spawn'); ready=ctx.Event(); release=ctx.Event()
    p=ctx.Process(target=hold,args=(str(tmp_path/'state.db'),ready,release)); p.start()
    try:
        assert ready.wait(10); result=cli(tmp_path)
        assert result.returncode==0,result.stderr
        assert 'in_progress' in result.stdout
        assert SQLiteStateStore(tmp_path/'state.db').get_workflow('wf')['status']=='running'
    finally:release.set();p.join(15)
    assert p.exitcode==0


@pytest.mark.parametrize("historical", [False, True])
def test_close_runtime_session_identity_required(tmp_path,monkeypatch,historical):
    import importlib.machinery
    import importlib.util
    loader=importlib.machinery.SourceFileLoader('close_receipt_real',str(ROOT/'bin/herdr-task'))
    spec=importlib.util.spec_from_loader(loader.name,loader); m=importlib.util.module_from_spec(spec); loader.exec_module(m)
    store=seed(tmp_path)
    if historical:
        store.save_task({'task_id':'old','workflow_id':'wf','run_id':'old-run','status':'cleaned','integration_mode':'none','pane_id':'p','agent':'codex','runtime':{'agent':'codex','agent_session_id':'old-session','pane_id':'p'}})
    store.save_task({'task_id':'t','workflow_id':'wf','run_id':'r','status':'cleaned','integration_mode':'none','pane_id':'p','agent':'codex','runtime':{'agent':'codex','agent_session_id':'owned','pane_id':'p'}})
    monkeypatch.setattr(m,'_get_store',lambda:store)
    monkeypatch.setattr(m,'load_tasks',lambda:{'tasks':store.list_tasks()})
    monkeypatch.setattr(m,'project_for_workflow',lambda w:None)
    monkeypatch.setattr(m,'stage_reset',lambda w:None)
    monkeypatch.setattr(m,'dump_transcript',lambda t:None)
    monkeypatch.setattr(m,'WORKFLOWS_FILE',str(tmp_path/'workflows.json'))
    closed=[]
    def transport(*args):
        if args[:2]==('pane','get'): payload={'result':{'pane':{'pane_id':'p','agent_session':'owned'}}}
        elif args[:2]==('agent','get'):payload={'result':{'agent':{'agent_session':'owned','agent_status':'idle'}}}
        elif args[:2]==('pane','close'):closed.append(args[2]);payload={}
        else:payload={}
        return subprocess.CompletedProcess(args,0,json.dumps(payload),'')
    monkeypatch.setattr(m,'_herdr',transport)
    report=m.close_workflow('wf'); assert report['tasks'][-1]['pane_closed'] is True and closed==['p']
    assert m.close_workflow('wf')['operation_id']==report['operation_id'] and closed==['p']


def test_real_reopen_cli_starts_new_close_operation(tmp_path):
    store=seed(tmp_path)
    store.save_workflow({**store.get_workflow('wf'),'coordinator_pane_id':'coordinator'})
    transport=tmp_path/'.local/bin/herdr'; transport.parent.mkdir(parents=True)
    transport.write_text("#!/bin/sh\necho '{\"result\":{\"panes\":[{\"pane_id\":\"coordinator\"}]}}'\n")
    transport.chmod(0o755)
    assert cli(tmp_path).returncode==0
    with __import__('sqlite3').connect(store.db_path) as c:
        old=json.loads(c.execute('SELECT receipt FROM workflow_close_operations WHERE workflow_id=?',('wf',)).fetchone()[0])
    reopened=subprocess.run([sys.executable,str(ROOT/'bin/herdr-task'),'reopen-workflow','wf'],env=env(tmp_path),text=True,capture_output=True,timeout=20)
    assert reopened.returncode==0,reopened.stderr+reopened.stdout
    second=cli(tmp_path)
    assert second.returncode==0,second.stderr
    assert store.get_workflow('wf')['status']=='completed','reopened workflow was left active by stale close receipt'
    with __import__('sqlite3').connect(store.db_path) as c:
        receipts=[json.loads(r[0]) for r in c.execute('SELECT receipt FROM workflow_close_operations WHERE workflow_id=?',('wf',))]
    assert len(receipts)==2 and len({r['operation_id'] for r in receipts})==2
    assert old in receipts


def hold_completed_before_receipt(db,ready,release):
    from herdr.workflow_close import workflow_close_claim
    store=SQLiteStateStore(db)
    with workflow_close_claim(store,'wf') as claim:
        store.save_workflow({**store.get_workflow('wf'),'status':'completed'})
        ready.set(); assert release.wait(15)
        claim.complete({'done':True})


def test_reopen_cli_refuses_close_publication_window(tmp_path):
    store=seed(tmp_path)
    store.save_workflow({**store.get_workflow('wf'),'coordinator_pane_id':'coordinator'})
    transport=tmp_path/'.local/bin/herdr'; transport.parent.mkdir(parents=True)
    marker=tmp_path/'ui-read'
    transport.write_text("#!/bin/sh\ntouch '"+str(marker)+"'\necho '{\"result\":{\"panes\":[{\"pane_id\":\"coordinator\"}]}}'\n")
    transport.chmod(0o755)
    ctx=mp.get_context('spawn'); ready=ctx.Event(); release=ctx.Event()
    owner=ctx.Process(target=hold_completed_before_receipt,args=(str(store.db_path),ready,release));owner.start()
    argv=[sys.executable,str(ROOT/'bin/herdr-task'),'reopen-workflow','wf']
    try:
        assert ready.wait(10)
        result=subprocess.run(argv,env=env(tmp_path),text=True,capture_output=True,timeout=20)
        assert result.returncode==75,result.stdout+result.stderr
        assert store.get_workflow('wf')['status']=='completed'
        assert not marker.exists(),'busy reopen must not query native UI'
    finally:release.set();owner.join(15)
    assert owner.exitcode==0
    result=subprocess.run(argv,env=env(tmp_path),text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert store.get_workflow('wf')['status']=='in_progress' and marker.exists()


def test_dry_run_never_reports_executed_resources_or_queries_transport(tmp_path,monkeypatch):
    from tests.test_fix_loop_pr1 import _load_module
    m=_load_module('close_dry_run_semantics',ROOT/'bin/herdr-task')
    store=seed(tmp_path);clone=tmp_path/'clone';clone.mkdir()
    subprocess.run(['git','-C',str(clone),'init'],check=True,capture_output=True)
    store.save_task({'task_id':'t','workflow_id':'wf','status':'cleaned','pane_id':'p','clone_path':str(clone),'integration_ref':'refs/integrated'})
    monkeypatch.setattr(m,'_get_store',lambda:store)
    monkeypatch.setattr(m,'load_tasks',lambda:{'tasks':store.list_tasks()})
    monkeypatch.setattr(m,'_workflow_stage_tabs',lambda *a:{'workspace_id':'w','tab_ids':['tab'],'owned_pane_ids':{'p'},'coordinator_pane':'coordinator'})
    monkeypatch.setattr(m,'_herdr',lambda *a:(_ for _ in ()).throw(AssertionError('dry-run queried native transport')))
    report=m.close_workflow('wf',dry_run=True,include_coordinator=True)
    row=report['tasks'][0]
    assert row['pane_closed'] is False and row['clone_deleted'] is False
    assert row['pane_would_close'] is False,'unknown runtime must not predict closure'
    assert row['clone_would_delete'] is True
    assert report['tabs_closed']==[] and report['tabs_would_close']==[]
    assert report['coordinator_closed'] is False and report['execution_mode']=='dry-run'
    assert store.get_workflow('wf')['status']=='running' and clone.exists()


def test_abort_names_clone_path_for_unsettled_git(tmp_path):
    store=seed(tmp_path)
    store.save_task({'task_id':'impl','workflow_id':'wf','status':'completed','integration_mode':'git','clone_path':'/tmp/demo/clones/impl-atomic-fix'})
    result=cli(tmp_path)
    assert result.returncode==2,(result.stdout,result.stderr)
    assert 'impl' in result.stdout
    assert '/tmp/demo/clones/impl-atomic-fix' in result.stdout


def test_abort_names_status_and_clone_for_blocking(tmp_path):
    store=seed(tmp_path)
    store.save_task({'task_id':'w1','workflow_id':'wf','status':'working','integration_mode':'git','clone_path':'/tmp/demo/clones/w1'})
    result=cli(tmp_path)
    assert result.returncode==2,(result.stdout,result.stderr)
    assert 'w1' in result.stdout and 'working' in result.stdout
    assert '/tmp/demo/clones/w1' in result.stdout


def test_abort_substitutes_real_clone_in_remediation(tmp_path):
    store=seed(tmp_path)
    clone=tmp_path/'clone';clone.mkdir()
    subprocess.run(['git','-C',str(clone),'init','-q'],check=True)
    subprocess.run(['git','-C',str(clone),'config','user.email','t@e.invalid'],check=True)
    subprocess.run(['git','-C',str(clone),'config','user.name','t'],check=True)
    (clone/'note.txt').write_text('unpreserved\n')
    store.save_task({'task_id':'g1','workflow_id':'wf','status':'integrated','integration_mode':'git','clone_path':str(clone)})
    result=cli(tmp_path)
    assert result.returncode==2,(result.stdout,result.stderr)
    assert '<clone>' not in result.stdout
    assert str(clone) in result.stdout
