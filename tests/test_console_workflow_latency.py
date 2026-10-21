"""Protect first workflow paint from workspace scans and auxiliary requests."""
import pytest
from console import herdr_factory_console as c
from herdr import state_db


def test_lightweight_detail_uses_current_snapshot_without_workspace_scan(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    state_db.save_workflow({'workflow_id': 'wf-fast', 'status': 'running',
                           'config': {'nodes': [{'id': 'impl', 'label': '实现'}]}}, db_path=db)
    state_db.save_task({'task_id': 't-fast', 'workflow_id': 'wf-fast', 'node': 'impl',
                       'status': 'working', 'goal': 'Build'}, db_path=db)
    state_db.save_workflow({'workflow_id': 'wf-foreign', 'config': {}}, db_path=db)
    state_db.save_task({'task_id': 't-foreign', 'workflow_id': 'wf-foreign'}, db_path=db)
    def forbidden(*args, **kwargs):
        pytest.fail('lightweight detail must not scan workspaces or list all workflows')
    monkeypatch.setattr(c.inventory, 'workflows', forbidden)
    monkeypatch.setattr(c.read_api, 'api_workflow_deliverables', forbidden)
    monkeypatch.setattr(c.runtime, 'agent_runtime', forbidden)
    monkeypatch.setattr(c.inventory, 'tasks', forbidden)
    detail = c.workflow_detail('wf-fast', lightweight=True)
    assert detail['workflow']['workflow_id'] == 'wf-fast'
    assert [t['task_id'] for t in detail['tasks']] == ['t-fast']
    assert detail['graph']['nodes'][0]['id'] == 'impl'
    assert detail['deliverables']['pending'] is True
    assert detail['coordinator_pending'] is True


def test_lightweight_unknown_workflow_is_not_empty_success(tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    state_db.init_db(db)
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    with pytest.raises((RuntimeError, ValueError), match='不存在|not found'):
        c.workflow_detail('missing', lightweight=True)


def assert_workflow_paints_before_extras(source):
    """Execute production JS; held API responses cannot block the main workflow."""
    import subprocess
    block = source[source.index('async function loadWorkflow('):source.index('function clearWorkflow()')]
    program = r'''
const assert=require('node:assert/strict');
const renders=[];const pending=[];let completed=false;
process.on('beforeExit',()=>assert.equal(completed,true,'workflow never completed rendering assertions'));
const state={workflowId:null,openWorkflowTabIds:[],workflow:null};
const performance={now:()=>42};
const renderWorkflowTabs=()=>{},renderWorkflowHead=()=>{},renderStages=()=>{},renderTasks=()=>{},
 destroyFlowGraph=()=>{},renderDeliverables=()=>{},renderSidebarWorkflows=()=>{},
 renderNodeInspector=()=>{},saveViewState=()=>{},openControllerCockpitModal=()=>{};
const renderFlowWorkbench=()=>renders.push(state.workflow.workflow.workflow_id);
function api(path){
 if(path.startsWith('/api/workflow?id=')){
  const id=new URL('http://local'+path).searchParams.get('id');
  return Promise.resolve({workflow:{workflow_id:id},deliverables:{items:[],pending:true}});
 }
 return new Promise(resolve=>pending.push({path,resolve}));
}
''' + block + r'''
(async()=>{
 await loadWorkflow('A');await new Promise(r=>setImmediate(r));
 assert.deepEqual(renders,['A']);assert.equal(pending.length,5);
 const sidecars=['/api/workflow/controller-cockpit','/api/workflow/deliverables','/api/workflow/controller-actions','/api/workflow/decisions','/api/workflow/runtime'];
 assert.deepEqual(pending.map(p=>p.path.split('?')[0]).sort(),sidecars.slice().sort());
 await loadWorkflow('B');await new Promise(r=>setImmediate(r));
 assert.equal(state.workflow.workflow.workflow_id,'B');
 assert.deepEqual(renders,['A','B'],'primary B render does not await any sidecar');
 assert.equal(pending.length,6,'only the identity-fenced Cockpit read bypasses the four coalesced sidecars');
 assert.equal(pending[5].path,'/api/workflow/controller-cockpit?workflow_id=B');
 const response=id=>({workflow:{workflow_id:id},workflow_id:id,read_state:'confirmed',items:[{name:'from-'+id}],counts:{},coordinator:id});
 for(const p of pending.slice(0,5))p.resolve(response('A'));
 await new Promise(r=>setImmediate(r));
 assert.equal(pending.length,10);
 assert.deepEqual(pending.slice(5).map(p=>p.path.split('?')[0]).sort(),sidecars.slice().sort());
 assert.equal(state.controllerCockpit,null,'late Cockpit A must not install in B');
 assert.deepEqual(state.controllerActionsData,{pending:true});
 assert.deepEqual(state.decisionData,{pending:true});
 assert.deepEqual(state.workflow.deliverables,{items:[],pending:true});
 for(const p of pending.slice(5))p.resolve(response('B'));
 await new Promise(r=>setImmediate(r));
 assert.equal(state.workflow.deliverables.items[0].name,'from-B');
 assert.equal(state.workflow.coordinator,'B');
 assert.equal(state.controllerCockpit.workflow.workflow_id,'B');
 assert.equal(state.controllerActionsData.workflow_id,'B');
 assert.equal(state.decisionData.workflow_id,'B');completed=true;
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result=subprocess.run(['node','-e',program],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr


def test_workflow_paints_before_slow_extras_and_drops_foreign_late_responses():
    assert_workflow_paints_before_extras(c.HTML)


def test_lightweight_graph_keeps_foreign_required_task_diagnostic(tmp_path, monkeypatch):
    db=tmp_path/'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    state_db.save_workflow({'workflow_id':'wf-fast','status':'completed','config':{'nodes':[
        {'id':'impl','required_task_ids':['foreign-task']}]}},db_path=db)
    state_db.save_task({'task_id':'foreign-task','workflow_id':'wf-other','node':'impl'},db_path=db)
    def forbidden():
        pytest.fail('full task inventory must not be loaded')
    monkeypatch.setattr(c.inventory,'tasks',forbidden)
    d=c.workflow_detail('wf-fast',lightweight=True)
    assert d['tasks']==[]
    assert d['graph']['nodes'][0]['completion_issues']==[
        {'task_id':'foreign-task','reason':'required_task_out_of_workflow'}]


@pytest.mark.parametrize('saved_id', ['wf-remembered', '__ctl__', '__templates__', None])
def test_plain_home_restores_workflow_before_slow_overview(saved_id):
    """Exercise the actual startup entry, rather than only a workflow deep link."""
    import json
    import subprocess
    source = c.HTML
    start = source.index('async function initFromUrlOrState(')
    block = source[start:source.index("window.addEventListener('popstate'", start)]
    script = r'''
const assert=require('node:assert/strict');
const savedId=JSON.parse(process.argv[1]);
const state={workflowId:savedId,shellView:'workbench',dashMode:false,opsMode:false};
const window={location:{search:''}};
const calls=[],paint=[];let release,completed=false;
const held=new Promise(r=>release=r);
process.on('beforeExit',()=>assert.equal(completed,true,'startup assertions did not finish'));
async function api(path){calls.push(path);return {workflow:{workflow_id:savedId},project:{project_id:'project',workspace_id:'space'}};}
async function loadWorkflow(id,data){paint.push(id);assert.equal(data.workflow.workflow_id,id);}
function refreshAll(){calls.push('overview');return held;}
function showTemplateLibrary(){calls.push('templates');}
function toast(message){throw new Error(message);}
''' + block + r'''
(async()=>{
 const startup=initFromUrlOrState();await new Promise(r=>setImmediate(r));
 if(savedId==='wf-remembered'){
  assert.deepEqual(paint,[savedId],'plain home must paint remembered workflow while overview is pending');
  assert.equal(calls[0],'/api/workflow?id=wf-remembered&lightweight=1');
  assert.equal(state.projectId,'project');assert.equal(state.spaceId,'space');
 }else{
  assert.deepEqual(paint,[]);
  assert.deepEqual(calls,savedId==='__templates__'?['templates','overview']:['overview'],
    '__templates__ must dispatch the library first, then hydrate overview without blocking startup');
 }
 release();await startup;completed=true;
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script, json.dumps(saved_id)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('endpoint', ['deliverables', 'controller-actions'])
def test_auxiliary_reads_do_not_load_the_global_registry(tmp_path, monkeypatch, endpoint):
    db = tmp_path / 'state.db'
    monkeypatch.setenv('HERDR_STATE_DB', str(db))
    state_db.save_workflow({'workflow_id': 'wf-local', 'status': 'completed',
                           'config': {'nodes': [{'id': 'impl'}]}}, db_path=db)
    state_db.save_task({'task_id': 'local', 'workflow_id': 'wf-local', 'node': 'impl',
                       'status': 'completed'}, db_path=db)
    state_db.save_workflow({'workflow_id': 'wf-other'}, db_path=db)
    state_db.save_task({'task_id': 'foreign', 'workflow_id': 'wf-other'}, db_path=db)
    def forbidden(*args, **kwargs):
        pytest.fail('workflow auxiliary reads must not load global workflow/task inventory')
    monkeypatch.setattr(c.inventory, 'workflows', forbidden)
    monkeypatch.setattr(c.inventory, 'tasks_for_workflow', forbidden)
    if endpoint == 'deliverables':
        result = c.api_workflow_deliverables('wf-local')
        assert all(item.get('task_id') != 'foreign' for item in result['items'])
    else:
        monkeypatch.setattr(c.workflow_api, 'api_workflow_recovery', lambda wid: {'operations': []})
        result = c.api_workflow_controller_actions('wf-local')
        assert result['workflow_id'] == 'wf-local'
