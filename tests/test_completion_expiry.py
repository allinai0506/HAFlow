import json
from pathlib import Path
import time
import pytest
from herdr import completion_receipt as api
from herdr.state_store import SQLiteStateStore

@pytest.fixture
def scene(tmp_path):
    store=SQLiteStateStore(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf','status':'running'})
    store.save_task({'task_id':'t','workflow_id':'wf','run_id':'r','status':'working','started_at':time.time()-100})
    contract=api.issue_completion_contract('t',store)
    return store,contract,json.loads(Path(contract['path']).read_text())

def test_server_expiry_persisted_and_bounded(scene):
    store,contract,identity=scene
    task=store.get_task('t')
    assert task['completion_expires_at']-task['completion_issued_at']==86400
    assert identity.get('expires_at') is None  # client cannot control authority

@pytest.mark.parametrize('metadata',[{'completion_expires_at':1},{'completion_expires_at':None},{'completion_issued_at':None}])
def test_report_rejects_expired_or_unknown_without_writes(scene,metadata):
    store,contract,identity=scene
    store.update_task_metadata('t',metadata)
    with pytest.raises(ValueError,match='expiry'): api.report_completion('t',identity,[],store)
    assert store.get_task('t')['status']=='working'
    assert not api.pending_completion_task_ids(store)

@pytest.mark.parametrize('unknown',[False,True])
def test_expiry_between_report_and_consume_excluded_from_pending(scene,unknown):
    store,contract,identity=scene
    api.report_completion('t',identity,[],store)
    now=store.get_task('t')['completion_expires_at']+1
    if unknown: store.update_task_metadata('t',{'completion_expires_at':None})
    assert not api.pending_completion_task_ids(store,now=now)
    result=api.consume_completion_receipt('t',store,now=now)
    assert result=={'accepted':False,'reason':'completion_expiry_unknown' if unknown else 'completion_expired'}
    assert store.get_task('t')['status']=='working'

def test_exact_expiry_boundary_rejected_and_client_fields_ignored(scene,monkeypatch):
    store,contract,identity=scene
    expiry=store.get_task('t')['completion_expires_at']
    identity['expires_at']=expiry+100000
    monkeypatch.setattr(api.time,'time',lambda:expiry)
    with pytest.raises(ValueError,match='expiry'): api.report_completion('t',identity,[],store)
