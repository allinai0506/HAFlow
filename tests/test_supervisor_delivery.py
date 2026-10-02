import importlib
from pathlib import Path
import pytest
from herdr.state_store import get_state_store

@pytest.fixture
def scene(tmp_path):
    store = get_state_store(tmp_path / 'state.db')
    task = {'task_id':'t','workflow_id':'wf','node':'n','run_id':'r','pane_id':'p','status':'rework','completion_protocol':'receipt-v1'}
    store.save_task(task)
    return store, task

@pytest.mark.parametrize('action', ['RETRY','VERIFY'])
@pytest.mark.parametrize('window', ['before_prepared','after_prepared','before_prompt','after_prompt'])
def test_delivery_crash_windows(scene, monkeypatch, action, window):
    from importlib.util import find_spec
    assert find_spec('herdr.supervisor_delivery'), 'durable receipt delivery missing'
    api = importlib.import_module('herdr.supervisor_delivery')
    store, task = scene
    calls=[]
    class Crash(BaseException): pass
    def boundary(phase):
        if phase == window: raise Crash()
    def send(pane,prompt): calls.append(prompt)
    with pytest.raises(Crash):
        api.deliver(task,store,action,{'intervention_id':'i','action':action},'static prompt',send,boundary=boundary)
    epoch = store.get_task('t').get('completion_epoch')
    if window in ('before_prompt','after_prompt'):
        with pytest.raises(api.DeliveryUnknown) as raised:
            api.deliver(task,store,action,{'intervention_id':'i','action':action},'static prompt',send)
        assert raised.value.intervention_error['side_effects'] == 'unknown'
        assert len(calls) == (window == 'after_prompt')
        assert not store.list_events(task_id='t',event_type=('retry' if action=='RETRY' else 'verification')+'_dispatched')
    else:
        result=api.deliver(task,store,action,{'intervention_id':'i','action':action},'static prompt',send)
        assert result[('retry' if action=='RETRY' else 'verification')+'_dispatched']
        assert len(calls)==1
        if epoch: assert store.get_task('t')['completion_epoch']==epoch
        api.deliver(task,store,action,{'intervention_id':'i','action':action},'static prompt',send)
        assert len(calls)==1
    events=store.list_events(task_id='t')
    assert 'token' not in str([e['payload'] for e in events])

@pytest.mark.parametrize('action,fn', [('RETRY','_dispatch_supervisor_retry'),('VERIFY','_dispatch_supervisor_verification')])
def test_controller_real_entry_retains_unknown_intent(scene, monkeypatch, action, fn):
    controller=importlib.import_module('services.herdr-controller')
    store,task=scene
    monkeypatch.setattr(controller,'_working_context_ref_for_task',lambda *a,**k: None)
    monkeypatch.setattr(controller,'_verification_snapshot',lambda *a: {})
    calls=[]
    def native(*a,**k):
        calls.append(a)
        raise TimeoutError('native ambiguous')
    monkeypatch.setattr(controller.subprocess,'run',native)
    decision={'intervention':{'intervention_id':'i','decision_id':'d'}}
    from herdr.supervisor_delivery import DeliveryUnknown
    for _ in range(2):
        with pytest.raises(DeliveryUnknown): getattr(controller,fn)(task,decision,store)
    assert len(calls)==1
    assert store.get_task('t')['completion_epoch']
    assert not store.list_events(task_id='t',event_type=('retry' if action=='RETRY' else 'verification')+'_dispatched')

def test_prepared_receipt_failure_rolls_back_epoch_and_private_file(scene,monkeypatch):
    from herdr import supervisor_delivery as api
    store,task=scene
    original=api.state_db.record_event
    def reject(event,**kwargs):
        if event['event_type']=='retry_dispatch_prepared': raise RuntimeError('disk failure')
        return original(event,**kwargs)
    monkeypatch.setattr(api.state_db,'record_event',reject)
    with pytest.raises(RuntimeError,match='disk failure'):
        api.deliver(task,store,'RETRY',{'intervention_id':'i'},'static',lambda *a: pytest.fail('must not send'))
    assert not store.get_task('t').get('completion_epoch')
    assert not list((Path(store.db_path).parent/'completion-credentials').glob('receipt-*'))

def test_old_receipt_intent_without_delivery_phase_is_unknown(scene):
    from herdr import supervisor_delivery as api
    store,task=scene
    store.record_event('retry_dispatch_intent',{'intervention_id':'i','action':'RETRY'},task_id='t',run_id='r',source='supervisor')
    with pytest.raises(api.DeliveryUnknown):
        api.deliver(task,store,'RETRY',{'intervention_id':'i'},'static',lambda *a: pytest.fail('must not guess resend'))
    assert not store.get_task('t').get('completion_epoch')
