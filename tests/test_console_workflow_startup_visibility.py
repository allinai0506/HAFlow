import importlib.machinery
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative_path):
    path = ROOT / relative_path
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    from herdr.state_store import SQLiteStateStore, reset_state_store

    reset_state_store()
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-startup-pending", "project_id": "p1", "status": "running", "startup_ready": False, "title": "", "requirement": "用户看不到的有效运行内容", "created_at": 1_000_000.0})
    store.save_workflow({"workflow_id": "wf-startup-failed", "project_id": "p1", "status": "completed", "startup_ready": False, "outcome": "startup_failed", "failed_stage": "preflight", "startup_failure_reason": "<script>alert('x')</script>" + "x" * 5000, "title": "", "requirement": "启动失败的需求", "created_at": 1_000_001.0})
    store.save_workflow({"workflow_id": "wf-old-record", "project_id": "p1", "status": "completed", "title": "已有标题", "requirement": "普通需求", "created_at": 999_999.0})
    yield _load("console_read_api_startup", "console/read_api.py")
    reset_state_store()


def test_workflow_list_exposes_bounded_startup_fields_and_title_fallback(console):
    items = {item["workflow_id"]: item for item in console.api_workflows("p1")}
    pending = items["wf-startup-pending"]
    assert pending["startup_ready"] is False
    assert pending["outcome"] == ""
    assert pending["failed_stage"] == ""
    assert pending["startup_failure_reason"] == ""
    assert pending["title"] == ""
    assert pending["requirement_subject"] == "用户看不到的有效运行内容"
    assert items["wf-old-record"]["startup_ready"] is True
    failed = items["wf-startup-failed"]
    assert failed["outcome"] == "startup_failed"
    assert failed["failed_stage"] == "preflight"
    assert 0 < len(failed["startup_failure_reason"]) <= 2048


def test_workflow_detail_matches_list_semantics(console):
    workflow = console.workflow_detail("wf-startup-failed", lightweight=True)["workflow"]
    assert workflow["outcome"] == "startup_failed"
    assert workflow["failed_stage"] == "preflight"
    assert workflow["startup_ready"] is False
    assert 0 < len(workflow["startup_failure_reason"]) <= 2048


