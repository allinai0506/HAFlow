"""Execute real entrypoint main functions with loops/transports stopped safely."""
import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).resolve().parents[1]
class StopEntry(Exception): pass

def stop(*args,**kwargs): raise StopEntry()

@pytest.mark.parametrize('relative,service', [('services/herdr-controller.py','com.user.herdr-controller'),('services/herdr-sentinel.py','com.user.herdr-sentinel'),('console/herdr_factory_console.py','com.user.herdr-factory-console')])
def test_main_logs_actual_import_fingerprint_before_work(relative,service,tmp_path,capsys,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','fake-secret-do-not-log')
    path=ROOT/relative
    tree=ast.parse(path.read_text())
    main=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='main')
    namespace={'__file__':str(path),'Path':Path,'json':json,'os':os,'HERDR_ROOT':ROOT,'ROOT':tmp_path,'STATE_FILE':tmp_path/'state.json','TASKS_FILE':str(tmp_path/'tasks.json'),'COORDINATOR_PANE':'unknown','PRODUCT_NAME':'HAFlow','HOST':'localhost','PORT':0,'load_json':lambda *args:{'seen':{},'nudged':{}},'_get_store':stop,'report_integration_gaps':stop,'reset_queued_stage_states':stop,'ThreadingHTTPServer':stop,'Handler':object,'threading':SimpleNamespace(Thread=stop)}
    exec(compile(ast.Module(body=[main],type_ignores=[]),str(path),'exec'),namespace)
    with pytest.raises(StopEntry): namespace['main']()
    output=capsys.readouterr().out
    records=[json.loads(line.split('=',1)[1]) for line in output.splitlines() if line.startswith('HERDR_RUNTIME_FINGERPRINT=')]
    assert len(records)==1
    assert records[0]['service']==service
    assert records[0]['running_import_root']==str(ROOT.resolve())
    assert records[0]['running_sha']=='unknown' # workspace is not release evidence
    assert str(path.resolve()) in records[0]['component_versions']
    assert 'fake-secret-do-not-log' not in output


def test_doctor_reports_configured_snapshot_but_runtime_unknown(tmp_path,capsys,monkeypatch):
    import plistlib
    home=tmp_path/'home'; agents=home/'Library'/'LaunchAgents'; agents.mkdir(parents=True)
    release=home/'releases'/'candidate-release'
    data={'ProgramArguments':['python3',str(release/'services'/'herdr-controller.py')],'EnvironmentVariables':{'HERDR_ROOT':str(release),'OPENAI_API_KEY':'fake-doctor-secret'}}
    (agents/'com.user.herdr-controller.plist').write_bytes(plistlib.dumps(data))
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    path=ROOT/'bin'/'herdr-factory'
    tree=ast.parse(path.read_text()); doctor=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='doctor')
    namespace={'Path':Path,'json':json,'HERDR_ROOT':ROOT,'TASKS_FILE':tmp_path/'tasks.json','POLICIES_FILE':tmp_path/'policies.json','PROJECTS_FILE':tmp_path/'projects.json','WORKFLOWS_FILE':tmp_path/'workflows.json','SERVICE':'service','load_projects':lambda:{'projects':{}},'run':lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout='status: running state = running')}
    exec(compile(ast.Module(body=[doctor],type_ignores=[]),str(path),'exec'),namespace)
    namespace['doctor']()
    output=capsys.readouterr().out
    rows=[json.loads(line.split('=',1)[1]) for line in output.splitlines() if line.startswith('SERVICE_RELEASE_FINGERPRINT=')]
    controller=next(row for row in rows if row['service']=='com.user.herdr-controller')
    assert controller['configured_sha']=='candidate-release'
    assert controller['running_sha']=='unknown'
    assert controller['running_import_root']=='unknown'
    assert 'fake-doctor-secret' not in output
