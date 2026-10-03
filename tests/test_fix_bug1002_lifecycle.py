import json
from pathlib import Path
from herdr.state_store import get_state_store
from herdr.task_resources import begin_launch_intent, record_launch_resources, write_worker_launch_identity


def test_owned_unstarted_launch_can_reclaim_and_retry(tmp_path):
    from herdr.task_resources import recover_launch_resources
    store = get_state_store(tmp_path/'state.db')
    intent = begin_launch_intent(store, workflow_id='w', node_id='n', task_id='t')['intent']
    clone = tmp_path/'t'; clone.mkdir()
    intent = record_launch_resources(store,intent,{'planned_clone_path':str(clone),'run_id':'r'})
    write_worker_launch_identity(clone, {'intent_id':intent['intent_id'],'task_id':'t','run_id':'r','pane_id':'p','pane_source':'dynamic','terminal_id':'term-private','phase':'pane_allocated'},initial=True)
    calls=[]
    closed=False
    def native(argv,timeout):
        nonlocal closed
        calls.append(argv)
        if argv[1:3]==['pane','list']: value={'panes':[] if closed else [{'pane_id':'p','cwd':str(clone)}]}
        elif argv[1:3]==['pane','get']: value={'pane':{'pane_id':'p','terminal_id':'term-private','cwd':str(clone)}}
        elif argv[1:3]==['agent','get']: value={'agent':None}
        elif argv[1:3]==['pane','close']: closed=True;value={}
        else: raise AssertionError(argv)
        return 0,json.dumps({'result':value}),''
    result=recover_launch_resources(store,intent,runner=native)
    assert result['status']=='resources_absent'
    assert not clone.exists()
    assert any(a[1:3]==['pane','close'] for a in calls)
    assert begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t')['status']=='claimed'


def test_start_requested_and_foreign_agent_never_reclaimed(tmp_path):
    from herdr.task_resources import recover_launch_resources
    store=get_state_store(tmp_path/'state.db')
    intent=begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t')['intent']
    clone=tmp_path/'t';clone.mkdir()
    intent=record_launch_resources(store,intent,{'planned_clone_path':str(clone),'run_id':'r'})
    write_worker_launch_identity(clone,{'intent_id':intent['intent_id'],'task_id':'t','run_id':'r','pane_id':'p','pane_source':'dynamic','phase':'agent_start_requested'},initial=True)
    calls=[]
    def native(argv,timeout):
        calls.append(argv)
        return 0,json.dumps({'result':{'panes':[{'pane_id':'p','cwd':str(clone)}]}}),''
    assert recover_launch_resources(store,intent,runner=native)['status']=='recovery_required'
    assert clone.exists()
    assert not any(a[1:3]==['pane','close'] for a in calls)


def test_legacy_completion_authorization_never_marks_success(tmp_path):
    from herdr.completion_receipt import authorize_legacy_completion
    store=get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'w','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'w','status':'working','run_id':'r'})
    task=store.get_task('t')
    contract=authorize_legacy_completion('t','r',task['version'],'operator checked run evidence',store)
    assert Path(contract['path']).exists()
    assert store.get_task('t')['status']=='working'
    assert store.get_task('t')['completion_protocol']=='receipt-v1'
    assert store.list_events(task_id='t',event_type='completion_authorized')


def test_worker_rolls_back_only_unstarted_private_dynamic_pane(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path/"worker.db"))
    import importlib.util
    spec=importlib.util.spec_from_file_location('worker_lifecycle',Path(__file__).resolve().parents[1]/'services/herdr-worker.py')
    worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    clone=tmp_path/'t';clone.mkdir()
    identity={'intent_id':'i','task_id':'t','run_id':'r','phase':'workspace_created','terminal_id':'term-owned'}
    write_worker_launch_identity(clone,identity,initial=True)
    calls=[]
    def native(argv, timeout=None):
        calls.append(argv)
        if argv[1:3]==['pane','get']: return {'result':{'pane':{'cwd':str(clone),'terminal_id':'term-owned'}}}
        if argv[1:3]==['agent','get']: return {'result':{'agent':None}}
        return {'result':{}}
    worker.run_json=native
    assert worker.rollback_unstarted_pane(clone,'p','dynamic',identity)
    assert calls[-1]==['herdr','pane','close','p']
    assert not worker.rollback_unstarted_pane(clone,'p','prebuilt',identity)
    calls.clear()
    assert not worker.rollback_unstarted_pane(clone,'p','dynamic',{**identity,'terminal_id':'replaced'})
    assert not worker.rollback_unstarted_pane(clone,'p','dynamic',{**identity,'terminal_id':None})
    assert not any(a[1:3]==['pane','close'] for a in calls)


