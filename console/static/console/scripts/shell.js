/* Shell navigation, breadcrumbs and resource views. */
var {
  toggleSpaceMenu, closeSpaceMenu, paintCrumb, showShellView,
  paintShellPage, openShellWorkflow
} = (function({
  state, saveViewState, syncOpsUi,
  toast, esc, relationText,
  visibleAlerts, showTemplateLibrary, showOpsCenter,
  stopDashTimer, showDashboard, refreshAll,
  selectSpace, loadProjectRuntime, workflowDisplayName,
  loadWorkflow, renderSidebarWorkflows, renderWorkflowTabs,
  renderStages, renderTasks, renderShellExperience,
  setWorkspaceMode, resizeFlowGraph, fitFlowGraph,
  renderFlowWorkbench, renderFlowGraph, renderAgents,
  renderSlots
}){


function toggleSpaceMenu(ev){if(ev)ev.stopPropagation();const m=document.getElementById('spaceMenu');if(m)m.hidden=!m.hidden;}
function closeSpaceMenu(){const m=document.getElementById('spaceMenu');if(m)m.hidden=true;}
function paintCrumb(){
  const sec=document.getElementById('crumbSection');
  const view=state.opsMode?'ops':state.dashMode?'dashboard':(state.shellView||'workbench');
  const map={workbench:'工作台',dashboard:'仪表板',ops:'运维',alerts:'告警',agents:'执行者',slots:'工位',workflows:'工作流',experience:'企业经验',templates:'模板规范'};
  if(sec)sec.textContent=map[view]||'工作台';
  const tabsBar=document.getElementById('workflowTabsBar');
  const crumbEl=document.querySelector('.top .crumb');
  if(tabsBar&&tabsBar.style)tabsBar.style.display=(view==='workbench'?'flex':'none');
  if(crumbEl&&crumbEl.classList)crumbEl.classList.toggle('visually-hidden', view==='workbench');
  document.querySelectorAll('[data-nav]').forEach(el=>el.classList&&el.classList.toggle('on', el.getAttribute('data-nav')===view));
  const ctlBtn=document.getElementById('navController');
  const tplBtn=document.getElementById('navTemplates');
  const arcBtn=document.getElementById('navArchive');
  const logBtn=document.getElementById('sidebarLogsItem');
  if(ctlBtn&&ctlBtn.classList)ctlBtn.classList.toggle('on',state.workflowId==='__ctl__');
  if(tplBtn&&tplBtn.classList)tplBtn.classList.toggle('on',state.workflowId==='__templates__');
  if(arcBtn&&arcBtn.classList)arcBtn.classList.toggle('on',state.workflowId==='__archive__');
  if(logBtn&&logBtn.classList)logBtn.classList.toggle('on',state.workflowId==='__logs__');
  const railActiveId = (view === 'templates' || (view === 'workbench' && state.workflowId === '__templates__')) ? 'templates' : view;
  document.querySelectorAll('.rail-item[data-rail-nav]').forEach(el=>el.classList&&el.classList.toggle('active', el.getAttribute('data-rail-nav')===railActiveId));
  const shell=document.querySelector('.shell');
  if(shell&&shell.dataset)shell.dataset.view=view;
  const s=state.space;
  if(s){
    const label=s.label||s.project_name||s.workspace_id||'空间';
    const name=document.getElementById('spaceName');
    const meta=document.getElementById('spaceMeta');
    const av=document.getElementById('spaceAvatar');
    const rav=document.getElementById('railSpaceAvatar');
    const foot=document.getElementById('sideFootTitle');
    if(name)name.textContent=label;
    if(meta)meta.textContent=relationText(s);
    if(av)av.textContent=String(label).slice(0,1)||'空';
    if(rav)rav.textContent=String(label).slice(0,1)||'共';
    if(foot)foot.textContent=label+' · '+relationText(s);
  }
  const o=state.overview||{};
  const setCount=(id,n)=>{const el=document.getElementById(id);if(!el)return;const v=Number(n)||0;el.textContent=String(v);el.hidden=v<=0;};
  const totalWfCount=((state.project&&state.project.workflows)||[]).length;
  setCount('navWfCount', totalWfCount);
  const wfTotalEl=document.getElementById('sidebarWfTotalCount');
  if(wfTotalEl)wfTotalEl.textContent=String(totalWfCount);
  setCount('navAgentCount', o.active_agents);
  const alertCnt=(typeof visibleAlerts==='function'?visibleAlerts():[]).length;
  setCount('navAlertCount', alertCnt);
  const railAlert=document.getElementById('railAlertDot');
  if(railAlert){if(railAlert.style)railAlert.style.display=alertCnt>0?'block':'none';railAlert.hidden=alertCnt<=0;}
  const bw=document.getElementById('bottomActiveWorkers');
  if(bw)bw.textContent=String(o.active_agents||o.active_workers||0);
  const slotsCount=(state.project&&(state.project.slots||[]).length)||18;
  const activeAgents=o.active_agents||0;
  const resCountEl=document.getElementById('sidebarResourceCounts');
  if(resCountEl)resCountEl.textContent=`${activeAgents}/${slotsCount}`;
  const onlinePill=document.getElementById('sidebarOnlinePill');
  if(onlinePill){
    onlinePill.textContent=`${activeAgents} 在线`;
    if(onlinePill.classList)onlinePill.classList.toggle('is-idle',!activeAgents);
  }
  const fleetNames=document.getElementById('sidebarFleetNames');
  if(fleetNames){
    const agents=(state.project&&state.project.agents)||[];
    const names=agents.slice(0,4).map(a=>a.name||a.agent||a.id).filter(Boolean);
    fleetNames.textContent=names.length?names.join(', '):'Claude, OpenCode, Codex, Human';
  }
  const panesSummary=document.getElementById('sidebarPanesSummary');
  if(panesSummary){
    panesSummary.textContent=`${slotsCount} 个物理工位 (${activeAgents} 运行中)`;
  }
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
}
async function showShellView(view){
  if(view==='dashboard'){state.shellView='dashboard';return showDashboard();}
  if(view==='ops'){state.shellView='ops';return showOpsCenter();}
  if(view==='templates'){return showTemplateLibrary();}
  const leaving=!!(state.opsMode||state.dashMode);
  state.opsMode=false;state.dashMode=false;stopDashTimer();
  state.shellView=view||'workbench';
  saveViewState();syncOpsUi();
  if(['slots','workbench'].includes(state.shellView)&&state.project?.runtime_pending)loadProjectRuntime(state.projectId,state.projectLoadSequence);
  if(leaving){if(state.shellView!=='workbench')paintShellPage(state.shellView);await refreshAll();return;}
  if(state.shellView==='workbench'){
    if(!state.workflow){
      if(state.space&&state.space.relation&&state.space.relation!=='current_factory'){
        await selectSpace(state.space.workspace_id,false);
        return;
      }
      setWorkspaceMode('aux');
      const empty=document.getElementById('tasks');
      if(empty)empty.innerHTML='<div class="empty">暂无任务</div>';
      paintCrumb();
      return;
    }
    renderStages();
    setWorkspaceMode(state.workflowView||'flow');
    paintCrumb();
    if((state.workflowView||'flow')==='list')renderTasks();
    else{
      renderFlowWorkbench();
      if(!state.flowGraph)renderFlowGraph();
      requestAnimationFrame(()=>{resizeFlowGraph();fitFlowGraph();});
    }
    return;
  }
  paintShellPage(state.shellView);
}
function paintShellPage(view){
  if(view==='templates'){showTemplateLibrary();return;}
  setWorkspaceMode('aux');
  paintCrumb();
  if(['alerts','agents','slots','workflows'].includes(view)&&!state.overview){
    document.getElementById('tasks').innerHTML='<div class="empty" role="status">正在读取页面数据…</div>';return;
  }
  if(view==='alerts')renderShellAlerts();
  else if(view==='agents')renderShellAgents();
  else if(view==='slots')renderShellSlots();
  else if(view==='workflows')renderShellWorkflows();
  else if(view==='experience')renderShellExperience();
}
function renderShellAlerts(){
  const el=document.getElementById('tasks');if(!el)return;
  const src=document.getElementById('alerts');
  const body=src&&src.innerHTML.trim()?src.innerHTML:'<div class="empty">当前项目 / 工作流暂无告警</div>';
  el.innerHTML='<div class="shell-page"><h3>告警</h3>'+body+'</div>';
}
function renderShellAgents(){
  const el=document.getElementById('tasks');if(!el)return;
  if(!state.project){el.innerHTML='<div class="shell-page"><h3>执行者</h3><div class="empty">当前空间暂无执行者</div></div>';return;}
  renderAgents();
  const src=document.getElementById('agents');
  el.innerHTML='<div class="shell-page"><h3>执行者</h3>'+(src?src.innerHTML:'')+'</div>';
}
function renderShellSlots(){
  const el=document.getElementById('tasks');if(!el)return;
  if(!state.project){el.innerHTML='<div class="shell-page"><h3>工位</h3><div class="empty">当前空间暂无工位</div></div>';return;}
  renderSlots();
  const src=document.getElementById('slots');
  el.innerHTML='<div class="shell-page"><h3>工位</h3>'+(src?src.innerHTML:'')+'</div>';
}
function renderShellWorkflows(){
  const el=document.getElementById('tasks');if(!el)return;
  const ws=(state.project&&state.project.workflows)||[];
  el.innerHTML='<div class="shell-page"><h3>工作流</h3>'+(ws.length?ws.map(w=>`<button class="shell-row" onclick="openShellWorkflow('${esc(w.workflow_id)}')"><strong>${esc(workflowDisplayName(w))}</strong><small>${esc(w.workflow_id)}</small></button>`).join(''):'<div class="empty">当前空间暂无工作流</div>')+'</div>';
}
async function openShellWorkflow(id){
  state.shellView='workbench';state.workflowView='flow';saveViewState();
  if(state.project?.runtime_pending)loadProjectRuntime(state.projectId,state.projectLoadSequence);
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(id&&!state.openWorkflowTabIds.includes(id))state.openWorkflowTabIds.push(id);
  try{await loadWorkflow(id);}catch(e){toast(e.message,true);return;}
  paintCrumb();
  renderWorkflowTabs();
  requestAnimationFrame(()=>{resizeFlowGraph();fitFlowGraph();});
}
return {
  toggleSpaceMenu, closeSpaceMenu, paintCrumb, showShellView,
  paintShellPage, openShellWorkflow
};
})({
  state, saveViewState, syncOpsUi,
  toast, esc, relationText,
  visibleAlerts, showTemplateLibrary, showOpsCenter,
  stopDashTimer, showDashboard, refreshAll,
  selectSpace, loadProjectRuntime, workflowDisplayName,
  loadWorkflow, renderSidebarWorkflows, renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args),
  renderStages, renderTasks, renderShellExperience:(...args)=>renderShellExperience(...args),
  setWorkspaceMode:(...args)=>setWorkspaceMode(...args), resizeFlowGraph:(...args)=>resizeFlowGraph(...args), fitFlowGraph:(...args)=>fitFlowGraph(...args),
  renderFlowWorkbench:(...args)=>renderFlowWorkbench(...args), renderFlowGraph:(...args)=>renderFlowGraph(...args), renderAgents,
  renderSlots
});