def _run_node(script, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    path = tmp_path / "workflow-visibility.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, (result.stderr or "") + "\n" + (result.stdout or "")


def test_presentation_workflow_labels_escape_reason_and_keep_task_labels(tmp_path):
    source = (ROOT / "console/static/console/scripts/presentation.js").read_text(encoding="utf-8")
    start = source.index("/* workflow-status:start */")
    end = source.index("/* workflow-status:end */") + len("/* workflow-status:end */")
    _run_node("const humanStatus = s => ({running:'运行中', completed:'待收尾'})[s] || s;\n" + source[start:end] + """
const cases = [[{status:'running', startup_ready:false},'启动中（尚未就绪）'],[{status:'completed',outcome:'startup_failed'},'启动失败'],[{status:'completed',outcome:'abandoned'},'已放弃'],[{status:'running',startup_ready:true},'运行中']];
for (const [item, expected] of cases) if (workflowStatusLabel(item) !== expected) process.exit(1);
if (workflowStatusLabel({status:'completed'}) !== '待收尾') process.exit(2);
const hint=workflowStartupHint({workflow_id:'wf-x',status:'completed',outcome:'startup_failed',failed_stage:'<x>',startup_failure_reason:'<script>x</script>'});
if (!hint.includes('<script>x</script>') || hint.includes('&lt;')) process.exit(3);
if (!hint.includes('herdr-task close-workflow wf-x --abandon')) process.exit(4);
if (humanStatus('completed') !== '待收尾') process.exit(5);
""", tmp_path)


def test_simple_workflow_renderer_uses_workflow_status_and_escapes_hint(tmp_path):
    source = (ROOT / "console/static/simple.js").read_text(encoding="utf-8")
    start = source.index("/* workflow-status:start */")
    end = source.index("/* workflow-status:end */") + len("/* workflow-status:end */")
    _run_node(source[start:end] + """
if (workflowLabel({status:'running',startup_ready:false}) !== '启动中（尚未就绪）') process.exit(1);
if (workflowLabel({status:'completed',outcome:'startup_failed'}) !== '启动失败') process.exit(2);
if (workflowLabel({status:'completed',outcome:'abandoned'}) !== '已放弃') process.exit(3);
const hint=workflowHint({workflow_id:'wf-y',status:'completed',outcome:'startup_failed',startup_failure_reason:'<img src=x>'});
if (!hint.includes('<img src=x>') || hint.includes('&lt;')) process.exit(4);
if (!hint.includes('herdr-task close-workflow wf-y --abandon')) process.exit(5);
""", tmp_path)


def test_sidebar_workflow_renderer_uses_workflow_status(tmp_path):
    source = (ROOT / "console/static/console/scripts/sidebar-tabs.js").read_text(encoding="utf-8")
    start = source.index("function renderWorkflowTabs(){")
    end = source.index("function setWorkflowTabsRenderer", start)
    _run_node("""
const box={innerHTML:'',dataset:{}};
const document={getElementById:id=>id==='workflowTabsList'?box:null};
const state={openWorkflowTabIds:['wf-x'],workflowId:'wf-x',project:{workflows:[{workflow_id:'wf-x',status:'completed',outcome:'abandoned',title:'<unsafe>'}]}};
const workflowSubject=w=>w.title||w.requirement_subject||'';
const esc=v=>String(v).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
const workflowStatusLabel=w=>w.outcome==='abandoned'?'已放弃':w.status;
const workflowStatusTone=w=>w.outcome==='abandoned'?'danger':'completed';
const workflowStartupHint=w=>'';
""" + source[start:end] + """
renderWorkflowTabs();
if (!box.innerHTML.includes('已放弃')) process.exit(1);
if (box.innerHTML.includes('<unsafe>')) process.exit(2);
""", tmp_path)


_CONSOLE_SCRIPTS = [
    "console/static/console/scripts/presentation.js",
    "console/static/console/scripts/transport.js",
    "console/static/console/scripts/dialogs.js",
    "console/static/console/scripts/workspace.js",
    "console/static/console/scripts/workflow-reader.js",
    "console/static/console/scripts/creation.js",
    "console/static/console/scripts/template-library.js",
    "console/static/console/scripts/template-editor.js",
    "console/static/console/scripts/operations.js",
    "console/static/console/scripts/dashboard.js",
    "console/static/console/scripts/sidebar-tabs.js",
    "console/static/console/scripts/task-board.js",
    "console/static/console/scripts/shell.js",
    "console/static/console/scripts/experience.js",
    "console/static/console/scripts/flow-canvas.js",
]


def _compose_console_scripts():
    return "\n".join((ROOT / relative).read_text(encoding="utf-8") for relative in _CONSOLE_SCRIPTS)


_DOM_PRELUDE = r"""
function makeEl(tag){
  const el={tag:tag||'div',children:[],style:{},dataset:{},hidden:false,value:'',open:false,className:'',disabled:false,clientWidth:800,clientHeight:480};
  el.classList={add(){},remove(){},toggle(){return false;},contains(){return false;}};
  let html='',text='',eid='';
  Object.defineProperty(el,'innerHTML',{get(){return html;},set(v){html=String(v??'');if(/<script/i.test(html))globalThis.__xss=true;}});
  Object.defineProperty(el,'textContent',{get(){return text;},set(v){text=v==null?'':String(v);}});
  Object.defineProperty(el,'id',{get(){return eid;},set(v){if(eid&&els[eid]===el)delete els[eid];eid=String(v||'');if(eid)els[eid]=el;}});
  el.append=(...n)=>{el.children.push(...n);};
  el.appendChild=(n)=>{el.children.push(n);return n;};
  el.replaceChildren=(...n)=>{el.children=[...n];};
  el.add=(option)=>{(el.options||(el.options=[])).push(option);};
  el.showModal=()=>{el.open=true;};
  el.close=()=>{el.open=false;};
  el.focus=()=>{};
  el.click=()=>{if(el.onclick)return el.onclick();};
  el.contains=()=>false;
  el.closest=()=>null;
  el.querySelector=()=>makeEl('q');
  el.querySelectorAll=()=>[];
  el.addEventListener=()=>{};
  el.setAttribute=(k,v)=>{el[k]=v;};
  el.getAttribute=(k)=>el[k];
  el.removeAttribute=()=>{};
  el.getBoundingClientRect=()=>({width:800,height:480,top:0,left:0});
  return el;
}
const els={};
const document={
  hidden:false,
  getElementById(id){return els[id]||(els[id]=Object.assign(makeEl(id),{id:id}));},
  createElement(tag){return makeEl(tag);},
  querySelector(){return null;},
  querySelectorAll(){return [];},
  addEventListener(){},
  activeElement:{closest(){return null;}}
};
const window={location:{search:''},addEventListener(){},getSelection(){return{toString(){return '';}};}};
const localStorage={_s:{},getItem(k){return this._s[k]??null;},setItem(k,v){this._s[k]=String(v);},removeItem(k){delete this._s[k];}};
const sessionStorage={_s:{},getItem(k){return this._s[k]??null;},setItem(k,v){this._s[k]=String(v);},removeItem(k){delete this._s[k];}};
const history={replaceState(){}};
const location={search:''};
const performance={now(){return 1;}};
function Option(text,value){this.text=text;this.value=value;}
function currentRecovery(){return [];}
function recoveryUnavailable(){return false;}
function dispatchConfirmationHtml(){return '';}
function recoveryCard(){return '';}
function renderDeliverables(){}
function workflowDeliverables(){return [];}
function deliverableNoteHtml(){return '';}
function deliverableRowHtml(){return '';}
function openTaskDrawer(){}
function openSignoffChamber(){}
function showPane(){}
globalThis.__xss=undefined;
"""


def test_professional_console_real_script_init_renders_startup_states(tmp_path):
    """Execute the real include order through flow-canvas. No injected status fakes."""
    script = _DOM_PRELUDE + """
let state={overview:null,project:null,workflow:null,ops:null,dash:null,projectId:null,workflowId:null,openWorkflowTabIds:[],sidebarCollapsed:false,completedExpanded:false,completedVisibleLimit:5,spaceId:null,space:null,templateSelections:{},opsMode:false,dashMode:false,dashWorkflowId:null,shellView:'workbench',taskFilter:'all',drawerTab:'tty',selectedTaskId:null,taskDrawerTab:'overview',taskTrace:null,taskTraceSeq:0,taskTracePoll:null,selectedTaskDetail:null,workflowView:'flow',flowSelectedNodeId:null,flowInspectorTab:'summary',flowGraph:null,flowGraphWfId:null,flowHtmlReady:false};
const VIEW_KEY='herdrConsoleView';
""" + _compose_console_scripts() + r"""
const failures=[];
const check=(ok,msg)=>{if(!ok)failures.push(msg);};
const payload='<script>alert(1)</script> &';
check(typeof renderWorkflowTabs==='function','renderWorkflowTabs missing');
check(typeof renderSidebarWorkflows==='function','renderSidebarWorkflows missing');
check(humanStatus('completed')==='待收尾','task humanStatus changed');
const workflows=[
  {workflow_id:'wf-pending',status:'running',startup_ready:false,title:'待启动'},
  {workflow_id:'wf-failed',status:'completed',outcome:'startup_failed',startup_ready:false,failed_stage:'preflight',startup_failure_reason:payload,title:'失败项'},
  {workflow_id:'wf-abandoned',status:'completed',outcome:'abandoned',startup_ready:false,failed_stage:'plan',startup_failure_reason:'停了',title:'放弃项'},
  {workflow_id:'wf-done',status:'completed',startup_ready:true,outcome:'',title:'正常完成'},
  {workflow_id:'wf-run',status:'running',startup_ready:true,title:'运行中'}
];
state.project={workflows};
state.openWorkflowTabIds=workflows.map(w=>w.workflow_id);
state.workflowId='wf-failed';
state.completedExpanded=true;
renderSidebarWorkflows();
const side=document.getElementById('sidebarWorkflowGroups').innerHTML;
const sig=document.getElementById('sidebarWorkflowGroups').dataset.sig||'';
check(side.includes('启动中（尚未就绪）'),'sidebar pending label');
check(side.includes('启动失败'),'sidebar failed label');
check(side.includes('已放弃'),'sidebar abandoned label');
check(side.includes('待收尾'),'sidebar completed label');
check(side.includes('运行中'),'sidebar running label');
check(!side.includes('>就绪<'),'sidebar ready meta');
check(!side.includes('>已完成<'),'sidebar completed meta');
check(side.includes('status-dot danger')||side.includes('tab-dot danger')||side.includes('danger'),'sidebar danger tone');
check(side.includes('warning'),'sidebar warning tone');
check(sig.includes('false')&&sig.includes('startup_failed'),'listSig startup fields: '+sig);
renderWorkflowTabs();
const tabs=document.getElementById('workflowTabsList').innerHTML;
check(tabs.includes('启动失败')&&tabs.includes('已放弃')&&tabs.includes('启动中（尚未就绪）'),'tabs labels');
check(tabs.includes('&lt;script&gt;')&&!tabs.includes('&amp;lt;')&&!tabs.includes('<script'),'tabs escape once');
check(!globalThis.__xss,'xss from tabs');
state.workflow={workflow:workflows[1],tasks:[{task_id:'t-done',status:'completed',node:'plan'}]};
state.shellView='workbench';state.opsMode=false;state.dashMode=false;
renderTasks();
const banner=document.getElementById('attentionBanner').innerHTML;
const taskHtml=document.getElementById('tasks').innerHTML;
check(banner.includes('启动失败')&&!banner.includes('已交付'),'startup banner: '+banner.slice(0,180));
check(taskHtml.includes('待收尾'),'task badge changed');
state.workflow={workflow:{workflow_id:'wf-del',status:'completed',outcome:'delivered',startup_ready:true},tasks:[]};
renderTasks();
check(document.getElementById('attentionBanner').innerHTML.includes('已交付'),'delivered banner');
function opsText(card){
  state.ops={boss:{},anomalies:[],agent_fleet:[],workflow_cards:[card]};
  filterOpsWorkflows('all');
  return document.getElementById('tasks').innerHTML;
}
const failedOps=opsText({workflow_id:'wf-failed',workflow_label:'失败项',status:'completed',outcome:'startup_failed',startup_ready:false,failed_stage:'preflight',startup_failure_reason:payload,tasks:{active:0,completed:0,blocked:0,failed:0},nodes:[{status:'completed',node_label:'计划'}]});
check(failedOps.includes('启动失败')&&!failedOps.includes('>已完成<'),'ops startup_failed');
const pendingOps=opsText({workflow_id:'wf-pending',workflow_label:'待启动',status:'running',startup_ready:false,tasks:{active:0,completed:0,blocked:0,failed:0},nodes:[]});
check(pendingOps.includes('启动中（尚未就绪）')&&!pendingOps.includes('>就绪<'),'ops startup_pending');
const plainOps=opsText({workflow_id:'wf-plain',workflow_label:'普通',tasks:{active:0,completed:1,blocked:0,failed:0},nodes:[{status:'completed',node_label:'收尾'}]});
check(plainOps.includes('已完成'),'ops plain completed');
state.dash={tasks:[{task_id:'t-done',status:'completed',status_text:'执行完成',goal:'g',node:'n',agent:'a',updated_at_text:'t'}],attention:[],deliveries:[],stuck:[],decisions:[],counts:{tasks:1,attention:0,deliveries:0,stuck:0},generated_at_text:'—',scope:'all',workflows:[
  {workflow_id:'wf-failed',title:'失败项',active:0,attention:0,status:'completed',outcome:'startup_failed',startup_ready:false},
  {workflow_id:'wf-abandoned',title:'放弃项',active:0,attention:0,status:'completed',outcome:'abandoned',startup_ready:false},
  {workflow_id:'wf-pending',title:'待启动',active:0,attention:0,status:'running',startup_ready:false},
  {workflow_id:'wf-done',title:'正常完成',active:0,attention:0,status:'completed',startup_ready:true},
  {workflow_id:'wf-run',title:'运行中',active:0,attention:0,status:'running',startup_ready:true},
  {workflow_id:'wf-idle',title:'无字段',active:0,attention:0}
]};
renderDashboard();
const pop=document.getElementById('dashLinearList').innerHTML;
const dashTasks=document.getElementById('tasks').innerHTML;
check(pop.includes('启动失败')&&pop.includes('已放弃')&&pop.includes('启动中（尚未就绪）'),'dashboard startup pills');
check(pop.includes('待收尾')&&pop.includes('运行中')&&pop.includes('已完成'),'dashboard normal pills');
check(dashTasks.includes('已完成'),'dashboard task pill');
state.dashMode=false;state.opsMode=false;state.shellView='workbench';state.workflowView='flow';
state.controllerActionsData=null;
state.workflow={workflow:workflows[1],graph:{nodes:[],edges:[]},tasks:[]};
renderFlowWorkbench();
const ticker=document.getElementById('bottomTicker').textContent;
const summary=document.getElementById('flowSummary').innerHTML;
check(summary.includes('启动失败'),'flow summary failed');
check(document.getElementById('flowSummary').style.display!=='none','flow summary hidden');
check(ticker.includes(payload)&&ticker.includes('preflight')&&ticker.includes('herdr-task close-workflow wf-failed --abandon'),'flow ticker raw');
check(!ticker.includes('&lt;')&&!ticker.includes('工作流调度就绪'),'flow ticker escaped or ready');
state.workflow={workflow:workflows[2],graph:{nodes:[],edges:[]},tasks:[]};
renderFlowWorkbench();
check(document.getElementById('flowSummary').innerHTML.includes('已放弃'),'flow abandoned');
state.workflow={workflow:workflows[0],graph:{nodes:[],edges:[]},tasks:[]};
renderFlowWorkbench();
check(document.getElementById('flowSummary').innerHTML.includes('启动中（尚未就绪）'),'flow pending');
state.workflow={workflow:workflows[4],graph:{nodes:[{id:'n1',status:'working',label:'实现'}],edges:[]},tasks:[]};
renderFlowWorkbench();
check(document.getElementById('bottomTicker').textContent.includes('工作流调度就绪'),'running ticker');
check(document.getElementById('flowSummary').innerHTML.includes('运行中'),'flow running');
check(!globalThis.__xss,'xss flag');
if(failures.length){console.error(failures.join('\n'));process.exit(1);}
console.log('VISIBILITY_OK');
"""
    _run_node(script, tmp_path)


def test_simple_workbench_record_button_and_raw_notice(tmp_path):
    source = (ROOT / "console/static/simple.js").read_text(encoding="utf-8")
    script = _DOM_PRELUDE + "const realSetTimeout=setTimeout; setInterval=()=>0; globalThis.fetch=async()=>({ok:true,json:async()=>({ok:true,data:[]})});\n" + source + r"""
realSetTimeout(async()=>{
  const failures=[];
  const check=(ok,msg)=>{if(!ok)failures.push(msg);};
  const payload='<script>alert(1)</script> &';
  const rows=[
    {workflow_id:'wf-failed',project_id:'p1',status:'completed',outcome:'startup_failed',startup_ready:false,failed_stage:'preflight',startup_failure_reason:payload,title:'失败项',tasks:[{task_id:'t-done',status:'completed',node_label:'实现'}],stages:[]},
    {workflow_id:'wf-abandoned',project_id:'p1',status:'completed',outcome:'abandoned',startup_ready:false,failed_stage:'plan',startup_failure_reason:'停了',title:'放弃项',tasks:[],stages:[]},
    {workflow_id:'wf-done',project_id:'p1',status:'completed',startup_ready:true,outcome:'delivered',title:'正常完成',tasks:[],stages:[]},
    {workflow_id:'wf-run',project_id:'p1',status:'running',startup_ready:true,title:'运行中',tasks:[],stages:[]}
  ];
  const byId=Object.fromEntries(rows.map(w=>[w.workflow_id,{workflow:w,tasks:w.tasks,stages:w.stages,project:{project_id:'p1'}}]));
  globalThis.fetch=async(url)=>{
    const u=String(url);
    const data=u.startsWith('/api/projects')?[]:u.includes('/api/workflow/decisions')?{decisions:[]}:u.includes('/api/workflow/deliverables')?{items:[]}:u.includes('/api/workflows')?[]:u.includes('/api/workflow')?byId[new URL(u,'http://local').searchParams.get('id')]:{};
    return {ok:true,json:async()=>({ok:true,data})};
  };
  model.projectId='p1';
  renderWorkflows(rows);
  const buttons=[...document.getElementById('requestList').children].map(row=>row.children[2]);
  const expect=[['查看记录','record'],['查看记录','record'],['查看成果','results'],['查看进度','progress']];
  buttons.forEach((button,i)=>{
    check(button.textContent===expect[i][0],'button '+i+' text '+button.textContent);
    check(button.dataset.action===expect[i][1],'button '+i+' action '+(button.dataset.action||''));
  });
  document.getElementById('results').hidden=true;
  await buttons[0].onclick();
  const notice=document.getElementById('notice').textContent;
  const record=document.getElementById('startupRecord');
  check(document.getElementById('progressDialog').open===true,'failed dialog');
  check(document.getElementById('progressTitle').textContent==='启动记录','failed title');
  check(document.getElementById('results').hidden===true,'failed results hidden');
  check(record&&record.textContent.includes(payload)&&record.textContent.split(payload).length-1===1,'record raw once');
  check(record&&record.textContent.includes('preflight')&&record.textContent.includes('herdr-task close-workflow wf-failed --abandon'),'record command');
  check(notice.includes(payload)&&notice.split(payload).length-1===1&&!notice.includes('&lt;'),'notice raw once: '+notice);
  check(!globalThis.__xss,'xss');
  check(document.getElementById('tasks').innerHTML.includes('执行完成，待收尾'),'simple task label');
  document.getElementById('progressDialog').close();
  await buttons[1].onclick();
  const abandoned=document.getElementById('startupRecord').textContent;
  check(document.getElementById('progressTitle').textContent==='启动记录','abandoned title');
  check(abandoned.includes('plan')&&abandoned.includes('herdr-task close-workflow wf-abandoned --abandon'),'abandoned record');
  await buttons[2].onclick();
  check(document.getElementById('results').hidden===false,'results shown');
  check(document.getElementById('progressDialog').open!==true,'results skips dialog');
  document.getElementById('results').hidden=true;
  await buttons[3].onclick();
  check(document.getElementById('progressTitle').textContent==='需求进度','progress title');
  check(document.getElementById('progressDialog').open===true,'progress dialog');
  check(document.getElementById('startupRecord').hidden===true,'record hidden for progress');
  if(failures.length){console.error(failures.join('\n'));process.exit(1);}
  console.log('SIMPLE_OK');
},0);
"""
    _run_node(script, tmp_path)


def test_stage_summary_and_ops_overlay_honor_startup_outcome():
    inventory = _load("console_inventory_startup_visibility", "console/inventory.py")
    failed = {"status": "completed", "outcome": "startup_failed", "startup_ready": False}
    assert inventory.stage_summary([], "plan", workflow=failed)["status"] == "waiting"
    abandoned = {"status": "completed", "outcome": "abandoned", "startup_ready": False}
    tasks = [{"task_id": "t1", "stage": "plan", "status": "failed"}]
    assert inventory.stage_summary(tasks, "plan", workflow=abandoned)["status"] == "failed"
    pending = {"status": "running", "startup_ready": False}
    assert inventory.stage_summary([], "implementation", workflow=pending)["status"] == "waiting"
    normal = {"status": "completed", "startup_ready": True}
    assert inventory.stage_summary([], "wrapup", workflow=normal)["status"] == "cleaned"
    assert inventory.stage_summary([], "wrapup", workflow={"status": "completed"})["status"] == "cleaned"
    running = {"status": "running", "startup_ready": True}
    assert inventory.stage_summary([], "plan", workflow=running)["status"] == "waiting"
    snap = {
        "status": "completed",
        "outcome": "startup_failed",
        "startup_ready": False,
        "failed_stage": "preflight",
        "startup_failure_reason": "x" * 3000,
        "config": {"secret": "no"},
    }
    overlaid = inventory._overlay_startup_fields({"workflow_id": "wf-x", "tasks": {}}, snap)
    assert overlaid["outcome"] == "startup_failed"
    assert overlaid["startup_ready"] is False
    assert overlaid["failed_stage"] == "preflight"
    assert len(overlaid["startup_failure_reason"]) == 2048
    assert "config" not in overlaid
    kept = inventory._overlay_startup_fields({"status": "running", "outcome": "kept"}, snap)
    assert kept["status"] == "running"
    assert kept["outcome"] == "kept"
