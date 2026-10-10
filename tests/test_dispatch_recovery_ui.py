"""User decisions rebuild responsibility without erasing unknown delivery."""
import importlib.util
from pathlib import Path
import pytest
from tests.test_node_dispatch_contract import scene
from tests.test_downstream_dispatch_contract import downstream, SHA
from herdr import node_dispatch_store as nd, recovery_store


def cancelled(scene):
    downstream(scene)
    scene.store.save_task({'task_id': 'old-test', 'workflow_id': 'wf', 'node': 'test',
        'execution_id': 'execution-1', 'run_id': 'old-run', 'status': 'superseded',
        'replacement_pending': False})
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    return nd.operation_for_node(scene.store.db_path, 'wf', 'test')


def decision(scene, op, action, **extra):
    from herdr import dispatch_recovery
    args = dict(db_path=scene.store.db_path, workflow_id='wf', operation_id=op['id'],
        expected_version=op['version'], operator='reviewer', action=action,
        reason='已查看旧工位，恢复当前候选验收', confirmed_absent=True,
        candidate_sha=SHA, now=1000)
    shown=next(o for o in dispatch_recovery.list_operations(scene.store.db_path,'wf') if o['id']==op['id'])
    args['lineage_snapshot']=[{k:t[k] for k in ('task_id','run_id','version')} for t in shown.get('recovery',{}).get('lineage',[])]
    args.update(extra)
    return dispatch_recovery.decide(**args)


def test_restore_cancelled_scope_creates_new_claimable_predecessor_epoch(scene):
    old = cancelled(scene)
    receipt = decision(scene, old, 'restore_scope')
    new = nd.operation_for_node(scene.store.db_path, 'wf', 'test')
    assert new['id'] == receipt['operation']['id'] != old['id']
    assert new['status'] == 'pending' and not new['started']
    assert new['payload']['candidate_sha'] == SHA
    assert new['payload']['predecessors'] == [{'task_id': 'old-test', 'run_id': 'old-run'}]
    assert scene.store.get_task('old-test')['replacement_pending'] is True
    history = next(o for o in recovery_store.list_operations(scene.store.db_path, 'wf') if o['id'] == old['id'])
    assert history['status'] == 'superseded'
    assert history['detail']['human_decision']['operator'] == 'reviewer'
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['test'], now=1000)
    assert nd.claim(scene.store.db_path, new['id'], 'controller', now=1000)


def test_manual_absence_creates_new_epoch_and_revokes_late_old_launch(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['review'], now=1000)
    old = nd.operation_for_node(scene.store.db_path, 'wf', 'review')
    new = decision(scene, old, 'confirm_absent')['operation']
    assert new['status'] == 'pending' and not new['started']
    assert new['payload']['prior_operation_id'] == old['id']
    assert next(o for o in recovery_store.list_operations(scene.store.db_path, 'wf') if o['id'] == old['id'])['detail']['human_decision']['action'] == 'confirm_absent'
    from herdr import state_db
    conn = state_db.get_db_connection(scene.store.db_path)
    try:
        with pytest.raises(ValueError, match='authorize'):
            nd.validate_launch(conn, old['id'], 'wf', 'review', 1000)
    finally:
        conn.close()
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['review'], now=1000)
    assert nd.claim(scene.store.db_path, new['id'], 'controller', now=1000)


@pytest.mark.parametrize('override', [dict(confirmed_absent=False),dict(operator=''),
    dict(reason=''),dict(candidate_sha='b'*40),dict(expected_version=-1),dict(workflow_id='foreign')])
def test_stale_or_unconfirmed_decision_has_no_partial_writes(scene, override):
    old = cancelled(scene)
    before = scene.store.get_task('old-test')
    with pytest.raises(ValueError):
        decision(scene, old, 'restore_scope', **override)
    assert scene.store.get_task('old-test') == before
    assert next(o for o in recovery_store.list_operations(scene.store.db_path, 'wf') if o['id'] == old['id'])['status'] == 'waiting_human'


def test_absence_confirmation_refuses_unresolved_launch_intent(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['review'], now=1000)
    old = nd.operation_for_node(scene.store.db_path, 'wf', 'review')
    scene.store.record_event('launch_intent', {'intent_id': 'legacy-intent', 'node_id': 'review',
        'execution_id': 'execution-1', 'phase': 'resources_ready'},workflow_id='wf',source='launch',timestamp=1000)
    with pytest.raises(ValueError, match='启动|intent'):
        decision(scene, old, 'confirm_absent')
    assert nd.operation_for_node(scene.store.db_path, 'wf', 'review')['id'] == old['id']


