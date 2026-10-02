import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from herdr.state_store import SQLiteStateStore
from herdr.completion_receipt import issue_completion_contract
from herdr.evaluator import init_loop
from herdr.delivery_report import build_delivery_report

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('failed',[False,True])
@pytest.mark.parametrize('changed',[False,True])
def test_real_loop_cli_to_controller_to_report(tmp_path,monkeypatch,failed,changed):
    clone=tmp_path/'clone';clone.mkdir()
    subprocess.run(['git','init','-q',str(clone)],check=True)
    subprocess.run(['git','-C',str(clone),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','--allow-empty','-qm','base'],check=True)
    candidate=subprocess.check_output(['git','-C',str(clone),'rev-parse','HEAD'],text=True).strip()
    change = 'printf new > business.txt; ' if changed else ''
    init_loop(clone,'test',test_cmd=change + ("printf '1 failed, 1 passed, 2 skipped in 0.01s\\n'; exit 1" if failed else "printf '2 passed, 2 skipped in 0.01s\\n'"),lint_cmd='true')
    worker_spec=importlib.util.spec_from_file_location('evaluation_chain_worker',ROOT/'services/herdr-worker.py')
    worker=importlib.util.module_from_spec(worker_spec); worker_spec.loader.exec_module(worker)
    worker.write_task_context(clone,'codex','task/chain',mode='context')
    from herdr.task_resources import write_worker_launch_identity
    write_worker_launch_identity(clone,{'task_id':'test-chain','run_id':'run-chain','intent_id':'launch-chain'},initial=True)
    assert worker.build_baseline_fingerprint(clone)['untracked'] == {}
    store=SQLiteStateStore(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf-chain','status':'running','candidate_sha':candidate})
    store.save_task({'task_id':'test-chain','workflow_id':'wf-chain','run_id':'run-chain','status':'working',
        'clone_path':str(clone),'candidate_sha':candidate,'verified_candidate_sha':candidate})
    identity=issue_completion_contract('test-chain',store)
    result=subprocess.run([str(ROOT/'bin/herdr-task'),'tool-run','--task-id','test-chain','--run-id','run-chain','--epoch',identity['epoch'],
        '--',str(ROOT/'bin/herdr-loop'),'eval','--dir',str(clone)],env={**os.environ,'HOME':str(tmp_path),'HERDR_STATE_DB':str(store.db_path),
        'TASKS_FILE':str(tmp_path/'tasks.json'),'WORKFLOWS_FILE':str(tmp_path/'workflows.json')},capture_output=True,text=True,timeout=20)
    assert result.returncode==(1 if failed else 0),result.stdout+result.stderr
    snapshot=json.loads((clone/'.herdr-loop/EVAL_DONE.json').read_text())
    assert snapshot.get('candidate_sha')==(None if changed else candidate)
    assert snapshot.get('epoch')==identity['epoch']
    spec=importlib.util.spec_from_file_location('eval_chain_controller',ROOT/'services/herdr-controller.py')
    controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    from herdr.supervisor import config
    monkeypatch.setattr(config,'supervisor_enabled',lambda cfg:True)
    monkeypatch.setattr(controller.supervisor_harness,'get_supervisor',lambda cfg:None)
    monkeypatch.setattr(controller,'supervisor_checkpoint',lambda *a,**k:None)
    controller.check_task_tests_completed(store.get_task('test-chain'),store=store)
    fact=build_delivery_report('wf-chain',store)['tasks'][0]['verification']
    assert fact['status']==('unknown' if changed else 'fail' if failed else 'pass')
    assert fact['observed_result']==('fail' if failed else 'pass')
    assert fact['counts']['skip']==2
    assert fact['exit_code']==(1 if failed else 0)
    assert fact['event_id']


def test_tool_child_has_binding_without_database_authority(tmp_path):
    store = SQLiteStateStore(tmp_path/'private.db')
    store.save_task({'task_id':'tool','workflow_id':'wf','run_id':'run','completion_epoch':'epoch','status':'working'})
    default = SQLiteStateStore(tmp_path/'.herdr-controller/state.db')
    default.save_task({'task_id':'victim','workflow_id':'other','run_id':'unrelated','status':'working'})
    code = "import os,json; from herdr.state_store import get_state_store; get_state_store().delete_task('victim'); print(json.dumps({k:os.environ.get(k) for k in ['HERDR_STATE_DB','TASKS_FILE','WORKFLOWS_FILE','HERDR_TASK_ID','HERDR_RUN_ID','HERDR_COMPLETION_EPOCH']}))"
    result = subprocess.run([str(ROOT/'bin/herdr-task'),'tool-run','--task-id','tool','--run-id','run','--epoch','epoch','--',sys.executable,'-c',code],
        env={**os.environ,'HOME':str(tmp_path),'HERDR_STATE_DB':str(store.db_path),'TASKS_FILE':'private-tasks','WORKFLOWS_FILE':'private-workflows'},
        capture_output=True,text=True,timeout=10)
    assert result.returncode == 0,result.stderr
    child=json.loads(json.loads(result.stdout)['stdout'])
    assert default.get_task('victim') is not None
    assert child['HERDR_STATE_DB'] is None
    assert child['TASKS_FILE'] is None and child['WORKFLOWS_FILE'] is None
    assert child['HERDR_TASK_ID']=='tool' and child['HERDR_RUN_ID']=='run' and child['HERDR_COMPLETION_EPOCH']=='epoch'


def test_autosave_preserves_business_without_launch_identity(tmp_path):
    import importlib.machinery
    clone=tmp_path/'clone';clone.mkdir()
    subprocess.run(['git','init','-q',str(clone)],check=True)
    for key,value in [('user.name','Test'),('user.email','test@example.invalid')]:
        subprocess.run(['git','-C',str(clone),'config',key,value],check=True)
    subprocess.run(['git','-C',str(clone),'commit','--allow-empty','-qm','base'],check=True)
    (clone/'.herdr-launch-identity.json').write_text('private-runtime-identity')
    (clone/'.agent-task-context').write_text('internal-context')
    (clone/'business.txt').write_text('deliverable')
    loader=importlib.machinery.SourceFileLoader('internal_autosave_cli',str(ROOT/'bin/herdr-task'))
    spec=importlib.util.spec_from_loader(loader.name,loader); module=importlib.util.module_from_spec(spec);loader.exec_module(module)
    assert module._autosave_clone_wip({'task_id':'task','clone_path':str(clone),'branch':'task/local'})
    paths=subprocess.check_output(['git','-C',str(clone),'ls-tree','--name-only','HEAD'],text=True).splitlines()
    assert paths == ['business.txt']
