import json,time,importlib
from pathlib import Path
import pytest
from herdr.state_store import SQLiteStateStore
from herdr import completion_receipt as api

@pytest.fixture
def scene(tmp_path):
    store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'wf','run_id':'r','status':'working','pane_id':'p','started_at':time.time()-100})
    old=api.issue_completion_contract('t',store)
    return store,old

def test_renew_preserves_epoch_progress_revokes_bearer(scene):
    store,old=scene;oldidentity=json.loads(Path(old['path']).read_text());before=store.get_task('t')
    renewed=api.issue_completion_contract('t',store,renew=True)
    after=store.get_task('t');assert renewed['epoch']==old['epoch']
    assert after['completion_epoch_started_at']==before['completion_epoch_started_at']
    with pytest.raises(ValueError):api.report_completion('t',oldidentity,[],store)
    api.report_completion('t',json.loads(Path(renewed['path']).read_text()),[],store)

@pytest.mark.parametrize('window',['after_prepared','before_prompt','after_prompt'])
def test_renew_delivery_recovery_and_old_prepared_invalidated(scene,window):
    from herdr import supervisor_delivery as delivery
    store,old=scene;calls=[];task=store.get_task('t')
    store.update_task_metadata('t',{'completion_expires_at':time.time()-1})
    class Crash(BaseException):pass
    def boundary(phase):
        if phase==window:raise Crash()
    with pytest.raises(Crash): delivery.deliver(task,store,'RENEW',{'intervention_id':'renew-q'},'renew',lambda *a:calls.append(a),boundary=boundary)
    if window=='after_prepared':
        delivery.deliver(task,store,'RENEW',{'intervention_id':'renew-q'},'renew',lambda *a:calls.append(a));assert len(calls)==1
    else:
        with pytest.raises(delivery.DeliveryUnknown):delivery.deliver(task,store,'RENEW',{'intervention_id':'renew-q'},'renew',lambda *a:calls.append(a))
        assert len(calls)==(window=='after_prompt')
    assert store.get_task('t')['completion_epoch']==old['epoch']

def test_controller_keyset_advances_past_blocked_hundred(tmp_path,monkeypatch):
    store=SQLiteStateStore(tmp_path/'fair.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    for i in range(101):
        task_id=f't{i}';store.save_task({'task_id':task_id,'workflow_id':'wf','run_id':task_id,'status':'working','started_at':time.time()-100})
        issued=api.issue_completion_contract(task_id,store)
        api.report_completion(task_id,json.loads(Path(issued['path']).read_text()),[],store)
        if i<100:store.create_intervention({'action':'VERIFY','decision_id':f'd{i}','task_id':task_id,'run_id':task_id,'workflow_id':'wf','requested_at':time.time()})
    controller=importlib.import_module('services.herdr-controller');monkeypatch.setattr(controller,'_get_store',lambda:store)
    now=time.time()+61
    # The former scheduler repeatedly selects the same first 100; prove the fixture's starvation.
    def legacy_batch():
        return sum(bool(api.consume_completion_receipt(task_id,store,now=now).get('accepted'))
                   for task_id in api.pending_completion_task_ids(store,now=now))
    assert legacy_batch()==legacy_batch()==0
    assert controller.process_structured_completions(now=now)==0
    assert controller.process_structured_completions(now=now)==1
    assert store.get_task('t100')['status']=='agent_done'

def test_real_cli_renew_entry_preserves_checkpoint_and_expired_old_prepared(scene,monkeypatch):
    import importlib.util,importlib.machinery
    from types import SimpleNamespace
    from herdr import supervisor_delivery as delivery
    from herdr.task_checkpoint import publish_task_checkpoint,read_task_checkpoints
    store,old=scene
    checkpoint=publish_task_checkpoint('t','r',old['epoch'],'saved progress',1,store=store)
    task=store.get_task('t')
    store.record_event('initial_dispatch_prepared',{'intervention_id':'initial:r','identity_path':old['path'],'completion_epoch':old['epoch'],'prompt':'old'},task_id='t',run_id='r',source='herdr-task')
    path=Path(__file__).resolve().parents[1]/'bin/herdr-task';loader=importlib.machinery.SourceFileLoader('renew_cli',str(path));spec=importlib.util.spec_from_loader(loader.name,loader);cli=importlib.util.module_from_spec(spec);loader.exec_module(cli)
    monkeypatch.setattr(cli,'_get_store',lambda:store)
    import herdr.task_resources
    monkeypatch.setattr(herdr.task_resources,'owned_live_pane',lambda task:(True,'identity_match'))
    calls=[];monkeypatch.setattr(cli,'_herdr',lambda *args:(calls.append(args) or SimpleNamespace(returncode=0)))
    args=SimpleNamespace(task_id='t',operation_id='explicit-q')
    cli.cmd_renew_completion(args);cli.cmd_renew_completion(args)
    assert len(calls)==1
    assert read_task_checkpoints('t','r',old['epoch'],store=store)['segments'][0]['sha256']==checkpoint['sha256']
    with pytest.raises(delivery.DeliveryUnknown):delivery.deliver(task,store,'INITIAL',{'intervention_id':'initial:r'},'old',lambda *args:pytest.fail('old credential must not send'))

def test_renew_actual_cli_parser_subprocess(scene,tmp_path):
    import os,subprocess,sys
    store,old=scene
    script='''import runpy,sys,subprocess
from types import SimpleNamespace
import herdr.task_resources
herdr.task_resources.owned_live_pane=lambda task:(True,'identity_match')
subprocess.run=lambda *a,**k:SimpleNamespace(returncode=0,stdout='',stderr='')
sys.argv=[sys.argv[1],'renew-completion','t','--operation-id','subprocess-q']
runpy.run_path(sys.argv[0],run_name='__main__')
'''
    root=Path(__file__).resolve().parents[1]
    env={**os.environ,'HERDR_STATE_DB':str(store.db_path),'HERDR_CONTROLLER_DIR':str(tmp_path),'TASKS_FILE':str(tmp_path/'tasks.json'),'WORKFLOWS_FILE':str(tmp_path/'workflows.json')}
    result=subprocess.run([sys.executable,'-c',script,str(root/'bin/herdr-task')],env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])['renewal_dispatched']
    assert store.get_task('t')['completion_epoch']==old['epoch']