def test_absence_confirmation_refuses_registered_task(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', legacy_notified=['review'], now=1000)
    old = nd.operation_for_node(scene.store.db_path, 'wf', 'review')
    scene.store.save_task({'task_id':'live-review','workflow_id':'wf','node':'review',
        'execution_id':'execution-1','run_id':'review-run','status':'working','candidate_sha':SHA})
    with pytest.raises(ValueError, match='任务'):
        decision(scene, old, 'confirm_absent')


def test_restore_requires_still_ready_active_workflow(scene):
    old = cancelled(scene)
    w=scene.store.get_workflow('wf'); w['status']='paused';scene.store.save_workflow(w)
    with pytest.raises(ValueError):decision(scene,old,'restore_scope')
    assert scene.store.get_task('old-test')['replacement_pending'] is False


def test_console_read_and_post_share_recovery_contract(scene, monkeypatch):
    old = cancelled(scene)
    spec=importlib.util.spec_from_file_location('ui_recovery_console',Path(__file__).resolve().parents[1]/'console/herdr_factory_console.py')
    console=importlib.util.module_from_spec(spec);spec.loader.exec_module(console)
    monkeypatch.setattr(console,'TASKS_FILE',scene.store.db_path.parent/'tasks.json')
    view=console.api_workflow_recovery('wf')
    shown=next(o for o in view['operations'] if o['id']==old['id'])
    assert 'restore_scope' in [a['action'] for a in shown['recovery']['actions']]
    assert '取消' in shown['recovery']['summary']
    result=console.api_workflow_recovery_decide(dict(workflow_id='wf',operation_id=old['id'],
        expected_version=old['version'],operator='human',reason='恢复测试范围，已核实工位',
        action='restore_scope',confirmed_absent=True,candidate_sha=SHA,
        lineage_snapshot=[{k:t[k] for k in ('task_id','run_id','version')} for t in shown['recovery']['lineage']]))
    assert result['ok'] and result['operation']['status']=='pending'
    assert nd.claim(scene.store.db_path,result['operation']['id'],'controller',now=1000)


def test_frontend_resource_check_records_absence_before_restore(scene, monkeypatch):
    old=cancelled(scene)
    from herdr import dispatch_recovery, node_dispatch_store as nd, task_resources
    scene.store.record_event('launch_intent', {'key':'old-key','intent_id':'old-intent',
        'node_id':'test','workflow_id':'wf','task_id':'never-registered','phase':'allocating',
        'execution_id':'execution-1','resources':{}},workflow_id='wf',node_id='test',task_id='old-key',source='launch',timestamp=1000)
    monkeypatch.setattr(task_resources,'inventory_launch_resources',lambda *a,**k: {'resource_status':'absent'})
    result=dispatch_recovery.check_resources(scene.store.db_path,'wf',old['id'],old['version'],'human','已检查现场',candidate_sha=SHA,now=1000)
    assert result['checks'][0]['status']=='resources_absent'
    current=nd.operation_for_node(scene.store.db_path,'wf','test')
    assert decision(scene,current,'restore_scope')['operation']['status']=='pending'


def test_audited_partial_launch_retirement_unblocks_only_current_dispatch(scene, tmp_path):
    import hashlib, json
    from herdr import dispatch_recovery, node_dispatch_store as nd, scheduler_facts, task_resources
    from tests.test_downstream_dispatch_contract import downstream
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    old = nd.operation_for_node(scene.store.db_path, 'wf', 'test')
    old = nd.claim(scene.store.db_path, old['id'], 'controller', now=1000)
    old = nd.start(scene.store.db_path, old['id'], 'controller', now=1000)
    clone = tmp_path / 'clones' / 'old-partial'
    clone.mkdir(parents=True)
    intent = task_resources.begin_launch_intent(scene.store, workflow_id='wf', node_id='test',
        candidate_sha=SHA, task_id='old-partial-task', dispatch_operation_id=old['id'],
        run_id='old-partial-run', execution_id='execution-1', now=1001)['intent']
    resources = {'planned_clone_path': str(clone), 'run_id': 'old-partial-run',
        'pane_id': 'pane-partial', 'terminal_id': 'terminal-partial', 'pane_source': 'dynamic'}
    intent = task_resources.record_launch_resources(scene.store, intent, resources, now=1002)
    tag = {'workflow_id': 'wf', 'node_id': 'test', 'candidate_sha': SHA,
        'intent_id': intent['intent_id'], 'task_id': 'old-partial-task',
        'run_id': 'old-partial-run', 'pane_id': 'pane-partial',
        'terminal_id': 'terminal-partial', 'pane_source': 'dynamic',
        'phase': 'agent_start_requested'}
    task_resources.write_worker_launch_identity(clone, tag, initial=True)
    scheduler_facts.record_candidate_frozen('wf', 'b' * 40, db_path=scene.store.db_path)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1003)
    current = nd.operation_for_node(scene.store.db_path, 'wf', 'test')
    assert current['id'] != old['id']
    assert current['status'] == 'waiting_human'
    assert current['detail']['reason'] == 'dispatch_prior_delivery_unknown'
    prior = next(item for item in recovery_store.list_operations(scene.store.db_path, 'wf')
                 if item['id'] == old['id'])
    transcript = 'Agent startup\nWaiting for task instructions.\n'
    digest = hashlib.sha256(transcript.encode()).hexdigest()
    closed = []
    race_once = [True]
    def runner(argv, _timeout):
        if argv[1:3] == ['pane', 'get']:
            return 0, json.dumps({'result': {'pane': {'pane_id': 'pane-partial',
                'terminal_id': 'terminal-partial', 'cwd': str(clone)}}}), ''
        if argv[1:3] == ['agent', 'get']:
            return 0, json.dumps({'result': {'agent': None}}), ''
        if argv[1:3] == ['pane', 'read']:
            return 0, transcript, ''
        if argv[1:3] == ['pane', 'close']:
            closed.append(argv[3])
            if race_once[0]:
                with recovery_store._transaction(scene.store.db_path) as conn:
                    changed = recovery_store._get(conn, prior['id'])
                    nd._write(conn, changed, 'waiting_human', {'race_marker': 'version changed'}, 1004)
                race_once[0] = False
            return 0, '{}', ''
        if argv[1:3] == ['pane', 'list']:
            panes = [] if closed else [{'pane_id': 'pane-partial', 'cwd': str(clone)}]
            return 0, json.dumps({'result': {'panes': panes}}), ''
        raise AssertionError(argv)
    recovery_args = dict(db_path=scene.store.db_path, workflow_id='wf', operation_id=current['id'],
        expected_version=current['version'], prior_operation_id=prior['id'],
        expected_prior_version=prior['version'], operator='human', reason='startup only',
        candidate_sha='b' * 40, intent_id=intent['intent_id'], task_id='old-partial-task',
        pane_id='pane-partial', terminal_id='terminal-partial', transcript_sha256=digest,
        confirmed_startup_only=True, runner=runner, now=1004)
    event_count = len(scene.store.list_events(event_type='launch_intent'))
    for stale in ({'expected_version': current['version'] + 1}, {'candidate_sha': 'c' * 40}):
        with pytest.raises(ValueError):
            dispatch_recovery.abandon_partial_launch(**{**recovery_args, **stale})
        assert clone.is_dir() and closed == []
        assert len(scene.store.list_events(event_type='launch_intent')) == event_count
    with pytest.raises(ValueError, match='CAS'):
        dispatch_recovery.abandon_partial_launch(**recovery_args)
    assert closed == ['pane-partial'] and not clone.exists()
    # The native archive completed, but the concurrent version change prevented
    # dispatch release. Refreshing the recovery action must safely finish from
    # the durable resources_absent receipt without closing or deleting again.
    refreshed = next(item for item in dispatch_recovery.list_operations(scene.store.db_path, 'wf')
                     if item['id'] == current['id'])
    action = next(a for a in refreshed['recovery']['actions']
                  if a['action'] == 'abandon_partial_launch')
    retry_args = {**recovery_args, 'expected_prior_version': action['prior_version']}
    result = dispatch_recovery.abandon_partial_launch(**retry_args)
    assert result['ok']
    assert result['retired_operation']['status'] == 'superseded'
    assert result['operation']['status'] == 'pending'
    assert result['receipt']['archived_clone'] and Path(result['receipt']['archived_clone']).is_dir()
    assert closed == ['pane-partial']
    assert nd.claim(scene.store.db_path, current['id'], 'next-controller', now=1005)


