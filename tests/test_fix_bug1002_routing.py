import time
from pathlib import Path
import pytest
from herdr import agent_router as ar, deep_preflight as dp
from herdr.state_store import get_state_store

@pytest.fixture
def routing(tmp_path, monkeypatch):
    monkeypatch.setenv('HERDR_STATE_DB', str(tmp_path/'state.db'))
    monkeypatch.setattr(dp,'preflight_identity',lambda *a,**kw: {'verifiable':True,'fingerprint':'x'})
    for name, filename in [('POOLS_FILE','pools.json'),('RESERVATIONS_FILE','reservations.json'),('ROUTER_LOCK_FILE','router.lock')]:
        monkeypatch.setattr(ar,name,tmp_path/filename)
    monkeypatch.setattr(ar,'workflow_config_for',lambda _: {'nodes':[{'id':'plan','parallel':True}]})
    store=get_state_store(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf','project_id':'p','project_root':str(tmp_path),'status':'active','healthy_agents':['codex'],'unhealthy_agents':{'claude':'TIMEOUT'},'preflight_checked_at':time.time()-4000})
    ar.save_pools({'projects':{'p':{'allowed_agents':['claude','codex'],'stage_preferences':{'plan':['claude','codex']}}}})
    return store


def test_stale_candidates_require_actual_verification(routing, monkeypatch):
    monkeypatch.setattr(dp,'inspect',lambda *a, **kw: [{'agent':a,'final_status':'UNKNOWN','request_verified':False} for a in kw['target_agents']])
    with pytest.raises(RuntimeError,match='verified|Preflight|preflight'):
        ar.choose_agent('wf','plan','fix')


def test_failed_preference_falls_back_and_persists(routing,monkeypatch):
    monkeypatch.setattr(dp,'inspect',lambda *a, **kw: [{'agent':name,'final_status':'READY' if name=='codex' else 'ERROR','request_verified':name=='codex','preflight_identity':{'verifiable':True,'fingerprint':'x'}} for name in kw['target_agents']])
    monkeypatch.setattr(ar,'preflight_snapshot_fresh',lambda *a, **kw: False)
    assert ar.choose_agent('wf','plan','fix')=='codex'
    record=routing.get_workflow('wf')
    assert record['unhealthy_agents']['claude']=='ERROR'
    assert record['preflight_agent_checked_at']['codex'] > time.time()-5


def test_distinct_roles_exclude_registered_agent(routing,monkeypatch):
    record=routing.get_workflow('wf');record.update(preflight_checked_at=time.time(),healthy_agents=['codex','claude'],unhealthy_agents={});routing.save_workflow(record)
    routing.save_task({'task_id':'architect','workflow_id':'wf','project_id':'p','node':'plan','dispatch_role':'architect','agent':'claude','status':'completed'})
    assert ar.choose_agent('wf','plan','fix',dispatch_role='adversarial',reservation_key='reviewer')=='codex'


def test_partial_refresh_does_not_refresh_global_timestamp(routing,monkeypatch):
    old=routing.get_workflow('wf')['preflight_checked_at']
    monkeypatch.setattr(dp,'project_pool',lambda _: {'allowed_agents':['claude','codex']})
    monkeypatch.setattr(dp,'inspect',lambda *a,**kw: [{'agent':'codex','final_status':'READY','request_verified':True,'preflight_identity':{'verifiable':True,'fingerprint':'x'}}])
    dp.refresh_workflow_preflight('wf',['codex'],store=routing)
    record=routing.get_workflow('wf')
    assert record['preflight_checked_at']==old
    monkeypatch.setattr(ar,'preflight_snapshot_fresh',lambda r,**kw: r.get('preflight_identity',{}).get('fingerprint')=='x')
    monkeypatch.setattr(dp,'inspect',lambda *a,**kw: pytest.fail('fresh single-agent result must not be reprobed'))
    assert ar.choose_agent('wf','plan','fix',requested='codex')=='codex'


def test_refresh_cli_real_adapter_temp_store(tmp_path):
    import os, subprocess, sys, json
    home=tmp_path/'home'; root=home/'.herdr-controller';root.mkdir(parents=True)
    project=tmp_path/'project';project.mkdir()
    binary=tmp_path/'codex';binary.write_text('#!/bin/sh\ncase "$1" in --help) echo exec;; --version) echo controlled-v1;; exec) echo HERDR_PREFLIGHT_OK;; esac\n');binary.chmod(0o755)
    (root/'agent-pools.json').write_text(json.dumps({'projects':{'p':{'allowed_agents':['codex','claude']}}}))
    store=get_state_store(tmp_path/'cli.db');store.save_workflow({'workflow_id':'wf','project_id':'p','project_root':str(project),'status':'active','preflight_checked_at':1})
    env={**os.environ,'HOME':str(home),'HERDR_STATE_DB':str(tmp_path/'cli.db'),'PATH':str(tmp_path)+os.pathsep+os.environ['PATH']}
    result=subprocess.run([sys.executable,'bin/herdr-deep-preflight','--workflow-id','wf','--agent','codex','--deep','--apply','--json'],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    record=store.get_workflow('wf');assert record['healthy_agents']==['codex'];assert record['preflight_checked_at']==1
    assert record['preflight_identities']['codex']['verifiable'] is True


def test_cross_process_role_reservations(routing,tmp_path):
    import subprocess,sys,os
    record=routing.get_workflow('wf');record.update(preflight_checked_at=time.time(),healthy_agents=['codex','claude'],unhealthy_agents={});routing.save_workflow(record)
    code='''import sys
from pathlib import Path
from herdr import agent_router as ar
root=Path(sys.argv[1])
ar.POOLS_FILE=root/'pools.json';ar.RESERVATIONS_FILE=root/'reservations.json';ar.ROUTER_LOCK_FILE=root/'router.lock'
ar.workflow_config_for=lambda _: {'nodes':[{'id':'plan','parallel':True}]}
print(ar.choose_agent('wf','plan','fix',reservation_key=sys.argv[2],dispatch_role=sys.argv[2]))
'''
    env={**os.environ,'HERDR_STATE_DB':str(tmp_path/'state.db')}
    workers=[subprocess.Popen([sys.executable,'-c',code,str(tmp_path),role],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for role in ['architect','adversarial']]
    results=[w.communicate(timeout=20) for w in workers]
    assert all(w.returncode==0 for w in workers),results
    assert {out.strip() for out,err in results}=={'claude','codex'}


def test_probe_does_not_restore_concurrently_halted_workflow(routing,monkeypatch):
    def probe(*args,**kwargs):
        current=routing.get_workflow('wf')
        routing.save_workflow({**current,'status':'halted','pause_reason':'operator halt'})
        return [{'agent':'codex','final_status':'READY','request_verified':True,
                 'preflight_identity':{'verifiable':True,'fingerprint':'x'}}]
    monkeypatch.setattr(dp,'inspect',probe)
    dp.refresh_workflow_preflight('wf',['codex'],store=routing)
    current=routing.get_workflow('wf')
    assert current['status']=='halted' and current['pause_reason']=='operator halt'
    assert current['healthy_agents']==['codex']


def test_slow_probe_does_not_block_other_workflow_route_and_release(routing,tmp_path):
    import os,subprocess,sys
    healthy=routing.get_workflow('wf');healthy.update(workflow_id='healthy',preflight_checked_at=time.time(),healthy_agents=['codex'],unhealthy_agents={});routing.save_workflow(healthy)
    script=tmp_path/'probe.py';script.write_text('''import sys,time
from pathlib import Path
from herdr import agent_router as ar,deep_preflight as dp
root=Path(sys.argv[1]); mode=sys.argv[2]
ar.POOLS_FILE=root/'pools.json';ar.RESERVATIONS_FILE=root/'reservations.json';ar.ROUTER_LOCK_FILE=root/'router.lock'
ar.workflow_config_for=lambda _: {'nodes':[{'id':'plan'}]}
if mode=='slow':
 def probe(*a,**kw):
  (root/'started').touch()
  deadline=time.monotonic()+10
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise RuntimeError('release timeout')
   time.sleep(.02)
  return [{'agent':agent,'final_status':'UNKNOWN','request_verified':False} for agent in kw['target_agents']]
 dp.inspect=probe
 try:ar.choose_agent('wf','plan','fix',reservation_key='slow')
 except RuntimeError:pass
else:
 assert ar.choose_agent('healthy','plan','fix',reservation_key='fast')=='codex'
 ar.release_agent_reservation('fast')
 (root/'fast-done').touch()
''')
    env={**os.environ,'HERDR_STATE_DB':str(tmp_path/'state.db'),'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    slow=subprocess.Popen([sys.executable,str(script),str(tmp_path),'slow'],env=env,cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    fast=None
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'started').exists():
            assert time.monotonic()<deadline
            time.sleep(.02)
        fast=subprocess.Popen([sys.executable,str(script),str(tmp_path),'fast'],env=env,cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline=time.monotonic()+2
        while not (tmp_path/'fast-done').exists():
            assert time.monotonic()<deadline,'unrelated healthy route/release blocked by stale probe'
            time.sleep(.02)
    finally:
        (tmp_path/'release').touch()
        for process in [slow,fast]:
            if process:
                out,err=process.communicate(timeout=15)
                assert process.returncode==0,(out,err)


def test_probe_resume_rechecks_role_ownership_and_preserves_new_metadata(routing,monkeypatch):
    def probe(*args,**kwargs):
        record=routing.get_workflow('wf');record['concurrent_note']='preserve';routing.save_workflow(record)
        routing.save_task({'task_id':'architect','workflow_id':'wf','project_id':'p','node':'plan','dispatch_role':'architect','status':'working','agent':'claude'})
        return [{'agent':a,'final_status':'READY','request_verified':True,'preflight_identity':{'verifiable':True,'fingerprint':'x'}} for a in kwargs['target_agents']]
    monkeypatch.setattr(dp,'inspect',probe)
    assert ar.choose_agent('wf','plan','fix',reservation_key='reviewer',dispatch_role='adversarial')=='codex'
    assert routing.get_workflow('wf')['concurrent_note']=='preserve'


def test_identity_drift_during_probe_does_not_authorize_agent(routing,monkeypatch):
    def probe(*args,**kwargs):
        monkeypatch.setattr(dp,'preflight_identity',lambda *a,**kw: {'verifiable':True,'fingerprint':'changed'})
        return [{'agent':a,'final_status':'READY','request_verified':True,'preflight_identity':{'verifiable':True,'fingerprint':'x'}} for a in kwargs['target_agents']]
    monkeypatch.setattr(dp,'inspect',probe)
    with pytest.raises(RuntimeError,match='Preflight|preflight'):
        ar.choose_agent('wf','plan','fix')
    assert not routing.get_workflow('wf')['healthy_agents']


def test_workflow_configuration_drift_during_probe_rejects_writeback(routing,tmp_path,monkeypatch):
    def probe(*args,**kwargs):
        record=routing.get_workflow('wf');record['project_root']=str(tmp_path/'different');routing.save_workflow(record)
        return [{'agent':a,'final_status':'READY','request_verified':True,'preflight_identity':{'verifiable':True,'fingerprint':'x'}} for a in kwargs['target_agents']]
    monkeypatch.setattr(dp,'inspect',probe)
    with pytest.raises(RuntimeError,match='configuration changed'):
        ar.choose_agent('wf','plan','fix')
    assert routing.get_workflow('wf')['unhealthy_agents']=={'claude':'TIMEOUT'}


def test_older_success_cannot_overwrite_newer_auth_failure(routing,monkeypatch):
    import threading
    started=threading.Event();release=threading.Event()
    def inspect(*args,**kwargs):
        if threading.current_thread().name=='older':
            started.set();assert release.wait(5)
            return [{'agent':'codex','final_status':'READY','request_verified':True,'preflight_identity':{'verifiable':True,'fingerprint':'x'}}]
        return [{'agent':'codex','final_status':'AUTH_REQUIRED','request_verified':False,'preflight_identity':{'verifiable':True,'fingerprint':'x'}}]
    monkeypatch.setattr(dp,'inspect',inspect)
    failures=[]
    def old():
        try:dp.refresh_workflow_preflight('wf',['codex'],store=routing)
        except Exception as exc:failures.append(exc)
    worker=threading.Thread(target=old,name='older');worker.start()
    try:
        assert started.wait(5)
        dp.refresh_workflow_preflight('wf',['codex'],store=routing)
        newer=routing.get_workflow('wf')
        assert newer['unhealthy_agents']['codex']=='AUTH_REQUIRED'
    finally:
        release.set();worker.join(5)
    assert not worker.is_alive() and not failures
    record=routing.get_workflow('wf')
    assert record['unhealthy_agents']['codex']=='AUTH_REQUIRED'
    assert 'codex' not in record['healthy_agents']
    assert record['preflight_agent_checked_at']['codex']==newer['preflight_agent_checked_at']['codex']


def test_cross_process_refresh_keeps_newer_auth_failure(routing,tmp_path):
    import os,subprocess,sys
    script=tmp_path/'refresh-race.py';script.write_text('''import sys,time
from pathlib import Path
from herdr import agent_router as ar,deep_preflight as dp
root=Path(sys.argv[1]);mode=sys.argv[2]
ar.ROUTER_LOCK_FILE=root/'router.lock'
dp.preflight_identity=lambda *a,**kw:{'verifiable':True,'fingerprint':'x'}
def probe(*args,**kwargs):
 if mode=='old':
  (root/'old-started').touch()
  end=time.monotonic()+10
  while not (root/'old-release').exists():
   if time.monotonic()>end:raise RuntimeError('release timeout')
   time.sleep(.02)
 return [{'agent':'codex','final_status':'READY' if mode=='old' else 'AUTH_REQUIRED','request_verified':mode=='old','preflight_identity':{'verifiable':True,'fingerprint':'x'}}]
dp.inspect=probe
dp.refresh_workflow_preflight('wf',['codex'])
''')
    env={**os.environ,'HERDR_STATE_DB':str(tmp_path/'state.db'),'PYTHONPATH':str(Path(__file__).resolve().parents[1])}
    old=subprocess.Popen([sys.executable,str(script),str(tmp_path),'old'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'old-started').exists():
            assert time.monotonic()<deadline
            time.sleep(.02)
        new=subprocess.run([sys.executable,str(script),str(tmp_path),'new'],env=env,capture_output=True,text=True,timeout=10)
        assert new.returncode==0,new.stderr
        newer=routing.get_workflow('wf')
        assert newer['unhealthy_agents']['codex']=='AUTH_REQUIRED'
    finally:
        (tmp_path/'old-release').touch();out,err=old.communicate(timeout=10)
        assert old.returncode==0,(out,err)
    record=routing.get_workflow('wf')
    assert record['unhealthy_agents']['codex']=='AUTH_REQUIRED'
    assert 'codex' not in record['healthy_agents']
    assert record['preflight_agent_checked_at']['codex']==newer['preflight_agent_checked_at']['codex']
