/* Page navigation, project hydration and view restoration. */
var {
  saveViewState, loadViewState, syncOpsUi, openWorkflowFromOps,
  refreshAll, selectSpace, loadProject, loadProjectRuntime
} = (function({
  state, VIEW_KEY, exitOpsCenter,
  showOpsCenter, exitDashboard, showDashboard,
  closeMoreMenu, stopOpsTimer, stopDashTimer,
  loadWorkflow, toast, startOpsTimer,
  loadOpsCenter, loadDashboard, pageRead,
  paintShellPage, paintCrumb, visibleAlerts,
  esc, relationClass, relationText,
  taskDisplayName, badge, closeSpaceMenu,
  destroyFlowGraph, setWorkspaceMode, populateTemplateSelect,
  showTemplateLibrary, renderAgents, renderSlots,
  renderWorkflowSwitcher, renderWorkflowTabs, openControllerCockpit,
  showArchive, showLogs, clearWorkflow
}){

function syncUrlView(){
  try{
    const u=new URL(window.location.href);
    const v=state.opsMode?'ops':state.dashMode?'dashboard':(state.shellView||'workbench');
    if(v==='workbench'){
      u.searchParams.delete('view');
      u.searchParams.delete('ops');
      if(state.workflowId&&state.workflowId!=='__templates__'&&state.workflowId!=='__ctl__'&&state.workflowId!=='__archive__'&&state.workflowId!=='__logs__'){
        u.searchParams.set('workflow_id',state.workflowId);
      }else{
        u.searchParams.delete('workflow_id');
      }
    }else{
      u.searchParams.set('view',v);
      if(v==='dashboard'&&state.dashWorkflowId){
        u.searchParams.set('workflow_id',state.dashWorkflowId);
      }else{
        u.searchParams.delete('workflow_id');
      }
      u.searchParams.delete('ops');
    }
    const newUrl=u.pathname+u.search+u.hash;
    if(window.location.pathname+window.location.search+window.location.hash!==newUrl){
      window.history.replaceState(null,'',newUrl);
    }
  }catch(e){}
}
function saveViewState(){try{localStorage.setItem(VIEW_KEY,JSON.stringify({opsMode:state.opsMode,dashMode:state.dashMode,dashWorkflowId:state.dashWorkflowId||null,spaceId:state.spaceId,workflowId:state.workflowId,shellView:state.shellView||'workbench'}));syncUrlView();}catch(e){}}
function loadViewState(){try{return JSON.parse(localStorage.getItem(VIEW_KEY)||'null')}catch(e){return null}}
function syncOpsUi(){
  const actions=document.querySelector('.actions');
  if(!actions)return;
  let b=document.getElementById('opsButton');
  if(!b){b=document.createElement('button');b.id='opsButton';actions.prepend(b)}
  b.textContent=state.opsMode?'← 返回工厂':'进入运维驾驶舱';
  b.className=state.opsMode?'btn primary':'btn';
  b.onclick=state.opsMode?exitOpsCenter:showOpsCenter;
  let db=document.getElementById('dashButton');
  if(!db){db=document.createElement('button');db.id='dashButton';actions.prepend(db)}
  db.textContent=state.dashMode?'← 返回工厂':'我的仪表板';
  db.className=state.dashMode?'btn primary':'btn';
  db.onclick=state.dashMode?exitDashboard:showDashboard;
  let oh=document.getElementById('opsHeaderActions');
  if(!oh){
    oh=document.createElement('div');
    oh.id='opsHeaderActions';
    oh.style.display='none';
    oh.style.alignItems='center';
    oh.style.gap='8px';
    oh.innerHTML='<span id="opsTimerBadge" class="fn-pill" style="font-size:11px;padding:2px 8px;border-radius:12px;background:var(--bg-subtle);border:1px solid var(--border-subtle);color:var(--text-secondary)">自动刷新 · 10s</span><button class="btn" id="opsRefreshBtn" onclick="triggerOpsRefresh()" style="padding:4px 12px;font-size:12px">⟳ 立即刷新</button>';
    actions.appendChild(oh);
  }
  oh.style.display=state.opsMode?'inline-flex':'none';
  closeMoreMenu();
  document.querySelectorAll('.factory-action').forEach(button=>{button.hidden=state.opsMode});
  document.querySelectorAll('.factory-action').forEach(button=>{if(state.dashMode)button.hidden=true});
}
async function openWorkflowFromOps(id){
  stopOpsTimer();
  state.opsMode=false;state.dashMode=false;state.shellView='workbench';stopDashTimer();
  syncOpsUi();
  state.workflowId=id;
  saveViewState();
  try{
    await loadWorkflow(id);
    const project=(state.workflow&&state.workflow.project)||{};
    if(project.project_id)state.projectId=project.project_id;
    if(project.workspace_id)state.spaceId=project.workspace_id;
    await refreshAll()
  }catch(e){
    toast(e.message,true);
    state.opsMode=true;
    saveViewState();
    syncOpsUi();
    startOpsTimer();
    await loadOpsCenter()
  }
}

async function refreshAll(){
  const sequence=state.refreshSequence=(state.refreshSequence||0)+1;
  try{
    syncOpsUi();
    if(state.dashMode){await loadDashboard();return}
    if(state.opsMode){await loadOpsCenter();return}
    const overview=await pageRead('overview','/api/overview');
    if(!overview||sequence!==state.refreshSequence)return;
    state.overview=overview;
    if(state.opsMode){await loadOpsCenter();return}
    const ss=state.overview.spaces||[];

    if(!state.spaceId||!ss.some(s=>s.workspace_id===state.spaceId)){
      const preferred=ss.find(s=>s.relation==='current_factory')||ss[0];
      state.spaceId=preferred?preferred.workspace_id:null
    }

    renderOverview();
    if(state.spaceId)await selectSpace(state.spaceId,false);
    if(sequence!==state.refreshSequence||state.opsMode||state.dashMode)return;
    if(state.shellView==='experience')return;
    if(state.shellView&&state.shellView!=='workbench')paintShellPage(state.shellView);
    else paintCrumb();
  }catch(e){toast(e.message,true)}
}

function renderOverview(){
  const o=state.overview;
  const ss=o.spaces||[];
  const alerts=visibleAlerts();

  document.getElementById('mProjects').textContent=ss.length;
  document.getElementById('mWorkflows').textContent=o.active_workflows;
  document.getElementById('mAgents').textContent=o.active_agents;
  document.getElementById('mAlerts').textContent=alerts.length;
  const mSpans=document.querySelectorAll('.metrics .metric span');
  if(mSpans[0])mSpans[0].textContent='项目空间';if(mSpans[1])mSpans[1].textContent='活跃工作流';if(mSpans[2])mSpans[2].textContent='活跃执行者';if(mSpans[3])mSpans[3].textContent='需要关注';

  document.getElementById('projects').innerHTML=ss.length?ss.map(s=>`
    <button class="project ${s.workspace_id===state.spaceId?'active':''}" onclick="selectSpace('${esc(s.workspace_id)}')">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
        <strong>${esc(s.label||s.workspace_id)}</strong>
        <span class="${relationClass(s)}" style="font-size:11px">${esc(relationText(s))}</span>
      </div>
      <small>${esc(s.workspace_id)} · ${s.tab_count||0} 个工作流节点 · ${s.pane_count||0} 个智能体工位</small>
      <small class="project-path">${esc(s.project_root||'未识别项目目录')}</small>
    </button>`).join(''):'<div class="empty">暂无项目空间</div>';

  document.getElementById('alerts').innerHTML=alerts.length?alerts.map(a=>`
    <div class="alert-row" onclick="openTaskDrawer('${esc(a.task_id||'')}')" style="cursor:pointer">
      <div style="flex:1;min-width:0">
        <div class="task-name">${esc(taskDisplayName(a))}</div>
        <div class="task-id">${esc(a.task_id||'')}</div>
        ${a.reason?`<div class="alert-reason" style="color:var(--danger,#f43f5e);font-size:11px;margin-top:2px">${esc(a.reason)}</div>`:''}
        <div class="task-meta">${esc(a.agent||'')} · ${esc(a.pane_id||'')}</div>
      </div>
      ${badge(a.status)}
    </div>`).join(''):'<div class="empty">当前项目 / 工作流暂无告警</div>';
  paintCrumb();
}

async function selectSpace(workspaceId,rer=true){
  closeSpaceMenu();
  const s=(state.overview.spaces||[]).find(x=>x.workspace_id===workspaceId);
  if(!s)return;

  state.spaceId=workspaceId;
  state.space=s;

  if(s.relation==='current_factory'&&s.project_id){
    state.projectId=s.project_id;
    await loadProject(s.project_id,false);
    if(state.spaceId!==workspaceId||state.opsMode||state.dashMode)return;
    document.getElementById('projectTitle').textContent=s.label||s.project_name||s.workspace_id;
  }else{
    state.projectLoadSequence=(state.projectLoadSequence||0)+1;
    destroyFlowGraph();
    state.project=null;
    state.projectId=s.project_id||null;
    state.workflow=null;
    state.workflowId=null;
    state.flowSelectedNodeId=null;
    setWorkspaceMode('aux');

    document.getElementById('projectTitle').textContent=s.label||s.workspace_id;
    document.getElementById('workflowSubject').textContent=relationText(s);
    document.getElementById('workflowSub').textContent=
      s.workspace_id+
      (s.factory_workspace_id?` · 当前工厂空间：${s.factory_workspace_id}`:'');
    document.getElementById('workflowSwitcher').style.display='none';

    document.getElementById('stages').innerHTML='';
    if(s.relation==='unregistered'){
      document.getElementById('tasks').innerHTML=`
        <div style="padding:20px;background:var(--card);border:1px solid var(--line);border-radius:14px;margin:16px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color:var(--accent)"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path></svg>
            <strong style="font-size:16px">按模板装配为工厂空间</strong>
          </div>
          <div class="muted" style="font-size:13px;line-height:1.6;margin-bottom:16px">
            当前终端空间（${esc(s.workspace_id)}）为独立终端，尚未接入工厂自动化流水线。<br>
            点击下方按钮，工厂将就地为您装配 <b>1总指挥</b> 与各阶段标准工位（阶段标签页与锚点工位），将其注册为该项目的当前工厂车间。
          </div>
          <div class="form" style="max-width:540px">
            <label for="adoptPath">项目代码根目录</label>
            <input id="adoptPath" value="${esc(s.project_root||'')}" placeholder="/path/to/project">
            <label for="adoptTemplate">装配流水线模板</label>
            <select id="adoptTemplate"></select>
            <button class="btn primary" style="margin-top:8px" onclick="submitAdoptSpace('${esc(s.workspace_id)}')">一键按模板装配为工厂空间</button>
          </div>
        </div>`;
      populateTemplateSelect('adoptTemplate');
      document.getElementById('agents').innerHTML='<div class="empty">装配接入工厂后，即可配置并查看执行者阵容。</div>';
      document.getElementById('slots').innerHTML='<div class="empty">装配接入工厂后，即可调度常驻智能体工位。</div>';
    }else{
      document.getElementById('tasks').innerHTML=`
        <div class="empty">
          <div style="font-weight:650;margin-bottom:8px">${esc(relationText(s))}</div>
          <div>项目目录：${esc(s.project_root||'未识别')}</div>
          <div style="margin-top:4px">${s.tab_count||0} 个工作流节点 · ${s.pane_count||0} 个智能体工位</div>
          ${s.factory_workspace_id?`<div style="margin-top:8px">该项目当前工厂空间：${esc(s.factory_workspace_id)}</div>`:''}
          <div style="margin-top:8px">该空间仅展示，不参与当前自动工作流调度。</div>
        </div>`;
      document.getElementById('agents').innerHTML='<div class="empty">历史空间不参与当前工厂执行者阵容。</div>';
      document.getElementById('slots').innerHTML='<div class="empty">只有当前工厂空间才显示可调度的常驻智能体工位。</div>';
    }
  }

  if(rer)renderOverview();
  paintCrumb();
  saveViewState();
  if(rer&&state.shellView==='templates')await showTemplateLibrary();
}

async function loadProject(id,rer=true){
  state.projectId=id;
  const sequence=state.projectLoadSequence=(state.projectLoadSequence||0)+1;
  const project=await pageRead('project','/api/project?id='+encodeURIComponent(id)+'&lightweight=1');
  if(!project||state.projectId!==id||sequence!==state.projectLoadSequence||state.opsMode||state.dashMode)return;
  state.project=project;
  if(!state.shellView||['workbench','slots'].includes(state.shellView))loadProjectRuntime(id,sequence);
  if(rer&&state.overview)renderOverview();
  document.getElementById('projectTitle').textContent=state.project.project.project_name;
  const w=state.project.workflows;
  const isSysTab=state.workflowId&&(state.workflowId.startsWith('__')||['__ctl__','__templates__','__archive__','__logs__'].includes(state.workflowId));
  if(!isSysTab&&(!state.workflowId||!w.some(x=>x.workflow_id===state.workflowId)))state.workflowId=state.project.latest_workflow_id;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(state.workflowId&&!state.openWorkflowTabIds.includes(state.workflowId)){
    state.openWorkflowTabIds.push(state.workflowId);
  }
  renderAgents();
  renderSlots();
  renderWorkflowSwitcher();
  renderWorkflowTabs();
  if(state.shellView&&state.shellView!=='workbench'){
    return;
  }
  if(isSysTab){
    paintCrumb();
    const sysId=state.workflowId;
    if(sysId==='__ctl__'){
      const target=state.controllerTarget;
      const ownsTarget=()=>state.projectId===id&&state.projectLoadSequence===sequence&&state.controllerTarget===target;
      const anchor=state.controllerTarget?.workflowId||(state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id)||(state.project&&state.project.latest_workflow_id);
      if(anchor&&!(state.controllerActionsData&&state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id===anchor)){
        const workflow=await pageRead('project-controller-workflow','/api/workflow?id='+encodeURIComponent(anchor)+'&lightweight=1');
        if(!workflow||!ownsTarget()||state.workflowId!==sysId)return;
        if(!state.workflow||!state.workflow.workflow)state.workflow={workflow:{workflow_id:anchor}};
        await loadWorkflow(anchor,workflow);
      }
      if(anchor){
        if(!ownsTarget())return;
        if(state.workflowId!=='__ctl__'&&state.workflowId!==anchor)return;
        if(state.controllerTarget?.workflowId===anchor){state.workflowId='__ctl__';openControllerCockpit();}
        else openControllerCockpit({workflowId:anchor});
      }
    }else if(sysId==='__templates__'&&typeof showTemplateLibrary==='function')showTemplateLibrary();
    else if(sysId==='__archive__'&&typeof showArchive==='function')showArchive();
    else if(sysId==='__logs__'&&typeof showLogs==='function')showLogs();
  }else if(state.workflowId){
    if(state.initialWorkflowPainted&&state.workflow?.workflow?.workflow_id===state.workflowId){
      state.initialWorkflowPainted=false;
    }else await loadWorkflow(state.workflowId);
  }else{
    clearWorkflow();
  }
}
async function loadProjectRuntime(id,sequence){
  const project=state.project;
  if(!project)return;
  try{
    const runtime=await pageRead('project-runtime','/api/project/runtime?id='+encodeURIComponent(id));
    if(!runtime||state.project!==project||state.projectId!==id||sequence!==state.projectLoadSequence||state.opsMode||state.dashMode)return;
    Object.assign(state.project,runtime,{runtime_pending:false});
    renderSlots();
    if(state.shellView==='slots')paintShellPage('slots');
  }catch(e){
    if(state.project===project&&state.projectId===id&&sequence===state.projectLoadSequence){
      state.project.runtime_pending=false;state.project.runtime_error=e.message;renderSlots();
      if(state.shellView==='slots')paintShellPage('slots');
    }
  }
}

return {
  saveViewState, loadViewState, syncOpsUi, openWorkflowFromOps,
  refreshAll, selectSpace, loadProject, loadProjectRuntime
};
})({
  state, VIEW_KEY, exitOpsCenter:(...args)=>exitOpsCenter(...args),
  showOpsCenter:(...args)=>showOpsCenter(...args), exitDashboard:(...args)=>exitDashboard(...args), showDashboard:(...args)=>showDashboard(...args),
  closeMoreMenu, stopOpsTimer:(...args)=>stopOpsTimer(...args), stopDashTimer:(...args)=>stopDashTimer(...args),
  loadWorkflow:(...args)=>loadWorkflow(...args), toast, startOpsTimer:(...args)=>startOpsTimer(...args),
  loadOpsCenter:(...args)=>loadOpsCenter(...args), loadDashboard:(...args)=>loadDashboard(...args), pageRead,
  paintShellPage:(...args)=>paintShellPage(...args), paintCrumb:(...args)=>paintCrumb(...args), visibleAlerts,
  esc, relationClass, relationText,
  taskDisplayName, badge, closeSpaceMenu:(...args)=>closeSpaceMenu(...args),
  destroyFlowGraph:(...args)=>destroyFlowGraph(...args), setWorkspaceMode:(...args)=>setWorkspaceMode(...args), populateTemplateSelect:(...args)=>populateTemplateSelect(...args),
  showTemplateLibrary:(...args)=>showTemplateLibrary(...args), renderAgents:(...args)=>renderAgents(...args), renderSlots:(...args)=>renderSlots(...args),
  renderWorkflowSwitcher:(...args)=>renderWorkflowSwitcher(...args), renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args), openControllerCockpit:(...args)=>openControllerCockpit(...args),
  showArchive:(...args)=>showArchive(...args), showLogs:(...args)=>showLogs(...args), clearWorkflow:(...args)=>clearWorkflow(...args)
});