def test_frontend_resource_check_never_converts_unknown_into_absence(scene, monkeypatch):
    old=cancelled(scene)
    from herdr import dispatch_recovery, task_resources
    scene.store.record_event('launch_intent', {'key':'old-key','intent_id':'old-intent',
        'node_id':'test','workflow_id':'wf','task_id':'never-registered','phase':'allocating',
        'execution_id':'execution-1','resources':{}},workflow_id='wf',node_id='test',task_id='old-key',source='launch',timestamp=1000)
    monkeypatch.setattr(task_resources,'inventory_launch_resources',lambda *a,**k: {'resource_status':'unknown','reason':'native_unavailable'})
    result=dispatch_recovery.check_resources(scene.store.db_path,'wf',old['id'],old['version'],'human','已检查现场',candidate_sha=SHA,now=1000)
    assert result['checks'][0]['status']=='unknown'
    with pytest.raises(ValueError,match='启动'):
        decision(scene,nd.operation_for_node(scene.store.db_path,'wf','test'),'restore_scope')


def test_two_independent_recovery_requests_create_only_one_epoch(scene):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    old=cancelled(scene)
    version=scene.store.get_task('old-test')['version']
    barrier=Barrier(2)
    def request():
        barrier.wait(timeout=5)
        try:return decision(scene,old,'restore_scope')['operation']['id']
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:request(),range(2)))
    assert sum(r is not None for r in results)==1
    assert scene.store.get_task('old-test')['version']==version+1
    operations=[o for o in recovery_store.list_operations(scene.store.db_path,'wf') if o['payload'].get('node_id')=='test']
    assert len(operations)==2
    assert sum(o['status']=='pending' for o in operations)==1


