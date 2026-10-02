"""A real execution receipt must not depend on terminal rendering or cross runs."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

import pytest
from herdr.state_store import SQLiteStateStore

ROOT = Path(__file__).resolve().parents[1]


def api():
    assert importlib.util.find_spec('herdr.completion_receipt') is not None, 'structured completion gateway is missing'
    return importlib.import_module('herdr.completion_receipt')


@pytest.fixture
def execution(tmp_path):
    store = SQLiteStateStore(tmp_path/'state.db')
    store.save_workflow({'workflow_id':'wf-receipt','status':'running'})
    store.save_task({'task_id':'work-a','workflow_id':'wf-receipt','node':'implementation',
                     'run_id':'run-a','status':'working','started_at':time.time()-100,
                     'clone_path':str(tmp_path/'clone'),'agent':'opencode'})
    return store


def test_cli_receipt_is_consumable_without_terminal_signal(execution, tmp_path):
    m=api(); identity=m.issue_completion_contract('work-a',execution)
    env={**os.environ,'HERDR_STATE_DB':str(execution.db_path),'TASKS_FILE':str(tmp_path/'tasks.json'),
         'WORKFLOWS_FILE':str(tmp_path/'workflows.json'),'HERDR_CONTROLLER_DIR':str(tmp_path)}
    result=subprocess.run([str(ROOT/'bin/herdr-task'),'report-completion','work-a',
                           '--identity-file',identity['path']],env=env,text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    receipt=json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt['task_id']=='work-a' and receipt['run_id']=='run-a'
    assert execution.get_task('work-a')['status']=='working'
    accepted=m.consume_completion_receipt('work-a',execution)
    assert accepted['accepted'] is True
    assert execution.get_task('work-a')['status']=='agent_done'
    assert execution.get_task('work-a').get('stage_verdict') not in ('pass','blocked')
    assert m.consume_completion_receipt('work-a',execution)['accepted'] is False


@pytest.mark.parametrize('field,value',[('run_id','run-other'),('epoch','old-epoch'),('token','forged-token')])
def test_wrong_identity_never_changes_state(execution,field,value):
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    identity=json.loads(Path(issued['path']).read_text());identity[field]=value
    with pytest.raises(ValueError):m.report_completion('work-a',identity,[],execution)
    assert execution.get_task('work-a')['status']=='working'


def test_duplicate_receipt_returns_same_authoritative_id(execution):
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    identity=json.loads(Path(issued['path']).read_text())
    a=m.report_completion('work-a',identity,[],execution)
    b=m.report_completion('work-a',identity,[],execution)
    assert a['receipt_id']==b['receipt_id']
    m.consume_completion_receipt('work-a',execution)
    assert m.report_completion('work-a',identity,[],execution)['receipt_id']==a['receipt_id']


def test_floor_and_new_epoch_reject_old_completion(execution):
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    identity=json.loads(Path(issued['path']).read_text())
    execution.update_task_metadata('work-a',{'started_at':time.time()})
    m.report_completion('work-a',identity,[],execution)
    assert m.consume_completion_receipt('work-a',execution)['accepted'] is False
    m.issue_completion_contract('work-a',execution)
    with pytest.raises(ValueError):m.report_completion('work-a',identity,[],execution)
    assert execution.get_task('work-a')['status']=='working'


def test_controller_poll_consumes_receipts_and_ignores_legacy_samples(execution, monkeypatch):
    spec=importlib.util.spec_from_file_location('receipt_controller',ROOT/'services/herdr-controller.py')
    controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    m.report_completion('work-a',json.loads(Path(issued['path']).read_text()),[],execution)
    monkeypatch.setattr(controller,'_get_store',lambda:execution)
    monkeypatch.setattr(controller,'load_tasks',lambda:[execution.get_task('work-a')])
    assert hasattr(controller,'process_structured_completions')
    assert controller.process_structured_completions()==1
    assert execution.get_task('work-a')['status']=='agent_done'
    assert controller.process_completion_observation(execution.get_task('work-a')) is False


def test_rework_invalidates_pending_receipt_even_after_resume(execution):
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    identity=json.loads(Path(issued['path']).read_text())
    m.report_completion('work-a',identity,[],execution)
    execution.transition_task('work-a','rework',reason='review_feedback',force=True)
    execution.transition_task('work-a','working',reason='retry',force=True)
    assert m.consume_completion_receipt('work-a',execution)['accepted'] is False
    with pytest.raises(ValueError): m.report_completion('work-a',identity,[],execution)


def test_closed_workflow_rejects_pending_completion(execution):
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    identity=json.loads(Path(issued['path']).read_text())
    m.report_completion('work-a',identity,[],execution)
    execution.save_workflow({**execution.get_workflow('wf-receipt'),'status':'completed'})
    assert m.consume_completion_receipt('work-a',execution)['accepted'] is False
    with pytest.raises(ValueError): m.report_completion('work-a',identity,[],execution)


def test_new_rework_contract_can_complete_same_task(execution):
    m=api(); m.issue_completion_contract('work-a',execution)
    execution.transition_task('work-a','rework',reason='review_feedback',force=True)
    fresh=m.issue_completion_contract('work-a',execution)
    m.report_completion('work-a',json.loads(Path(fresh['path']).read_text()),[],execution)
    assert m.consume_completion_receipt('work-a',execution,now=time.time()+61)['accepted'] is True


def test_completion_requires_current_registered_checkpoint_hash(execution):
    from herdr.task_checkpoint import publish_task_checkpoint
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    identity=json.loads(Path(issued['path']).read_text())
    checkpoint=publish_task_checkpoint('work-a','run-a',identity['epoch'],'segment',1,store=execution)
    receipt=m.report_completion('work-a',identity,[{'observation_id':checkpoint['observation_id'],'sha256':checkpoint['sha256']}],execution)
    assert receipt['artifacts'][0]['sha256']==checkpoint['sha256']


def test_collaboration_transport_does_not_rotate_task_contract(monkeypatch):
    spec=importlib.util.spec_from_file_location('receipt_collab',ROOT/'services/herdr-controller.py')
    controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    from types import SimpleNamespace
    monkeypatch.setattr(controller.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='',stderr=''))
    assert controller._default_collab_sender('pane-a','collaborate')=={'ok':True}


def _consume_process(db, barrier, results):
    from herdr.completion_receipt import consume_completion_receipt
    store=SQLiteStateStore(db)
    barrier.wait(timeout=10)
    results.put(consume_completion_receipt('work-a',store)['accepted'])


def test_independent_process_consumers_accept_exactly_once(execution):
    import multiprocessing
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    m.report_completion('work-a',json.loads(Path(issued['path']).read_text()),[],execution)
    context=multiprocessing.get_context('spawn')
    barrier=context.Barrier(2); results=context.Queue()
    processes=[context.Process(target=_consume_process,args=(execution.db_path,barrier,results)) for _ in range(2)]
    for process in processes:process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode==0
    assert sorted([results.get(timeout=2),results.get(timeout=2)])==[False,True]
    assert len(execution.list_events(task_id='work-a',event_type='task_transition'))==1


def test_private_credential_and_restart_recovery(execution):
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    assert Path(issued['path']).stat().st_mode & 0o777 == 0o600
    assert Path(issued['path']).parent.stat().st_mode & 0o777 == 0o700
    m.report_completion('work-a',json.loads(Path(issued['path']).read_text()),[],execution)
    reopened=SQLiteStateStore(execution.db_path)
    assert m.pending_completion_task_ids(reopened)==['work-a']
    assert m.consume_completion_receipt('work-a',reopened)['accepted'] is True


def test_real_cli_rework_rotates_contract_clears_verdict_and_restarts_floor(execution,monkeypatch):
    from importlib.machinery import SourceFileLoader
    from types import SimpleNamespace
    from herdr import task_resources
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    execution.save_task({**execution.get_task('work-a'),'stage_verdict':'blocked','pane_id':'pane-a'})
    loader=SourceFileLoader('receipt_rework_cli',str(ROOT/'bin/herdr-task'))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    cli=importlib.util.module_from_spec(spec);loader.exec_module(cli)
    monkeypatch.setattr(cli,'_get_store',lambda:execution)
    monkeypatch.setattr(task_resources,'owned_live_pane',lambda task:(True,'owned'))
    prompts=[]
    monkeypatch.setattr(cli,'_herdr',lambda *args:prompts.append(args[-1]) or SimpleNamespace(returncode=0))
    cli.cmd_rework(SimpleNamespace(task_id='work-a',request_id='review-1',prompt='fix',reason='review'))
    current=execution.get_task('work-a')
    assert current.get('stage_verdict') is None
    assert current['completion_epoch']!=issued['epoch']
    assert '--identity-file' in prompts[0]
    import shlex
    identity_path=shlex.split(prompts[0].split('--identity-file ',1)[1].splitlines()[0])[0]
    m.report_completion('work-a',json.loads(Path(identity_path).read_text()),[],execution)
    assert m.consume_completion_receipt('work-a',execution)['accepted'] is False
    assert m.consume_completion_receipt('work-a',execution,now=time.time()+61)['accepted'] is True


@pytest.mark.parametrize('started',[None,'broken',float('nan'),float('inf'),-1])
def test_missing_or_invalid_start_never_bypasses_floor(execution,started):
    execution.save_task({'task_id':'fresh','workflow_id':'wf-receipt','run_id':'fresh-run',
                         'status':'working','started_at':time.time(),'created_at':time.time()})
    execution.update_task_metadata('fresh',{'started_at':started})
    m=api(); issued=m.issue_completion_contract('fresh',execution)
    m.report_completion('fresh',json.loads(Path(issued['path']).read_text()),[],execution)
    assert m.consume_completion_receipt('fresh',execution)['accepted'] is False


def test_tampered_checkpoint_is_quarantined_without_advancing_task(execution):
    from herdr.task_checkpoint import publish_task_checkpoint
    from herdr.observation import ObservationStore
    m=api(); issued=m.issue_completion_contract('work-a',execution)
    identity=json.loads(Path(issued['path']).read_text())
    checkpoint=publish_task_checkpoint('work-a','run-a',identity['epoch'],'segment',1,store=execution)
    m.report_completion('work-a',identity,[{'observation_id':checkpoint['observation_id'],'sha256':checkpoint['sha256']}],execution)
    artifact = Path(ObservationStore(execution.db_path).get(checkpoint['observation_id']).content_ref)
    artifact.chmod(0o600)
    artifact.write_text('tampered')
    assert m.consume_completion_receipt('work-a',execution)['accepted'] is False
    assert m.pending_completion_task_ids(execution)==[]
    assert execution.get_task('work-a')['status']=='working'
    assert len(execution.list_events(task_id='work-a',event_type='completion_receipt_rejected'))==1


def test_receipt_cannot_supersede_pending_verification(execution):
    m=api()
    execution.transition_task('work-a','rework',reason='verify',force=True)
    intervention=execution.create_intervention({'run_id':'run-a','task_id':'work-a','workflow_id':'wf-receipt',
        'decision_id':'verify-decision','action':'VERIFY','requested_at':time.time()})
    issued=m.issue_completion_contract('work-a',execution)
    m.report_completion('work-a',json.loads(Path(issued['path']).read_text()),[],execution)
    execution.record_event('verification_dispatched',{'intervention_id':intervention['intervention_id']},
        workflow_id='wf-receipt',task_id='work-a',run_id='run-a',source='supervisor')
    assert m.consume_completion_receipt('work-a',execution,now=time.time()+61)['accepted'] is False
    assert execution.get_task('work-a')['status']=='rework'
    execution.record_event('verification_completed',{'verification':{'passed':False,'intervention_id':intervention['intervention_id']}},
        workflow_id='wf-receipt',task_id='work-a',run_id='run-a',source='trajectory')
    assert m.consume_completion_receipt('work-a',execution,now=time.time()+61)['accepted'] is True
