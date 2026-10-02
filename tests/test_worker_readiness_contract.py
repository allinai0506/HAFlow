import importlib.util
import importlib.machinery
import json
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

ROOT=Path(__file__).resolve().parents[1]

def load(path,name):
    spec=importlib.util.spec_from_loader(name,importlib.machinery.SourceFileLoader(name,str(ROOT/path)))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

@pytest.mark.parametrize('live,text,expected', [({'name':'owned','agent_session':'s','agent_status':'idle'},'Do you trust this workspace?','TRUST_REQUIRED'),({'name':'foreign','agent_session':'other','agent_status':'idle'},'','IDENTITY_MISMATCH'),({'name':'owned','agent_session':'s','agent_status':'starting'},'','UNKNOWN')])
def test_worker_refuses_runtime_before_success_return(live,text,expected):
    worker=load('services/herdr-worker.py','worker_runtime_gate')
    reads=[]
    def transport(argv,timeout=5):
        reads.append(argv)
        if argv[1:3]==['agent','get']: return {'result':{'agent':live}}
        raise AssertionError(argv)
    with patch.object(worker,'run_json',side_effect=transport), patch.object(worker.subprocess,'run',return_value=__import__('subprocess').CompletedProcess([],0,text,'')):
        result=worker.wait_startup_ready('codex','p',{'name':'owned','agent_session':'s'},attempts=1)
    assert result['status']==expected
    if expected=='IDENTITY_MISMATCH': assert all(argv[1]!='pane' for argv in reads)


def test_worker_main_does_not_auto_trust_and_unknown_produces_no_result(tmp_path):
    worker=load('services/herdr-worker.py','worker_main_gate')
    fake={'agent':'claude','name':'owned','agent_session':'s','agent_status':'idle'}
    home=tmp_path/'home'; home.mkdir()
    argv=['worker','--task-id','t','--source',str(tmp_path),'--agent','claude','--execution-mode','context','--pane-id','p']
    with patch.object(worker.Path,'home',return_value=home), patch.object(worker,'CLONE_ROOT',tmp_path/'clones'), patch.object(worker,'verify_request_preflight',return_value={'request_verified':True}), patch.object(worker,'prepare_existing_pane'), patch.object(worker,'start_agent',return_value=fake), patch.object(worker,'wait_startup_ready',return_value={'status':'UNKNOWN','interactive_ready':False}), patch.object(worker,'is_task_active_in_registry',return_value=False), patch.object(sys,'argv',argv):
        with pytest.raises(RuntimeError,match='UNKNOWN'): worker.main()
    assert not (tmp_path/"home"/".claude.json").exists()
    assert (tmp_path/'clones'/'t').exists() # preserve potentially-live instance workspace


def test_factory_persists_request_identity_in_real_store(tmp_path):
    from herdr.state_store import get_state_store
    factory=load('bin/herdr-factory','factory_runtime_identity')
    store=get_state_store(tmp_path/'state.db'); store.save_workflow({'workflow_id':'w','project_id':'p','status':'running'})
    identity={'agent':'codex','fingerprint':'a','verifiable':True}
    row={'agent':'codex','final_status':'READY','request_verified':True,'preflight_identity':identity}
    result=__import__('subprocess').CompletedProcess([],0,json.dumps({'agents':[row]}),'')
    with patch.object(factory,'_get_store',return_value=store), patch.object(factory,'run',return_value=result), patch('herdr.state_store.sync_workflows_projection'):
        factory.run_workflow_preflight({'project_id':'p'},'w')
    assert store.get_workflow('w')['preflight_identities']=={'codex':identity}


@pytest.mark.parametrize('mode,success', [('ready',True), ('trust',False), ('foreign',False), ('unknown',False), ('start_error',False)])
def test_real_worker_cli_native_transport_is_gated(tmp_path,mode,success):
    import os, subprocess
    home=tmp_path/'home'; bindir=home/'.local'/'bin'; bindir.mkdir(parents=True)
    agent=bindir/'codex'
    agent.write_text("#!/usr/bin/env python3\nimport sys\nif '--help' in sys.argv: print('exec')\nelif '--version' in sys.argv: print('fake-safe-cli 1')\nelse: print('HERDR_PREFLIGHT_OK')\n")
    agent.chmod(0o755)
    native=bindir/'herdr'
    native.write_text(r"""#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
with open(os.environ['TEST_NATIVE_CALLS'],'a') as log: log.write(json.dumps(args)+'\n')
mode=os.environ['TEST_NATIVE_MODE']
if mode=='start_error' and args[:2]==['agent','start']:
 print('ambiguous start error',file=sys.stderr); sys.exit(1)
if args[:2]==['pane','read']:
 print('Do you trust this workspace?' if mode=='trust' else '')
else:
 session='other' if mode=='foreign' and args[:2]==['agent','get'] else 'owned-session'
 print(json.dumps({'result':{'agent':{'name':'owned','agent':'codex','agent_session':session,'agent_status':'idle' if mode!='unknown' else 'starting'}}}))
""")
    native.chmod(0o755)
    env=dict(os.environ,HOME=str(home),PATH=str(bindir)+os.pathsep+os.environ['PATH'],HERDR_CLONES_DIR=str(tmp_path/'clones'),HERDR_STATE_DB=str(tmp_path/'state.db'),TEST_NATIVE_CALLS=str(tmp_path/'calls.jsonl'),TEST_NATIVE_MODE=mode,HERDR_RUN_ID='test-run')
    result=subprocess.run([sys.executable,str(ROOT/'services/herdr-worker.py'),'--task-id','test-native','--source',str(tmp_path),'--agent','codex','--execution-mode','context','--pane-id','test-pane'],env=env,text=True,capture_output=True,timeout=12)
    assert (result.returncode==0) is success, result.stderr
    assert ('HERDR_WORKER_RESULT=' in result.stdout) is success
    if not success:
        failures=[json.loads(line.split('=',1)[1]) for line in result.stderr.splitlines() if line.startswith('HERDR_WORKER_FAILURE=')]
        assert len(failures)==1
        failure=failures[0]
        assert failure['task_id']=='test-native'
        assert failure['run_id']=='test-run'
        assert failure['agent_session_id']==('owned-session' if mode!='start_error' else None)
        assert (tmp_path/'clones'/'test-native').exists()
        assert failure['pane_id']=='test-pane'
        assert failure['clone']==str((tmp_path/'clones'/'test-native').resolve())
        assert failure['disposition']=='unknown'
        assert failure['recovery_required'] is True

    calls=[json.loads(line) for line in (tmp_path/'calls.jsonl').read_text().splitlines()]
    assert not any(cmd[:2]==['pane','send-text'] for cmd in calls)
    if mode=='foreign': assert not any(cmd[:2]==['pane','read'] for cmd in calls)
    if success:
        payload=json.loads(next(line.split('=',1)[1] for line in result.stdout.splitlines() if line.startswith('HERDR_WORKER_RESULT=')))
        assert payload['request_verified'] and payload['interactive_ready']
