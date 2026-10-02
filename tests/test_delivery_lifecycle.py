import importlib.machinery,importlib.util,json,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from herdr.state_store import SQLiteStateStore
from herdr import completion_receipt as receipts
from herdr import supervisor_delivery as delivery

@pytest.fixture
def scene(tmp_path,monkeypatch):
    store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'wf','run_id':'r','status':'working','pane_id':'p','integration_mode':'none','started_at':time.time()-100})
    issued=receipts.issue_completion_contract('t',store)
    receipts.report_completion('t',json.loads(Path(issued['path']).read_text()),[],store)
    loader=importlib.machinery.SourceFileLoader('lifecycle_cli',str(Path(__file__).resolve().parents[1]/'bin/herdr-task'));spec=importlib.util.spec_from_loader(loader.name,loader);cli=importlib.util.module_from_spec(spec);loader.exec_module(cli)
    monkeypatch.setattr(cli,'_get_store',lambda:store);monkeypatch.setattr(cli,'load_tasks',lambda:{'tasks':store.list_tasks()})
    for name in ['stage_reset','dump_transcript']:monkeypatch.setattr(cli,name,lambda *a:None)
    monkeypatch.setattr(cli,'project_for_workflow',lambda *a:None)
    monkeypatch.setattr(cli,'WORKFLOWS_FILE',str(tmp_path/'workflows.json'))
    monkeypatch.setattr(cli,'_herdr',lambda *args:SimpleNamespace(returncode=0,stdout='{}',stderr=''))
    return store,cli

def test_real_completion_and_close_after_prepared_never_sends_stale_pane(scene):
    store,cli=scene;sent=[];task=store.get_task('t')
    def boundary(phase):
        if phase=='after_prepared':
            assert receipts.consume_completion_receipt('t',store,now=time.time()+61)['accepted']
            assert store.transition_task('t','completed',reason='accepted',force=True)['accepted']
            report=cli.close_workflow('wf');assert report['close_state']=='completed'
    with pytest.raises(delivery.DeliveryUnknown):
        delivery.deliver(task,store,'RENEW',{'intervention_id':'renew-after-close'},'renew',lambda *a:sent.append(a),boundary=boundary)
    assert not sent
    assert not store.list_events(task_id='t',event_type='renewal_dispatched')

def hold_lifecycle(db,ready,release):
    from herdr.workflow_close import workflow_lifecycle_lock
    with workflow_lifecycle_lock(SimpleNamespace(db_path=db),'wf') as acquired:
        assert acquired;ready.set();assert release.wait(10)

def test_delivery_contends_with_independent_close_owner_without_ui(scene):
    import multiprocessing as mp
    store,_=scene;ctx=mp.get_context('spawn');ready=ctx.Event();release=ctx.Event()
    process=ctx.Process(target=hold_lifecycle,args=(str(store.db_path),ready,release));process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(delivery.DeliveryUnknown):
            delivery.deliver(store.get_task('t'),store,'RENEW',{'intervention_id':'locked-renew'},'renew',lambda *a:pytest.fail('lifecycle owner forbids UI'))
        assert not store.list_events(task_id='t',event_type='renewal_transport_started')
    finally:release.set();process.join(10)
    assert process.exitcode==0

def test_close_cannot_own_lifecycle_while_native_transport_runs(scene):
    from herdr.workflow_close import workflow_close_claim
    store,_=scene
    def send(*args):
        with workflow_close_claim(store,'wf') as claim:
            assert claim.state=='in_progress'
    assert delivery.deliver(store.get_task('t'),store,'RENEW',{'intervention_id':'safe-renew'},'renew',send)['renewal_dispatched']

@pytest.mark.parametrize('mutation',['pane','epoch','status'])
def test_native_instance_probe_then_authority_drift_rejected(scene,monkeypatch,mutation):
    store,_=scene;task=store.get_task('t')
    import herdr.task_resources
    def probe(fresh):
        if mutation=='status':store.transition_task('t','agent_done',reason='concurrent',force=True)
        else:store.update_task_metadata('t',{'pane_id':'new-pane'} if mutation=='pane' else {'completion_epoch':'new-epoch'})
        return {'status':'available','reason':'identity_match'}
    monkeypatch.setattr(herdr.task_resources,'probe_live_runtime',probe)
    with pytest.raises(delivery.DeliveryUnknown):delivery.current_delivery_task(task,store,native=True)