def test_recovery_candidate_rotation_cannot_restore_old_scope(scene):
    old=cancelled(scene)
    scene.store.record_event('candidate_frozen',{'candidate_sha':SHA},workflow_id='wf',
        source='critical-path-scheduler',timestamp=1001)
    with pytest.raises(ValueError,match='变化'):
        decision(scene,old,'restore_scope')
    assert scene.store.get_task('old-test')['replacement_pending'] is False


def test_successful_user_recovery_reaches_real_registration_without_extra_send(scene, monkeypatch):
    old=cancelled(scene)
    new=decision(scene,old,'restore_scope')['operation']
    assert nd.claim(scene.store.db_path,new['id'],'controller',now=1000)
    nd.start(scene.store.db_path,new['id'],'controller',now=1000,expected_task_ids=['new-test'])
    from herdr.task_resources import begin_launch_intent, finish_launch_intent
    intent=begin_launch_intent(scene.store,workflow_id='wf',node_id='test',candidate_sha=SHA,
        task_id='new-test',dispatch_round=2,execution_id='execution-1',run_id='new-run',
        dispatch_operation_id=new['id'],supersedes='old-test',now=1000)['intent']
    scene.store.save_task({'task_id':'new-test','workflow_id':'wf','node':'test',
        'status':'working','candidate_sha':SHA,'execution_id':'execution-1','run_id':'new-run',
        'launch_intent_id':intent['intent_id'],'dispatch_operation_id':new['id'],'supersedes':'old-test',
        'dispatch_role':'worker','dispatch_round':2})
    finish_launch_intent(scene.store,intent,now=1000)
    nd.reconcile_workflow(scene.store.db_path,'wf',now=1000)
    assert nd.operation_for_node(scene.store.db_path,'wf','test')['status']=='resolved'
    from herdr import dispatch_recovery
    shown=next(o for o in dispatch_recovery.list_operations(scene.store.db_path,'wf') if o['id']==new['id'])
    assert shown['confirmation']['task_ids']==['new-test']
    assert nd.claim(scene.store.db_path,new['id'],'duplicate',now=1000) is None