def test_real_reconcile_role_mismatch_explains_available_role(tmp_path):
    import os, subprocess, sys
    store=get_state_store(tmp_path/'state.db')
    begin_launch_intent(store,workflow_id='w',node_id='n',role='reviewer',task_id='t')
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'HOME':str(tmp_path),'HERDR_STATE_DB':str(store.db_path),'TASKS_FILE':str(tmp_path/'tasks.json'),'WORKFLOWS_FILE':str(tmp_path/'workflows.json')}
    result=subprocess.run([sys.executable,str(root/'bin/herdr-task'),'launch-reconcile','--workflow-id','w','--node','n'],env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode==2
    assert 'role mismatch: requested=worker; available=reviewer' in result.stderr


def test_legacy_authorization_rejects_foreign_run_and_stale_version(tmp_path):
    import pytest
    from herdr.completion_receipt import authorize_legacy_completion
    store=get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'w','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'w','status':'working','run_id':'r'})
    before=store.get_task('t')
    for run,version in [('foreign',before['version']),('r',before['version']+1)]:
        with pytest.raises(ValueError):
            authorize_legacy_completion('t',run,version,'proof',store)
    assert store.get_task('t').get('completion_protocol') is None
    assert not store.list_events(task_id='t',event_type='completion_authorized')


def test_failed_launch_is_recoverable_before_lease_expiry(tmp_path):
    store=get_state_store(tmp_path/'state.db')
    intent=begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t',now=100)['intent']
    record_launch_resources(store,intent,{'allocation_failed':True},now=101)
    assert begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t',now=102)['status']=='recovery_required'


def test_native_terminal_aba_or_missing_token_never_closes(tmp_path):
    from herdr.task_resources import recover_launch_resources
    store=get_state_store(tmp_path/'state.db')
    intent=begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t')['intent']
    clone=tmp_path/'t';clone.mkdir()
    intent=record_launch_resources(store,intent,{'planned_clone_path':str(clone),'run_id':'r'})
    identity={'intent_id':intent['intent_id'],'task_id':'t','run_id':'r','pane_id':'p','pane_source':'dynamic','phase':'pane_allocated'}
    calls=[]
    def native(argv,timeout):
        calls.append(argv)
        if argv[1:3]==['pane','list']: val={'panes':[{'pane_id':'p','cwd':str(clone)}]}
        else: val={'pane':{'pane_id':'p','cwd':str(clone),'terminal_id':'replacement'}}
        return 0,json.dumps({'result':val}),''
    for token in (None,'original'):
        write_worker_launch_identity(clone,{**identity,'terminal_id':token},initial=not (clone/'.herdr-launch-identity.json').exists())
        assert recover_launch_resources(store,intent,runner=native)['status']=='recovery_required'
    assert clone.exists()
    assert not any(c[1:3]==['pane','close'] for c in calls)


def test_real_legacy_authorize_cli_returns_report_command_without_success(tmp_path):
    import os, subprocess, sys
    store=get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'w','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'w','status':'working','run_id':'r'})
    task=store.get_task('t')
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'HOME':str(tmp_path),'HERDR_STATE_DB':str(store.db_path),'TASKS_FILE':str(tmp_path/'tasks.json'),'WORKFLOWS_FILE':str(tmp_path/'workflows.json')}
    result=subprocess.run([sys.executable,str(root/'bin/herdr-task'),'authorize-completion','t','--run-id','r','--expected-version',str(task['version']),'--reason','run verified by operator'],env=env,text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads(result.stdout)
    assert store.get_task('t')['status']=='working'
    assert 'report-completion' in result.stdout


def test_explicit_legacy_pane_authorization_only_binds_token(tmp_path):
    from herdr.task_resources import authorize_launch_recovery
    store=get_state_store(tmp_path/'state.db')
    intent=begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t')['intent']
    clone=tmp_path/'t';clone.mkdir()
    intent=record_launch_resources(store,intent,{'planned_clone_path':str(clone),'run_id':'r'})
    write_worker_launch_identity(clone,{'intent_id':intent['intent_id'],'task_id':'t','run_id':'r','pane_id':'p','pane_source':'dynamic','phase':'workspace_created'},initial=True)
    calls=[]
    def native(argv,timeout):
        calls.append(argv)
        if argv[1:3]==['pane','list']: val={'panes':[{'pane_id':'p','cwd':str(clone)}]}
        elif argv[1:3]==['pane','get']: val={'pane':{'pane_id':'p','cwd':str(clone),'terminal_id':'term-owned'}}
        elif argv[1:3]==['agent','get']: val={'agent':None}
        else: raise AssertionError(argv)
        return 0,json.dumps({'result':val}),''
    result=authorize_launch_recovery(store,intent,'term-owned','operator verified start was never attempted',runner=native)
    assert result['authorized']
    assert clone.exists()
    assert json.loads((clone/'.herdr-launch-identity.json').read_text())['terminal_id']=='term-owned'
    assert not any(c[1:3]==['pane','close'] for c in calls)
    assert store.list_events(task_id='t',event_type='launch_recovery_authorized')


def test_recovery_never_declares_absent_with_second_workspace_pane(tmp_path):
    from herdr.task_resources import recover_launch_resources
    store=get_state_store(tmp_path/'state.db')
    intent=begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t')['intent']
    clone=tmp_path/'t';clone.mkdir()
    intent=record_launch_resources(store,intent,{'planned_clone_path':str(clone),'run_id':'r'})
    write_worker_launch_identity(clone,{'intent_id':intent['intent_id'],'task_id':'t','run_id':'r','pane_id':'p',
        'pane_source':'dynamic','terminal_id':'term-owned','phase':'pane_allocated'},initial=True)
    closed=False
    def native(argv,timeout):
        nonlocal closed
        if argv[1:3]==['pane','list']:
            panes=[{'pane_id':'second','cwd':str(clone)}]
            if not closed:panes.insert(0,{'pane_id':'p','cwd':str(clone)})
            result={'panes':panes}
        elif argv[1:3]==['pane','get']:result={'pane':{'pane_id':'p','terminal_id':'term-owned','cwd':str(clone)}}
        elif argv[1:3]==['agent','get']:result={'agent':None}
        elif argv[1:3]==['pane','close']:closed=True;result={}
        else:raise AssertionError(argv)
        return 0,json.dumps({'result':result}),''
    assert recover_launch_resources(store,intent,runner=native)['status']=='recovery_required'
    assert clone.exists()
    assert begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t')['status']!='claimed'


def test_independent_recovery_processes_close_once(tmp_path):
    import os, subprocess, sys
    store=get_state_store(tmp_path/'state.db')
    intent=begin_launch_intent(store,workflow_id='w',node_id='n',task_id='t')['intent']
    clone=tmp_path/'t';clone.mkdir()
    intent=record_launch_resources(store,intent,{'planned_clone_path':str(clone),'run_id':'r'})
    write_worker_launch_identity(clone,{'intent_id':intent['intent_id'],'task_id':'t','run_id':'r','pane_id':'p',
        'pane_source':'dynamic','terminal_id':'term-owned','phase':'pane_allocated'},initial=True)
    (tmp_path/'intent.json').write_text(json.dumps(intent))
    (tmp_path/'pane.json').write_text(json.dumps({'pane_id':'p','cwd':str(clone),'terminal_id':'term-owned'}))
    script=tmp_path/'recover.py'
    script.write_text('''import json,sys,time
from pathlib import Path
from herdr.state_store import get_state_store
from herdr.task_resources import recover_launch_resources
root=Path(sys.argv[1]);store=get_state_store(root/'state.db');intent=json.loads((root/'intent.json').read_text())
def native(argv,budget):
    pane=json.loads((root/'pane.json').read_text())
    if argv[1:3]==['pane','list']:
        time.sleep(.1); result={'panes':[pane] if pane else []}
    elif argv[1:3]==['pane','get']:result={'pane':pane}
    elif argv[1:3]==['agent','get']:result={'agent':None}
    elif argv[1:3]==['pane','close']:
        with (root/'closed').open('a') as stream:stream.write('close\\n')
        (root/'pane.json').write_text('null'); result={}
    else:raise AssertionError(argv)
    return 0,json.dumps({'result':result}),''
print(json.dumps(recover_launch_resources(store,intent,runner=native)))
''')
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    children=[subprocess.Popen([sys.executable,str(script),str(tmp_path)],env=env,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
    results=[]
    for child in children:
        out,err=child.communicate(timeout=30)
        assert child.returncode==0,err
        results.append(json.loads(out))
    assert all(row['status']=='resources_absent' for row in results)
    assert (tmp_path/'closed').read_text().splitlines()==['close']
    assert not clone.exists()
