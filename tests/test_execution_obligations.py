"""Trusted-fact fixtures test the policy; they are not real browser integration."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from herdr import state_db
from herdr.execution_obligations import freeze_contract, verify_executions
from herdr.ego_execution_evidence import observe_task, native_ego_facts
from herdr.observation import ObservationStore
from herdr.trajectory import TrajectoryLedger

DECL = [{'tool': 'ego-browser', 'source': 'native-session-v1'}]
TASK = dict(task_id='test-1', workflow_id='wf', run_id='run-1', node='cleanroom_test',
            candidate_sha='a' * 40, agent='codex', created_at=100, started_at=100)


def contract():
    return freeze_contract(TASK, DECL)


def good_fact():
    # Injected verified event boundary; production native adapter cannot yet
    # establish the browser/runtime/readback semantics represented here.
    return dict(workflow_id='wf', task_id='test-1', run_id='run-1', candidate_sha='a'*40,
                tool='ego-browser', source='native-session-v1', invoked=True,
                outcome='success', evidence_verified=True, evidence_refs=['obs-proof'],
                browser_actions_verified=True, runtime_candidate_verified=True,
                e2e_verified=True, state_readback_verified=True)


def test_trusted_complete_facts_satisfy_pure_contract():
    before = copy.deepcopy(contract())
    assert verify_executions(before, [good_fact()])['status'] == 'satisfied'
    assert before == contract()


@pytest.mark.parametrize('field,value', [('run_id','old'), ('task_id','other'),
    ('candidate_sha','b'*40), ('workflow_id','other'), ('source','agent-report')])
def test_cross_identity_and_untrusted_facts_never_satisfy(field, value):
    fact = good_fact(); fact[field] = value
    assert verify_executions(contract(), [fact])['status'] != 'satisfied'


def test_missing_self_report_failure_and_unavailable():
    assert verify_executions(contract(), [])['status'] == 'missing'
    assert verify_executions(contract(), [{'summary':'used ego-browser successfully'}])['status'] == 'missing'
    fact = good_fact(); fact['outcome'] = 'failed'
    assert verify_executions(contract(), [fact])['status'] == 'failed'
    assert verify_executions(contract(), [], available=False)['status'] == 'unverifiable'


@pytest.mark.parametrize('field', ['evidence_verified','browser_actions_verified',
    'runtime_candidate_verified','e2e_verified','state_readback_verified'])
def test_incomplete_or_blank_evidence_is_unverifiable(field):
    fact = good_fact(); fact[field] = False
    assert verify_executions(contract(), [fact])['status'] == 'unverifiable'


def action(command='ego-browser nodejs -e "console.log(1)"', code=0):
    return dict(command=command, exit_code=code, timestamp=101, source_ref='codex:session:1',
                session_id='session', tool='exec_command', is_error=False,
                ego_native_receipt={'provider':'codex','outcome':'success' if code==0 else 'failed' if type(code) is int else 'unknown'})


def test_native_call_only_proves_invocation_not_e2e():
    facts = native_ego_facts(TASK, [action()])
    assert facts[0]['invoked'] is True
    assert verify_executions(contract(), facts)['status'] == 'unverifiable'
    assert verify_executions(contract(), native_ego_facts(TASK, [action(code=1)]))['status'] == 'failed'


@pytest.mark.parametrize('command', ['cat /skills/ego-browser/SKILL.md', 'ego-browser skill',
    'echo ego-browser nodejs', 'false; ego-browser nodejs -e x',
    'ego-browser nodejs -e x || true', 'python spoof.py', 'env ego-browser nodejs -e x'])
def test_reads_text_and_shell_wrappers_not_invocation_proof(command):
    assert native_ego_facts(TASK, [action(command)]) == []


def test_old_or_unknown_time_not_rebound_to_current_run():
    for timestamp in (99, None):
        a = action(); a['timestamp'] = timestamp
        assert native_ego_facts(TASK, [a]) == []


def setup_db(tmp_path, declarations=DECL):
    db = tmp_path/'state.db'
    state_db.init_db(db)
    config = {'nodes': [{'id': 'cleanroom_test', 'required_executions': declarations}]}
    state_db.save_workflow(dict(workflow_id='wf', config=config), db_path=db)
    state_db.save_task({**TASK, 'status':'working'}, db_path=db)
    return db


def test_sqlite_freeze_survives_workflow_and_task_edits(tmp_path):
    db = setup_db(tmp_path)
    task = state_db.get_task('test-1', db_path=db)
    frozen = task['execution_contract_v1']
    state_db.save_workflow(dict(workflow_id='wf', config={'nodes':[]}), db_path=db)
    task['execution_contract_v1'] = {'fake':True}
    state_db.save_task(task, db_path=db)
    assert state_db.get_task('test-1', db_path=db)['execution_contract_v1'] == frozen
    assert frozen['requirements'] == DECL


def test_old_workflows_remain_disabled_even_if_template_changes(tmp_path):
    db = setup_db(tmp_path, [])
    task = state_db.get_task('test-1', db_path=db)
    assert not task.get('execution_contract_v1')
    assert observe_task(task, db, loader=Mock(side_effect=AssertionError('must not load'))) is None
    state_db.save_workflow(dict(workflow_id='wf', config={'nodes':[{'id':'cleanroom_test','required_executions':DECL}]}), db_path=db)
    state_db.save_task({**TASK, 'task_id':'new', 'run_id':'new-run'}, db_path=db)
    assert not state_db.get_task('new', db_path=db).get('execution_contract_v1')


def test_real_sqlite_observation_ledger_chain_and_no_state_mutation(tmp_path):
    db = setup_db(tmp_path)
    task = state_db.get_task('test-1', db_path=db)
    result = observe_task(task, db, loader=lambda *a, **k: ([action()], []))
    assert result['status'] == 'unverifiable'
    assert state_db.get_task('test-1', db_path=db) == task
    events = TrajectoryLedger(db).list_events('run-1')
    event = next(e for e in events if e['event_type']=='execution_obligations_checked')
    obs_id = event['metadata']['observation_id']
    assert ObservationStore(db).verify(obs_id)['valid']
    body = json.loads(ObservationStore(db).read(obs_id)['content'])
    assert body['facts'][0]['invoked'] is True
    assert body['result']['status'] == 'unverifiable'


def test_loader_unavailable_records_unverifiable(tmp_path):
    db = setup_db(tmp_path)
    task = state_db.get_task('test-1', db_path=db)
    assert observe_task(task, db, loader=Mock(side_effect=OSError('unavailable')))['status']=='unverifiable'


def test_bad_configuration_is_observed_not_a_launch_failure(tmp_path):
    db = setup_db(tmp_path, [{'tool':'other-tool'}])
    task = state_db.get_task('test-1', db_path=db)
    assert observe_task(task, db, loader=lambda *a, **k: ([], []))['status']=='unverifiable'


@pytest.mark.parametrize('status', ['missing','failed','unverifiable','satisfied'])
def test_observation_never_changes_auto_verdict_or_retries(monkeypatch, status):
    from services.controller.auto_verdict import try_auto_verdict
    monkeypatch.setattr('herdr.ego_execution_evidence.submit_ego_observation', Mock(return_value={'status':status}))
    run = Mock(return_value=SimpleNamespace(returncode=0))
    ports = SimpleNamespace(auto_verdict_enabled=lambda: True,
        get_task=lambda _: {**TASK,'status':'agent_done','execution_contract_v1':contract()},
        node_is_gate=lambda *_: True, read_gate_verdict=lambda _: ('pass','clean','file'),
        _get_store=lambda: SimpleNamespace(db_path='isolated.db'),
        subprocess=SimpleNamespace(run=run), TASK_MANAGER='herdr-task')
    assert try_auto_verdict(ports, 'test-1') is True
    assert run.call_args.args[0] == ['herdr-task','set','test-1','completed','--verdict','pass','--note','clean']


def test_observation_exception_never_prevents_verdict(monkeypatch):
    from services.controller.auto_verdict import try_auto_verdict
    monkeypatch.setattr('herdr.ego_execution_evidence.submit_ego_observation', Mock(side_effect=RuntimeError('broken')))
    run = Mock(return_value=SimpleNamespace(returncode=0))
    ports = SimpleNamespace(auto_verdict_enabled=lambda: True,
        get_task=lambda _: {**TASK,'status':'agent_done','execution_contract_v1':contract()},
        node_is_gate=lambda *_: True, read_gate_verdict=lambda _: ('pass','','file'),
        _get_store=lambda: SimpleNamespace(db_path='isolated.db'),
        subprocess=SimpleNamespace(run=run), TASK_MANAGER='herdr-task')
    assert try_auto_verdict(ports,'test-1')


def test_metadata_api_cannot_replace_contract(tmp_path):
    db = setup_db(tmp_path)
    before = state_db.get_task('test-1', db_path=db)['execution_contract_v1']
    with pytest.raises(ValueError):
        state_db.update_task_metadata('test-1', {'execution_contract_v1': {'fake':True}}, db_path=db)
    assert state_db.get_task('test-1', db_path=db)['execution_contract_v1'] == before


def test_native_parser_entry_uses_real_event_receipts_not_agent_messages():
    from herdr.native_session_trace import parse_codex_jsonl
    records = [
        {'type':'response_item','timestamp':'1970-01-01T00:01:41Z', 'payload':{
            'type':'function_call','name':'exec_command','call_id':'call1',
            'arguments':json.dumps({'cmd':'ego-browser nodejs -e "console.log(1)"'})}},
        {'type':'response_item','payload':{'type':'function_call_output','call_id':'call1',
            'output':json.dumps({'exit_code':0,'output':'ok'})}},
        {'type':'response_item','payload':{'type':'message','role':'assistant',
            'content':[{'type':'output_text','text':'ego-browser E2E passed'}]}}]
    actions, notes = parse_codex_jsonl('\n'.join(json.dumps(r) for r in records),session_id='session')
    facts = native_ego_facts(TASK, actions)
    assert len(facts) == 1
    assert verify_executions(contract(), facts)['status']=='unverifiable'


def test_source_unavailable_never_satisfies_even_with_complete_cached_facts():
    assert verify_executions(contract(), [good_fact()], available=False)['status']=='unverifiable'


def test_unreceipted_native_command_is_not_an_invocation():
    unreceipted = action(code=None)
    unreceipted.pop('ego_native_receipt')
    assert native_ego_facts(TASK, [unreceipted]) == []


def test_repeated_observation_reuses_evidence_and_tampering_is_visible(tmp_path):
    db = setup_db(tmp_path)
    task = state_db.get_task('test-1', db_path=db)
    loader = lambda *a, **k: ([action()], [])
    observe_task(task, db, loader=loader)
    observe_task(task, db, loader=loader)
    events = TrajectoryLedger(db).list_events('run-1',event_type='execution_obligations_checked')
    assert len(events)==1
    store = ObservationStore(db)
    obs = store.get(events[0]['metadata']['observation_id'])
    from pathlib import Path
    Path(obs.content_ref).write_text('{}')
    assert store.verify(obs.observation_id)['valid'] is False


def test_real_cli_to_sqlite_to_observation_and_trajectory(tmp_path):
    import subprocess
    import sys
    db = setup_db(tmp_path)
    before = state_db.get_task('test-1',db_path=db)
    result = subprocess.run([sys.executable,'-m','herdr.ego_execution_evidence',
                             '--db',str(db),'--task','test-1'],capture_output=True,text=True,timeout=15)
    assert result.returncode==0, result.stderr
    assert json.loads(result.stdout)['status']=='unverifiable'
    assert state_db.get_task('test-1',db_path=db)==before
    event = TrajectoryLedger(db).list_events('run-1',event_type='execution_obligations_checked')[0]
    assert ObservationStore(db).verify(event['metadata']['observation_id'])['valid']


def test_template_normalization_retains_optional_execution_declaration(tmp_path):
    from pathlib import Path
    from herdr.workflow import load_template, normalize_workflow
    template = load_template(str(Path('workflow_templates/code-craft-v1.yaml').resolve()))
    node = next(n for n in template['nodes'] if n['id']=='cleanroom_test')
    assert node['required_executions']==DECL
    db = tmp_path/'normalized.db'
    state_db.init_db(db)
    state_db.save_workflow({'workflow_id':'wf','config':template},db_path=db)
    state_db.save_task(TASK,db_path=db)
    assert state_db.get_task('test-1',db_path=db)['execution_contract_v1']['requirements']==DECL
    assert 'required_executions' not in normalize_workflow({'nodes':[{'id':'old'}]})['nodes'][0]


def test_native_session_file_to_sqlite_rejects_foreign_run(tmp_path):
    """Synthetic native event input, real file discovery/ownership/store chain."""
    from pathlib import Path
    import re
    clone = tmp_path/'clone'; clone.mkdir()
    root = Path.home()/'.claude/projects'/re.sub('[^A-Za-z0-9]','-',str(clone))
    root.mkdir(parents=True)
    session = 'native-session'
    rows = [dict(cwd=str(clone),sessionId=session,timestamp='1970-01-01T00:01:41Z',
        message={'content':[{'type':'tool_use','id':'c1','name':'Bash','input':{
            'command':'ego-browser nodejs -e "console.log(1)"'}}]}),
        dict(sessionId=session,message={'content':[{'type':'tool_result','tool_use_id':'c1',
            'exit_code':0,'content':'not an E2E proof'}]})]
    (root/(session+'.jsonl')).write_text('\n'.join(json.dumps(r) for r in rows))
    db = setup_db(tmp_path)
    task = state_db.get_task('test-1',db_path=db)
    task.update(agent='claude',clone_path=str(clone))
    state_db.save_task(task,db_path=db)
    result = observe_task(task,db)
    assert result['status']=='unverifiable'
    assert result['evidence_refs']==['claude:native-session:1']
    (clone/'.herdr-launch-identity.json').write_text(json.dumps({
        'task_id':'test-1','run_id':'old-run','native_session_id':session}))
    result = observe_task(task,db)
    assert result['status']=='unverifiable'
    assert not result['evidence_refs']


def test_concurrent_workflow_upserts_cannot_replace_first_declaration(tmp_path):
    import concurrent.futures
    import threading
    db = tmp_path/'race.db'; state_db.init_db(db)
    barrier = threading.Barrier(2)
    declarations = [DECL, [{'tool':'unsupported','source':'native-session-v1'}]]
    def save(declaration):
        barrier.wait(timeout=5)
        state_db.save_workflow({'workflow_id':'wf','config':{'nodes':[{
            'id':'cleanroom_test','required_executions':declaration}]}},db_path=db)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save,declarations))
    frozen = state_db.get_workflow('wf',db_path=db)['execution_declarations_v1']
    assert frozen['cleanroom_test'] in declarations
    state_db.save_workflow({'workflow_id':'wf','config':{'nodes':[]}},db_path=db)
    assert state_db.get_workflow('wf',db_path=db)['execution_declarations_v1']==frozen


def test_mutating_candidate_after_registration_never_reuses_contract(tmp_path):
    db = setup_db(tmp_path)
    task = state_db.get_task('test-1',db_path=db)
    task['candidate_sha']='b'*40
    state_db.save_task(task,db_path=db)
    result = observe_task(task,db,loader=lambda *a,**k: ([action()],[]))
    assert result['status']=='unverifiable'
    assert result['reason']=='contract_identity_changed'


def test_codex_unexecuted_wrapper_literal_is_not_invocation():
    from herdr.native_session_trace import parse_codex_jsonl
    rows = [dict(type='response_item',timestamp=101,payload=dict(
        type='custom_tool_call', name='exec',call_id='c',input=(
            'if(false){ await tools.exec_command({cmd:"ego-browser nodejs -e x"}); } text({exit_code:0});'))),
        dict(type='response_item',payload=dict(type='custom_tool_call_output',call_id='c',
                                             output='Script completed\n{"exit_code":0}'))]
    actions,_ = parse_codex_jsonl('\n'.join(json.dumps(r) for r in rows),session_id='session')
    facts = native_ego_facts(TASK,actions)
    assert not any(f['invoked'] for f in facts)
    assert verify_executions(contract(),facts)['status']=='unverifiable'


def test_read_or_unknown_tools_cannot_become_ego_invocations():
    from herdr.native_session_trace import parse_claude_jsonl
    for tool in ('Read','unknown'):
        rows = [dict(message={'content':[{'type':'tool_use','id':'c','name':tool,
            'input':{'command':'ego-browser nodejs -e x'}}]},timestamp=101),
            dict(message={'content':[{'type':'tool_result','tool_use_id':'c','exit_code':0,'content':'ok'}]})]
        actions,_ = parse_claude_jsonl('\n'.join(json.dumps(r) for r in rows),session_id='session')
        assert not any(f['invoked'] for f in native_ego_facts(TASK,actions))


def test_quoted_javascript_punctuation_is_a_direct_invocation():
    command = "ego-browser nodejs -e 'const t=await taskSpace(1); console.log(await t.pages());'"
    assert native_ego_facts(TASK,[action(command)])[0]['invoked']


def test_stdout_exit_code_cannot_override_native_receipt():
    from herdr.native_session_trace import parse_codex_jsonl
    rows = [dict(type='response_item',timestamp=101,payload=dict(type='function_call',
        name='exec_command',call_id='c',arguments=json.dumps({'cmd':'ego-browser nodejs -e x'}))),
        dict(type='response_item',payload=dict(type='function_call_output',call_id='c',
            output=json.dumps({'exit_code':7,'output':'{"exit_code":0}'})))]
    actions,_ = parse_codex_jsonl('\n'.join(json.dumps(r) for r in rows),session_id='session')
    facts=native_ego_facts(TASK,actions)
    assert facts[0]['outcome']=='failed'


def test_observation_dispatch_is_nonblocking_and_bounded(tmp_path):
    import threading
    from herdr.ego_execution_evidence import submit_ego_observation
    from herdr.observer.harness import ObservationScheduler
    entered,release=threading.Event(),threading.Event()
    calls=[]
    def observe(run_id,**kwargs):
        calls.append(run_id); entered.set()
        assert release.wait(timeout=5)
    scheduler=ObservationScheduler(observe=observe,config={'enabled':True,'interval':0},max_concurrent=1)
    task={**TASK,'execution_contract_v1':contract()}
    try:
        assert submit_ego_observation(task,tmp_path/'state.db',scheduler=scheduler)
        assert entered.wait(timeout=2)
        assert not submit_ego_observation({**task,'run_id':'another'},tmp_path/'state.db',scheduler=scheduler)
        assert len(calls)==1
    finally:
        release.set();scheduler.drain(timeout=2)
    assert not scheduler.in_flight()


def test_no_contract_does_not_start_background_observation(tmp_path):
    from herdr.ego_execution_evidence import submit_ego_observation
    scheduler=Mock()
    assert submit_ego_observation(TASK,tmp_path/'state.db',scheduler=scheduler) is False
    scheduler.submit.assert_not_called()


def test_unverified_hint_does_not_override_complete_trusted_proof():
    hint={**good_fact(),'invoked':False,'outcome':'unknown'}
    assert verify_executions(contract(),[hint,good_fact()])['status']=='satisfied'
    fact=good_fact();fact['evidence_refs']=['']
    assert verify_executions(contract(),[fact])['status']=='unverifiable'


@pytest.mark.parametrize('output', ['{"error":"execution denied before launch"}',
    'execution unavailable', '{"is_error":true,"content":"denied"}'])
def test_rejected_or_unknown_native_result_does_not_prove_process_invocation(output):
    from herdr.native_session_trace import parse_codex_jsonl
    rows=[dict(type='response_item',timestamp=101,payload=dict(type='function_call',
        name='exec_command',call_id='c',arguments=json.dumps({'cmd':'ego-browser nodejs -e x'}))),
        dict(type='response_item',payload=dict(type='function_call_output',call_id='c',output=output))]
    actions,_=parse_codex_jsonl('\n'.join(json.dumps(r) for r in rows),session_id='session')
    facts=native_ego_facts(TASK,actions)
    assert not any(f['invoked'] for f in facts)
    assert verify_executions(contract(),facts)['status']=='unverifiable'