@pytest.mark.parametrize('outcome',['success','failure'])
@pytest.mark.parametrize('change',['switch','loading','replace','cancel','during_refresh'])
def test_open_recovery_form_binds_original_workflow_even_if_user_switches(outcome,change):
    import json,subprocess
    source=(Path(__file__).resolve().parents[1]/'console/herdr_factory_console.py').read_text()
    functions='function currentRecovery(nodeId){'+source.split('function currentRecovery(nodeId){',1)[1].split('function decideRecovery(',1)[0]
    op={'id':7,'version':1,'workflow_id':'wf','payload':{'node_id':'test'}}
    op['recovery']={'summary':'取消范围','next_step':'核实现场','candidate_sha':SHA,
        'actions':[{'action':'restore_scope','label':'恢复范围'}]}
    script='const state='+json.dumps({'workflowId':'__ctl__','workflow':{'workflow':{'workflow_id':'wf'}},'controllerActionsData':{'recovery':[op]}})+';\n'+functions+'''
const nodes={recoveryForm:{isConnected:true},modal:{classList:{contains(){return nodes.open}}},open:true,recoverySubmit:{disabled:false},recoveryOperator:{value:'human'},recoveryReason:{value:'恢复已取消范围'},recoveryConfirmed:{checked:true},recoveryError:{}};
const document={getElementById:id=>nodes[id]};const esc=x=>String(x);const cleanStageLabel=x=>x;
let closeCount=0,loadCount=0,cockpitCount=0;function openModal(){}function closeModal(){closeCount++;}function toast(){}function openControllerCockpitModal(){cockpitCount++;}
async function loadWorkflow(){loadCount++;if(CHANGE==='during_refresh')state.workflowId='other';}let body,settle;
async function api(url,options){body=JSON.parse(options.body);return await new Promise((resolve,reject)=>{settle=()=>OUTCOME==='success'?resolve({ok:true}):reject(new Error('old-error'));});}
(async()=>{openRecoveryDecision('''+str(op['id'])+''','restore_scope');const request=nodes.recoveryForm.onsubmit({preventDefault(){}});if(CHANGE==='switch'){state.workflowId='other';state.workflow.workflow.workflow_id='other';}if(CHANGE==='loading')state.workflowId='other';if(CHANGE==='replace'){nodes.recoveryForm={isConnected:true};nodes.recoveryError={textContent:'new-error'};}if(CHANGE==='cancel')nodes.open=false;settle();await request;console.log(JSON.stringify({body,closeCount,loadCount,cockpitCount,error:nodes.recoveryError.textContent}));})();
'''
    script=script.replace('OUTCOME',json.dumps(outcome)).replace('CHANGE',json.dumps(change))
    result=subprocess.run(['node','-e',script],text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['body']['workflow_id']=='wf'
    assert json.loads(result.stdout)['closeCount']==(1 if change=='during_refresh' and outcome=='success' else 0)
    assert json.loads(result.stdout)['loadCount']==(1 if change=='during_refresh' and outcome=='success' else 0)
    assert json.loads(result.stdout)['cockpitCount']==0
    assert json.loads(result.stdout).get('error') != 'old-error' or (change=='during_refresh' and outcome=='failure')


def test_user_restore_flows_through_controller_and_still_waits_for_registration(scene, monkeypatch):
    old=cancelled(scene)
    new=decision(scene,old,'restore_scope')['operation']
    monkeypatch.setattr(scene.ctrl,'try_direct_stage_advance',lambda _:False)
    node=scene.store.get_workflow('wf')['config']['nodes'][1]
    item={'kind':'stage_advance','workflow_id':'wf','stage':'implementation',
          'node_id':'test','next_stage':'test','node':node}
    scene.ctrl._handle_coordinator_item(item)
    current=nd.operation_for_node(scene.store.db_path,'wf','test')
    assert current['id']==new['id'] and current['started']==1
    assert current['status']=='awaiting_result'
    assert len(scene.sent)==1
    scene.ctrl._handle_coordinator_item(item)
    assert len(scene.sent)==1


def test_late_workflow_refresh_keeps_controller_recovery_page():
    import subprocess
    source=(Path(__file__).resolve().parents[1]/'console/herdr_factory_console.py').read_text()
    function='async function loadWorkflow(id){'+source.split('async function loadWorkflow(id){',1)[1].split('\nfunction ',1)[0].split('\nasync function ',1)[0]
    script='''const state={workflowId:'wf',workflow:{workflow:{workflow_id:'wf'}},openWorkflowTabIds:[]};
let finish;const pending=new Promise(r=>finish=r);let graph=0,cockpit=0;
async function api(url){await pending;return url.includes('id=')?{workflow:{workflow_id:'wf'}}:{};}
function saveViewState(){}function renderWorkflowTabs(){}function renderWorkflowHead(){}function renderStages(){}function renderTasks(){}function destroyFlowGraph(){}
function renderFlowWorkbench(){graph++;}function openControllerCockpitModal(){cockpit++;state.workflowId='__ctl__';}
'''+function+'''
(async()=>{const request=loadWorkflow('wf');state.workflowId='__ctl__';finish();await request;console.log(JSON.stringify({selected:state.workflowId,graph,cockpit}));})();
'''
    result=subprocess.run(['node','-e',script],text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    import json
    assert json.loads(result.stdout)=={'selected':'__ctl__','graph':0,'cockpit':1}


def test_legacy_scope_requires_known_run_and_explicit_execution_binding(scene):
    old=cancelled(scene)
    task=scene.store.get_task('old-test');task.pop('execution_id');scene.store.save_task(task)
    with pytest.raises(ValueError,match='归属'):
        decision(scene,old,'restore_scope')
    result=decision(scene,old,'restore_scope',confirmed_lineage=True)
    assert result['operation']['status']=='pending'
    assert scene.store.get_task('old-test')['execution_id']=='execution-1'
    receipt=next(o for o in recovery_store.list_operations(scene.store.db_path,'wf') if o['id']==old['id'])['detail']['human_decision']
    assert receipt['bound_task_ids']==['old-test']


def test_cancelled_unknown_run_cannot_be_invented_by_human_checkbox(scene):
    old=cancelled(scene)
    task=scene.store.get_task('old-test');task.pop('run_id');scene.store.save_task(task)
    with pytest.raises(ValueError,match='身份'):
        decision(scene,old,'restore_scope',confirmed_lineage=True)
    assert scene.store.get_task('old-test').get('run_id') is None


def test_late_unbound_launch_and_registration_cannot_capture_human_epoch(scene):
    old=cancelled(scene)
    new=decision(scene,old,'restore_scope')['operation']
    from herdr.task_resources import begin_launch_intent
    with pytest.raises(ValueError,match='授权'):
        begin_launch_intent(scene.store,workflow_id='wf',node_id='test',candidate_sha=SHA,
            task_id='late-old',now=1000)
    with pytest.raises(ValueError,match='授权'):
        scene.store.save_task({'task_id':'late-old','workflow_id':'wf','node':'test','status':'working',
            'execution_id':'execution-1','run_id':'late-run','candidate_sha':SHA})
    assert nd.operation_for_node(scene.store.db_path,'wf','test')['id']==new['id']


def test_missing_recovery_response_does_not_render_all_clear():
    import subprocess
    source=(Path(__file__).resolve().parents[1]/'console/herdr_factory_console.py').read_text()
    functions='function currentRecovery(nodeId){'+source.split('function currentRecovery(nodeId){',1)[1].split('function decideRecovery(',1)[0]
    script="const state={workflowId:'wf',workflow:{workflow:{workflow_id:'wf'}},controllerActionsData:null};\n"+functions+"\nconsole.log(renderRecoveryPanel('wf'));"
    result=subprocess.run(['node','-e',script],text=True,capture_output=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert '无法读取' in result.stdout and '刷新' in result.stdout


def test_cancelled_started_dispatch_restores_scope_not_a_second_cancelled_epoch(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path,'wf',legacy_notified=['test'],now=1000)
    scene.store.save_task({'task_id':'old-test','workflow_id':'wf','node':'test',
        'execution_id':'execution-1','run_id':'old-run','status':'superseded','replacement_pending':False})
    nd.reconcile_workflow(scene.store.db_path,'wf',now=1000)
    old=nd.operation_for_node(scene.store.db_path,'wf','test')
    assert old['started'] and old['status']=='waiting_human'
    new=decision(scene,old,'restore_scope')['operation']
    assert scene.store.get_task('old-test')['replacement_pending'] is True
    assert nd.claim(scene.store.db_path,new['id'],'controller',now=1000)


@pytest.mark.parametrize('field,value',[('run_id','changed-run'),('goal','changed-after-form')])
def test_human_lineage_confirmation_binds_task_run_and_version(scene,field,value):
    old=cancelled(scene)
    task=scene.store.get_task('old-test');task.pop('execution_id');scene.store.save_task(task)
    snapshot=[{'task_id':'old-test','run_id':'old-run','version':scene.store.get_task('old-test')['version']}]
    task=scene.store.get_task('old-test');task[field]=value;scene.store.save_task(task)
    with pytest.raises(ValueError,match='任务.*变化'):
        decision(scene,old,'restore_scope',confirmed_lineage=True,lineage_snapshot=snapshot)
    assert scene.store.get_task('old-test').get('execution_id') is None


def test_registered_then_cancelled_task_can_restore_known_closed_launch(scene):
    downstream(scene)
    nd.reconcile_workflow(scene.store.db_path,'wf',now=1000)
    old=nd.operation_for_node(scene.store.db_path,'wf','test')
    nd.claim(scene.store.db_path,old['id'],'owner',now=1000);nd.start(scene.store.db_path,old['id'],'owner',now=1000)
    from herdr.task_resources import begin_launch_intent,finish_launch_intent
    intent=begin_launch_intent(scene.store,workflow_id='wf',node_id='test',candidate_sha=SHA,
        task_id='old-test',execution_id='execution-1',run_id='old-run',dispatch_operation_id=old['id'],now=1000)['intent']
    scene.store.save_task({'task_id':'old-test','workflow_id':'wf','node':'test','status':'working',
        'candidate_sha':SHA,'execution_id':'execution-1','run_id':'old-run','launch_intent_id':intent['intent_id'],
        'dispatch_operation_id':old['id'],'dispatch_role':'worker','dispatch_round':1})
    finish_launch_intent(scene.store,intent,now=1000)
    nd.reconcile_workflow(scene.store.db_path,'wf',now=1000)
    task=scene.store.get_task('old-test');task.update(status='superseded',replacement_pending=False);scene.store.save_task(task)
    nd.reconcile_workflow(scene.store.db_path,'wf',now=1000)
    recovery=nd.operation_for_node(scene.store.db_path,'wf','test')
    new=decision(scene,recovery,'restore_scope')['operation']
    assert nd.claim(scene.store.db_path,new['id'],'new-owner',now=1000)

@pytest.mark.parametrize('field,value', [('execution_id', None), ('run_id', None)])
def test_recovery_refuses_unknown_upstream_identity(scene, field, value):
    old = cancelled(scene)
    task = scene.store.get_task('impl'); task[field] = value
    scene.store.save_task(task)
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    old = nd.operation_for_node(scene.store.db_path, 'wf', 'test')
    from herdr import dispatch_recovery
    shown = next(o for o in dispatch_recovery.list_operations(scene.store.db_path, 'wf') if o['id'] == old['id'])
    assert '上游任务执行身份' in shown['recovery']['next_step']
    assert 'restore_scope' not in {a['action'] for a in shown['recovery']['actions']}
    with pytest.raises(ValueError, match='上游任务执行身份'):
        decision(scene, old, 'restore_scope')
    assert scene.store.get_task('old-test')['replacement_pending'] is False

def test_resource_check_rejects_candidate_rotation_even_for_root_operation(scene, monkeypatch):
    from herdr import dispatch_recovery, task_resources
    scene.store.record_event('candidate_frozen', {'candidate_sha':SHA},workflow_id='wf',source='critical-path-scheduler',timestamp=1000)
    nd.reconcile_workflow(scene.store.db_path,'wf',now=1000)
    old=nd.operation_for_node(scene.store.db_path,'wf','implementation')
    scene.store.record_event('launch_intent', {'key':'root-key','intent_id':'root-intent','node_id':'implementation','workflow_id':'wf','task_id':'never-root','phase':'allocating','execution_id':'execution-1','resources':{}},workflow_id='wf',node_id='implementation',source='launch',timestamp=1000)
    scene.store.record_event('candidate_frozen', {'candidate_sha':'b'*40},workflow_id='wf',source='critical-path-scheduler',timestamp=1001)
    calls=[]
    monkeypatch.setattr(task_resources,'inventory_launch_resources',lambda *a,**k:calls.append(1) or {'resource_status':'absent'})
    with pytest.raises(ValueError,match='候选'):
        dispatch_recovery.check_resources(scene.store.db_path,'wf',old['id'],old['version'],'human','现场核查',candidate_sha=SHA,now=1000)
    assert calls == []


def test_resource_check_does_not_attach_results_after_candidate_rotates_during_probe(scene, monkeypatch):
    old=cancelled(scene)
    from herdr import dispatch_recovery, task_resources
    scene.store.record_event('launch_intent', {'key':'probe-key','intent_id':'probe-intent','node_id':'test','workflow_id':'wf','task_id':'never-probe','phase':'allocating','execution_id':'execution-1','resources':{}},workflow_id='wf',node_id='test',task_id='probe-key',source='launch',timestamp=1000)
    def probe(*a,**k):
        scene.store.record_event('candidate_frozen',{'candidate_sha':'b'*40},workflow_id='wf',source='critical-path-scheduler',timestamp=1001)
        return {'resource_status':'absent'}
    monkeypatch.setattr(task_resources,'inventory_launch_resources',probe)
    with pytest.raises(ValueError,match='核查期间候选'):
        dispatch_recovery.check_resources(scene.store.db_path,'wf',old['id'],old['version'],'human','核查',candidate_sha=SHA,now=1000)
    shown=next(o for o in recovery_store.list_operations(scene.store.db_path,'wf') if o['id']==old['id'])
    assert 'resource_checks' not in shown['detail']


@pytest.mark.parametrize('action', ['hold', 'verify'])
def test_resource_check_rejects_version_change_without_partial_receipts(scene, monkeypatch, action):
    if action=='hold':
        old=cancelled(scene)
    else:
        downstream(scene)
        nd.reconcile_workflow(scene.store.db_path,'wf',legacy_notified=['review'],now=1000)
        old=nd.operation_for_node(scene.store.db_path,'wf','review')
    node=old['payload']['node_id']
    scene.store.record_event('launch_intent', {'key':'version-key','intent_id':'version-intent','node_id':node,'workflow_id':'wf','task_id':'never-version','phase':'allocating','execution_id':'execution-1','resources':{}},workflow_id='wf',node_id=node,task_id='version-key',source='launch',timestamp=1000)
    from herdr import dispatch_recovery, task_resources
    def probe(*a,**k):
        recovery_store.decide_operation(scene.store.db_path,old['id'],old['version'],'other',action,'concurrent decision',1001,until=2000)
        return {'resource_status':'absent'}
    monkeypatch.setattr(task_resources,'inventory_launch_resources',probe)
    with pytest.raises(ValueError,match='变化'):
        dispatch_recovery.check_resources(scene.store.db_path,'wf',old['id'],old['version'],'human','核查',candidate_sha=SHA,now=1002)
    intent=scene.store.list_events(task_id='version-key',event_type='launch_intent',source='launch',desc=True,limit=1)[0]['payload']
    assert intent['phase']=='allocating'
    shown=next(o for o in recovery_store.list_operations(scene.store.db_path,'wf') if o['id']==old['id'])
    assert 'resource_checks' not in shown['detail']
    assert not scene.store.list_events(workflow_id='wf',event_type='dispatch_resources_checked')


def test_resource_receipts_roll_back_when_audit_write_fails(scene, monkeypatch):
    old=cancelled(scene)
    from herdr import dispatch_recovery, task_resources, state_db
    intent={'key':'rollback-key','intent_id':'rollback-intent','node_id':'test','workflow_id':'wf','task_id':'never-rollback','phase':'allocating','execution_id':'execution-1','resources':{}}
    scene.store.record_event('launch_intent',intent,workflow_id='wf',node_id='test',task_id=intent['key'],source='launch',timestamp=1000)
    before=scene.store.list_events(workflow_id='wf')
    monkeypatch.setattr(task_resources,'inventory_launch_resources',lambda *a,**k:{'resource_status':'absent'})
    original=state_db.record_event
    def fail_audit(event,*a,**k):
        if event['event_type']=='dispatch_resources_checked':
            raise OSError('audit unavailable')
        return original(event,*a,**k)
    monkeypatch.setattr(state_db,'record_event',fail_audit)
    with pytest.raises(OSError,match='audit unavailable'):
        dispatch_recovery.check_resources(scene.store.db_path,'wf',old['id'],old['version'],'human','核查',candidate_sha=SHA,now=1000)
    assert scene.store.list_events(workflow_id='wf')==before
    assert nd.operation_for_node(scene.store.db_path,'wf','test')==old


def test_resource_check_refuses_intent_changed_during_probe(scene, monkeypatch):
    old=cancelled(scene)
    from herdr import dispatch_recovery, task_resources
    intent={'key':'changed-key','intent_id':'changed-intent','node_id':'test','workflow_id':'wf','task_id':'never-changed','phase':'allocating','execution_id':'execution-1','resources':{}}
    scene.store.record_event('launch_intent',intent,workflow_id='wf',node_id='test',task_id=intent['key'],source='launch',timestamp=1000)
    def probe(*a,**k):
        scene.store.record_event('launch_intent',dict(intent,intent_id='new-intent'),workflow_id='wf',node_id='test',task_id=intent['key'],source='launch',timestamp=1001)
        return {'resource_status':'absent'}
    monkeypatch.setattr(task_resources,'inventory_launch_resources',probe)
    with pytest.raises(ValueError,match='启动记录已变化'):
        dispatch_recovery.check_resources(scene.store.db_path,'wf',old['id'],old['version'],'human','核查',candidate_sha=SHA,now=1000)
    events=scene.store.list_events(task_id=intent['key'],event_type='launch_intent',source='launch',desc=True)
    assert len(events)==2 and all(e['payload']['phase']=='allocating' for e in events)
    assert nd.operation_for_node(scene.store.db_path,'wf','test')==old


def test_can_launch_with_replacement_allows_superseded_pointer():
    from herdr.node_dispatch_store import can_launch_with_replacement
    op = {'id': 'op-1', 'status': 'pending', 'payload': {'node_id': 'review'}}
    intent = {'dispatch_operation_id': 'op-1', 'supersedes': 'old-review'}
    tasks = [{'task_id': 'old-review', 'node': 'review', 'status': 'superseded'}]
    assert can_launch_with_replacement(op, intent, tasks) is True


def test_can_launch_with_replacement_refuses_terminal_operation():
    from herdr.node_dispatch_store import can_launch_with_replacement
    intent = {'dispatch_operation_id': 'op-1', 'supersedes': 'old-review'}
    tasks = [{'task_id': 'old-review', 'node': 'review', 'status': 'superseded'}]
    for status in ('resolved', 'superseded'):
        op = {'id': 'op-1', 'status': status, 'payload': {'node_id': 'review'}}
        assert can_launch_with_replacement(op, intent, tasks) is False


def test_can_launch_with_replacement_refuses_non_superseded():
    from herdr.node_dispatch_store import can_launch_with_replacement
    op = {'id': 'op-1', 'payload': {'node_id': 'review'}}
    intent = {'dispatch_operation_id': 'op-1', 'supersedes': 'old-review'}
    tasks = [{'task_id': 'old-review', 'node': 'review', 'status': 'working'}]
    assert can_launch_with_replacement(op, intent, tasks) is False


def test_explicit_replacement_reuses_pending_operation_with_precise_reasons(scene):
    from herdr import node_dispatch_store as nd, state_db
    from herdr.task_resources import begin_launch_intent
    nd.reconcile_workflow(scene.store.db_path, 'wf', now=1000)
    op = nd.operation_for_node(scene.store.db_path, 'wf', 'implementation')
    assert op['status'] == 'pending' and not op['started']
    scene.store.save_task({'task_id': 'old-impl', 'workflow_id': 'wf', 'node': 'implementation',
        'execution_id': 'execution-1', 'run_id': 'old-run', 'status': 'superseded',
        'dispatch_role': 'worker', 'dispatch_round': 1})
    conn = state_db.get_db_connection(scene.store.db_path)
    try:
        with pytest.raises(ValueError, match='never started'):
            nd.validate_launch(conn, op['id'], 'wf', 'implementation', 1000)
    finally:
        conn.close()
    intent = begin_launch_intent(scene.store, workflow_id='wf', node_id='implementation',
        task_id='impl-r2', role='worker', dispatch_operation_id=op['id'], run_id='run-impl-r2',
        execution_id='execution-1', supersedes='old-impl', dispatch_round=2,
        now=scene.clock[0])['intent']
    assert intent['dispatch_operation_id'] == op['id']
