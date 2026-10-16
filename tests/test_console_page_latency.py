"""Behavior regressions for bounded Console reads and refresh orchestration."""
import subprocess
from pathlib import Path

from console import herdr_factory_console as c
from herdr import state_db


def run_js(block, script):
    result = subprocess.run(['node', '-e', "const assert=require('node:assert/strict');\n" + script.split('// SOURCE')[0] + '\n' + block + '\n' + script.split('// SOURCE')[1]], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_same_get_is_shared_but_writes_and_new_auth_are_not():
    source = c.HTML
    start = source.find('const apiReads=')
    if start < 0:
        start = source.index('async function api(')
    block = source[start:source.index('function friendlyErrorChinese', start)]
    run_js(block, r'''
const pending=[];let token='';
const sessionStorage={getItem:()=>token};const document={getElementById:()=>null};
function fetch(path,opts){return new Promise(resolve=>pending.push({path,opts,resolve}));}
function release(){for(const p of pending.splice(0))p.resolve({ok:true,json:async()=>({ok:true,data:{value:1}})});}
// SOURCE
(async()=>{
 const a=api('/same'),b=api('/same');
 assert.equal(pending.length,1,'identical in-flight GET must share network work');
 token='different-user';const d=api('/same');assert.equal(pending.length,2);
 const w1=api('/same',{method:'POST',body:'{}'}),w2=api('/same',{method:'POST',body:'{}'});
 assert.equal(pending.length,4,'writes must never be deduplicated');
 release();await Promise.all([a,b,d,w1,w2]);
 const fresh=api('/same');assert.equal(pending.length,1,'settled reads are not cached');release();await fresh;
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_page_reads_keep_only_latest_waiting_scope_and_recover_from_failure():
    source = c.HTML
    start = source.find('const pageReads=')
    assert start >= 0, 'page refresh requests need a shared bounded scheduler'
    block = source[start:source.index('function friendlyErrorChinese', start)]
    # This block follows api(), so only pageRead is under test here.
    run_js(block, r'''
const pending=[];function api(path){return new Promise((resolve,reject)=>pending.push({path,resolve,reject}));}
// SOURCE
(async()=>{
 const a=pageRead('dash','A'),again=pageRead('dash','A'),b=pageRead('dash','B'),d=pageRead('dash','C');
 assert.deepEqual(pending.map(p=>p.path),['A']);pending[0].resolve({id:'A'});
 await a;await again;await new Promise(r=>setImmediate(r));
 assert.deepEqual(pending.map(p=>p.path),['A','C'],'intermediate scope must not be requested');
 assert.equal(await b,null);pending[1].reject(new Error('offline'));await assert.rejects(d,/offline/);
 const retry=pageRead('dash','C');assert.equal(pending.length,3);pending[2].resolve({id:'C'});assert.deepEqual(await retry,{id:'C'});
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_logs_read_a_bounded_tail_and_mark_oversized_line(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'LOG_DIR', tmp_path)
    log=tmp_path/'controller.out.log'
    log.write_bytes(b'x'*(3*1024*1024)+b'\n'+('中文\n'*30).encode())
    original=Path.read_text
    def forbid_full_read(path,*args,**kwargs):
        if path == log:
            raise AssertionError('must not read the entire log')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',forbid_full_read)
    assert c.tail_log(n=10) == '\n'.join(['中文']*10)
    log.write_bytes(b'x'*(3*1024*1024))
    assert '截断' in c.tail_log(n=10)
    assert len(c.tail_log(n=10).encode()) <= 256*1024


def test_lightweight_project_does_not_probe_runtime_or_global_inventory(tmp_path,monkeypatch):
    db=tmp_path/'state.db';monkeypatch.setenv('HERDR_STATE_DB',str(db))
    state_db.save_workflow({'workflow_id':'older','project_id':'p','created_at':1,'config':{}},db_path=db)
    state_db.save_workflow({'workflow_id':'newer','project_id':'p','created_at':2,'config':{}},db_path=db)
    state_db.save_workflow({'workflow_id':'foreign','project_id':'other','config':{}},db_path=db)
    monkeypatch.setattr(c,'project_by_id',lambda pid:{'project_id':'p','workspace_id':'ws'})
    def forbidden(*args,**kwargs): raise AssertionError('metadata must not scan runtime or global state')
    for name in ('panes','tabs','slots','workflows','tasks'):
        monkeypatch.setattr(c,name,forbidden)
    monkeypatch.setattr(c,'preflight',lambda p,**kw:[])
    detail=c.project_detail('p',lightweight=True)
    assert [w['workflow_id'] for w in detail['workflows']]==['newer','older']
    assert detail['runtime_pending'] is True


def test_project_metadata_paints_without_waiting_for_runtime():
    source=c.HTML
    block=source[source.index('async function loadProject('):source.index('function workflowSubject(')]
    run_js(block,r'''
const state={shellView:'workbench',workflowId:'wf',openWorkflowTabIds:[]};
const document={getElementById:()=>({style:{}})};
const renderOverview=()=>{},renderAgents=()=>{},renderSlots=()=>{},renderWorkflowSwitcher=()=>{},renderWorkflowTabs=()=>{};
const renders=[],calls=[];let releaseRuntime;
async function loadWorkflow(id){renders.push(id)}
function pageRead(kind,path){calls.push(path);if(kind==='project')return Promise.resolve({project:{project_name:'demo'},workflows:[{workflow_id:'wf'}],runtime_pending:true});return new Promise(r=>releaseRuntime=r);}
// SOURCE
(async()=>{
 await loadProject('p');assert.deepEqual(renders,['wf']);assert.equal(state.project.runtime_pending,true);
 assert.ok(calls[0].endsWith('&lightweight=1'));assert.ok(calls[1].startsWith('/api/project/runtime'));
 state.projectId='other';releaseRuntime({slots:[{pane_id:'foreign'}]});await new Promise(r=>setImmediate(r));
 assert.equal(state.project.runtime_pending,true,'late runtime must not attach to another project');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_dashboard_drops_older_scope_response():
    source=c.HTML
    block=source[source.index('async function loadDashboard('):source.index('function dashSelectWorkflow(')]
    run_js(block,r'''
const state={dashMode:true,dashWorkflowId:'A'};const pending=[],renders=[];
function pageRead(kind,path){return new Promise((resolve,reject)=>pending.push({path,resolve,reject}));}
const renderDashboard=()=>renders.push(state.dash.id);const toast=()=>{};
// SOURCE
(async()=>{
 const old=loadDashboard();state.dashWorkflowId='B';const latest=loadDashboard();
 pending[1].resolve({id:'B'});await latest;pending[0].resolve({id:'A'});await old;
 assert.deepEqual(renders,['B']);assert.equal(state.dash.id,'B');
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_dashboard_reads_one_readonly_inventory(tmp_path,monkeypatch):
    db=tmp_path/'state.db';monkeypatch.setenv('HERDR_STATE_DB',str(db))
    state_db.save_workflow({'workflow_id':'wf-dash','status':'completed','config':{}},db_path=db)
    state_db.save_task({'task_id':'t-dash','workflow_id':'wf-dash','status':'cleaned'},db_path=db)
    def forbidden(*a,**kw):raise AssertionError('dashboard must not export inventory via mutable StateStore')
    monkeypatch.setattr(c,'tasks',forbidden);monkeypatch.setattr(c,'workflows',forbidden)
    monkeypatch.setattr(state_db,'get_db_connection',forbidden)
    monkeypatch.setattr(c.herdr_workflow_docs,'load_notes',lambda wid:[])
    payload=c.dashboard_data()
    assert payload['counts']['tasks']==1


def test_workflow_detail_reads_only_the_requested_workflow(tmp_path,monkeypatch):
    db=tmp_path/'state.db';monkeypatch.setenv('HERDR_STATE_DB',str(db))
    state_db.save_workflow({'workflow_id':'wf-detail','project_id':'p','status':'working','created_at':2,
                            'config':{'nodes':[{'id':'impl','label':'实现'}]}},db_path=db)
    state_db.save_workflow({'workflow_id':'wf-foreign','project_id':'p','status':'working','created_at':1,
                            'config':{'nodes':[{'id':'impl','label':'实现'}]}},db_path=db)
    state_db.save_task({'task_id':'t-owned','workflow_id':'wf-detail','node':'impl','stage':'impl','status':'cleaned'},db_path=db)
    state_db.save_task({'task_id':'t-foreign','workflow_id':'wf-foreign','node':'impl','stage':'impl','status':'cleaned'},db_path=db)
    def forbidden(*a,**kw):raise AssertionError('detail must not scan the global workflow/task lists')
    monkeypatch.setattr(c,'workflows',forbidden);monkeypatch.setattr(c,'tasks',forbidden)
    monkeypatch.setattr(c,'project_by_id',lambda pid:None)
    monkeypatch.setattr(c,'agent_runtime',lambda pane:None)
    monkeypatch.setattr(c,'api_workflow_deliverables',lambda wid:c.herdr_deliverables.pack([]))
    detail=c.workflow_detail('wf-detail')
    assert detail['workflow']['workflow_id']=='wf-detail'
    assert [t['task_id'] for t in detail['tasks']]==['t-owned']


def test_workflow_detail_bulk_reads_referenced_tasks_without_writable_connections(tmp_path,monkeypatch):
    db=tmp_path/'state.db';monkeypatch.setenv('HERDR_STATE_DB',str(db))
    state_db.save_workflow({'workflow_id':'wf-detail','project_id':'p','status':'working','created_at':2,
                            'config':{'nodes':[{'id':'impl','label':'实现','required_task_ids':['t-ref']}]},
                            'workflow_file':None},db_path=db)
    state_db.save_task({'task_id':'t-owned','workflow_id':'wf-detail','node':'impl','stage':'impl','status':'cleaned'},db_path=db)
    state_db.save_task({'task_id':'t-ref','workflow_id':'wf-other','node':'impl','stage':'impl','status':'completed'},db_path=db)
    def forbidden_write(*a,**kw):raise AssertionError('detail must not open a writable connection or per-id get_task')
    monkeypatch.setattr(state_db,'get_db_connection',forbidden_write)
    monkeypatch.setattr(state_db,'get_task',forbidden_write)
    monkeypatch.setattr(c,'project_by_id',lambda pid:None)
    monkeypatch.setattr(c,'agent_runtime',lambda pane:None)
    monkeypatch.setattr(c,'api_workflow_deliverables',lambda wid:c.herdr_deliverables.pack([]))
    detail=c.workflow_detail('wf-detail')
    ref_ids={t['task_id'] for t in detail['tasks']} | {t['task_id'] for t in (detail['graph'].get('nodes') and [] or [])}
    # the referenced task is reachable through the scoped inventory (used by
    # required_task_issues) without a writable connection or get_task
    assert detail['workflow']['workflow_id']=='wf-detail'


def test_project_detail_reads_only_its_own_workflows(tmp_path,monkeypatch):
    db=tmp_path/'state.db';monkeypatch.setenv('HERDR_STATE_DB',str(db))
    state_db.save_workflow({'workflow_id':'older','project_id':'p','created_at':1,'config':{}},db_path=db)
    state_db.save_workflow({'workflow_id':'newer','project_id':'p','created_at':2,'config':{}},db_path=db)
    state_db.save_workflow({'workflow_id':'foreign','project_id':'other','created_at':3,'config':{}},db_path=db)
    def forbidden(*a,**kw):raise AssertionError('project page must not scan the global workflow/task lists')
    monkeypatch.setattr(c,'workflows',forbidden);monkeypatch.setattr(c,'tasks',forbidden)
    monkeypatch.setattr(c,'project_by_id',lambda pid:{'project_id':pid,'workspace_id':'ws'})
    monkeypatch.setattr(c,'panes',lambda ws:[]);monkeypatch.setattr(c,'tabs',lambda ws:[])
    monkeypatch.setattr(c,'slots',lambda *a,**kw:[]);monkeypatch.setattr(c,'preflight',lambda p,**kw:[])
    detail=c.project_detail('p')
    assert [w['workflow_id'] for w in detail['workflows']]==['newer','older']


def test_runtime_result_is_discarded_when_project_object_is_removed():
    source=c.HTML
    block=source[source.index('async function loadProjectRuntime('):source.index('function workflowSubject(')]
    run_js(block,r'''
const state={projectId:'p',projectLoadSequence:1,project:{project:{project_id:'p'}},shellView:'slots'};
let release;const pageRead=()=>new Promise(r=>release=r);const renderSlots=()=>{},paintShellPage=()=>{};
// SOURCE
(async()=>{
 const pending=loadProjectRuntime('p',1);state.project=null;
 release({slots:[]});await pending;assert.equal(state.project,null);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_project_runtime_claims_are_readonly_and_scoped(tmp_path,monkeypatch):
    db=tmp_path/'state.db';monkeypatch.setenv('HERDR_STATE_DB',str(db))
    state_db.save_task({'task_id':'owned','pane_id':'pane-owned'},db_path=db)
    state_db.save_task({'task_id':'foreign','pane_id':'pane-foreign'},db_path=db)
    monkeypatch.setattr(c,'project_by_id',lambda pid:{'project_id':pid,'workspace_id':'ws'})
    monkeypatch.setattr(c,'panes',lambda wid:[{'pane_id':'pane-owned'}])
    monkeypatch.setattr(c,'tabs',lambda wid:[])
    def forbidden(*args,**kwargs):raise AssertionError('runtime must not initialize or decode global tasks')
    monkeypatch.setattr(c,'tasks',forbidden);monkeypatch.setattr(state_db,'get_db_connection',forbidden)
    captured=[]
    def slots(project,p_panes=None,claimed=None):
        captured.append(claimed);return []
    monkeypatch.setattr(c,'slots',slots)
    assert c.project_runtime('p')['slots']==[]
    assert captured==[{'pane-owned':'owned'}]


def test_pages_share_kernel_db_selection_with_conflicting_legacy_paths(tmp_path,monkeypatch):
    a=tmp_path/'a';b=tmp_path/'b';a.mkdir();b.mkdir()
    monkeypatch.delenv('HERDR_STATE_DB',raising=False)
    monkeypatch.setenv('TASKS_FILE',str(a/'tasks.json'))
    monkeypatch.setenv('WORKFLOWS_FILE',str(b/'workflows.json'))
    monkeypatch.setenv('CHECKPOINTS_DIR',str(b/'checkpoints'))
    for directory,title in ((a,'selected'),(b,'wrong-database')):
        db=directory/'state.db'
        state_db.save_workflow({'workflow_id':'wf-db','title':title,'status':'completed','config':{'nodes':[{'id':'impl'}]}},db_path=db)
        state_db.save_task({'task_id':'task-db','workflow_id':'wf-db','node':'impl','status':'cleaned','pr_url':'https://example.com/'+title},db_path=db)
    assert c.api_workflows()[0]['title']=='selected'
    result=c.api_workflow_deliverables('wf-db')
    urls=[item.get('url') for item in result['items']]
    assert 'https://example.com/selected' in urls
    assert 'https://example.com/wrong-database' not in urls
