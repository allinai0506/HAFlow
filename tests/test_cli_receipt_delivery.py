import importlib.machinery
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest
from herdr.state_store import SQLiteStateStore

@pytest.fixture
def scene(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[1]/'bin/herdr-task'
    loader=importlib.machinery.SourceFileLoader('delivery_cli',str(path)); spec=importlib.util.spec_from_loader(loader.name,loader)
    cli=importlib.util.module_from_spec(spec);loader.exec_module(cli)
    store=SQLiteStateStore(tmp_path/'state.db');store.save_workflow({'workflow_id':'wf','status':'running'})
    task={'task_id':'t','workflow_id':'wf','run_id':'r','status':'pending','pane_id':'p','clone_path':str(tmp_path)};store.save_task(task)
    monkeypatch.setattr(cli,'_get_store',lambda:store)
    monkeypatch.setattr(cli,'load_tasks',lambda:{'tasks':store.list_tasks()})
    monkeypatch.setattr(cli,'_compile_working_context_ref',lambda task:None)
    monkeypatch.setattr(cli.time,'sleep',lambda seconds:None)
    return cli,store

@pytest.mark.parametrize('kind',['initial','rework'])
@pytest.mark.parametrize('window',['before_prepared','after_prepared','before_prompt','after_prompt'])
def test_real_cli_entry_crash_recovery(scene,monkeypatch,kind,window):
    cli,store=scene
    from herdr import supervisor_delivery as delivery
    calls=[]
    monkeypatch.setattr(cli.subprocess,'run',lambda *a,**k:(calls.append(a) or SimpleNamespace(returncode=0,stdout='',stderr='')))
    monkeypatch.setattr(cli,'_herdr',lambda *a,**k:(calls.append(a) or SimpleNamespace(returncode=0,stdout='',stderr='')))
    import herdr.task_resources
    monkeypatch.setattr(herdr.task_resources,'owned_live_pane',lambda task:(True,'identity_match'))
    if kind=='rework':
        task=store.get_task('t');task.update(completion_protocol='receipt-v1',status='working');store.save_task(task)
    class Crash(BaseException):pass
    original=delivery.deliver
    def crashing(*a,**k):
        def boundary(phase):
            if phase==window:raise Crash()
        return original(*a,**k,boundary=boundary)
    monkeypatch.setattr(delivery,'deliver',crashing)
    def invoke():
        if kind=='initial':cli.dispatch_task('t','work')
        else:cli.cmd_rework(SimpleNamespace(task_id='t',request_id='q',prompt='fix',reason='review'))
    with pytest.raises(Crash):invoke()
    epoch=store.get_task('t').get('completion_epoch')
    monkeypatch.setattr(delivery,'deliver',original)
    if window in ('before_prepared','after_prepared'):
        invoke();assert len(calls)==1
    else:
        with pytest.raises(delivery.DeliveryUnknown):invoke()
        assert len(calls)==(window=='after_prompt')
    if epoch: assert store.get_task('t')['completion_epoch']==epoch
