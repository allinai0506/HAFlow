/* Sidebar workflow grouping and tab lifecycle; supports the late renderer decorator. */
var {
  toggleSidebar, renderSidebarWorkflows, toggleCompletedExpanded, openWorkflowTab,
  closeWorkflowTab, renderWorkflowTabs, setWorkflowTabsRenderer
} = (function({
  state, saveViewState, toast,
  esc, showTemplateLibrary, workflowSubject,
  loadWorkflow, clearWorkflow, paintCrumb,
  setWorkspaceMode, openControllerCockpitModal, showLogs,
  showArchive, stopLogAutoRefresh
}){

function toggleSidebar(){
  state.sidebarCollapsed = !state.sidebarCollapsed;
  const sb = document.getElementById('sidebar');
  const shell = document.querySelector('.shell');
  const expandBtn = document.getElementById('btnExpandSidebar');
  if(state.sidebarCollapsed){
    if(sb)sb.classList.add('collapsed');
    if(shell)shell.classList.add('sidebar-collapsed');
    if(expandBtn)expandBtn.classList.add('visible');
    toast('侧边栏已收起 (⌘B)');
  } else {
    if(sb)sb.classList.remove('collapsed');
    if(shell)shell.classList.remove('sidebar-collapsed');
    if(expandBtn)expandBtn.classList.remove('visible');
    toast('侧边栏已展开 (⌘B)');
  }
}
function renderSidebarWorkflows(){
  const container = document.getElementById('sidebarWorkflowGroups');
  if(!container) return;
  const ws = (state.project && state.project.workflows) || [];
  const totalEl = document.getElementById('sidebarWfTotalCount');
  if(!state.project){
    if(totalEl)totalEl.textContent='正在加载';
    container.innerHTML='<div class="empty" role="status">正在读取空间工作流…</div>';return;
  }
  if(totalEl) totalEl.textContent = String(ws.length);

  if(!ws.length){
    container.innerHTML = '<div style="padding:12px 8px;font-size:11.5px;color:#8b909a;text-align:center">当前空间暂无工作流</div>';
    container.dataset.sig = '';
    return;
  }

  const listSig = JSON.stringify([
    Boolean(state.completedExpanded),
    state.completedVisibleLimit || 5,
    ws.map(w => [w.workflow_id, w.status||'waiting', w.progress||0, workflowSubject(w)||w.workflow_id])
  ]);
  if(container.dataset.sig === listSig) {
    container.querySelectorAll('.sidebar-item[data-wf-id]').forEach(el => {
      const wid = el.getAttribute('data-wf-id');
      el.classList.toggle('active', wid === state.workflowId);
    });
    return;
  }
  container.dataset.sig = listSig;

  const blocked = [];
  const running = [];
  const completed = [];

  ws.forEach(w => {
    const st = w.status || 'waiting';
    if(st === 'blocked' || st === 'decision' || st === 'failed'){
      blocked.push(w);
    } else if(st === 'completed' || st === 'cleaned' || st === 'archived'){
      completed.push(w);
    } else {
      running.push(w);
    }
  });

  let html = '';

  if(blocked.length > 0){
    html += `<div class="sidebar-subgroup-title">待拍板 / 阻塞 <span class="subgroup-badge warning">${blocked.length}</span></div>`;
    blocked.forEach(w => {
      const isActive = w.workflow_id === state.workflowId;
      const title = (typeof workflowSubject==='function'?workflowSubject(w):'') || w.title || w.workflow_id;
      html += `
        <div class="sidebar-item ${isActive ? 'active' : ''}" data-wf-id="${esc(w.workflow_id)}" onclick="openWorkflowTab('${esc(w.workflow_id)}')" title="${esc(title)} (${esc(w.workflow_id)})">
          <span class="status-dot decision"></span>
          <span class="item-text">${esc(title)}</span>
          <span class="item-meta warning-text">需决策</span>
        </div>`;
    });
  }

  if(running.length > 0){
    html += `<div class="sidebar-subgroup-title" style="${blocked.length > 0 ? 'margin-top:8px' : ''}">进行中 <span class="subgroup-badge running">${running.length}</span></div>`;
    running.forEach(w => {
      const isActive = w.workflow_id === state.workflowId;
      const title = (typeof workflowSubject==='function'?workflowSubject(w):'') || w.title || w.workflow_id;
      const meta = w.progress ? `${w.progress}%` : (w.status === 'working' ? '进行中' : '就绪');
      html += `
        <div class="sidebar-item ${isActive ? 'active' : ''}" data-wf-id="${esc(w.workflow_id)}" onclick="openWorkflowTab('${esc(w.workflow_id)}')" title="${esc(title)} (${esc(w.workflow_id)})">
          <span class="status-dot running"></span>
          <span class="item-text">${esc(title)}</span>
          <span class="item-meta tabular-nums">${esc(meta)}</span>
        </div>`;
    });
  }

  if(completed.length > 0){
    const limit = state.completedVisibleLimit || 5;
    const isExpanded = !!state.completedExpanded;
    let visibleCompleted = completed;
    let hasHidden = false;

    if(!isExpanded && completed.length > limit){
      hasHidden = true;
      visibleCompleted = completed.slice(0, limit);
      // 若当前选中的历史任务在截断范围之外，将其加入可见列表，确保选中项高亮可定位
      if(state.workflowId){
        const activeIdx = completed.findIndex(w => w.workflow_id === state.workflowId);
        if(activeIdx >= limit){
          visibleCompleted = visibleCompleted.concat([completed[activeIdx]]);
        }
      }
    }

    const titleStyle = (blocked.length > 0 || running.length > 0) ? 'margin-top:8px' : '';
    html += `
      <div class="sidebar-subgroup-title collapsible ${!isExpanded && completed.length > limit ? '' : ''}"
           style="${titleStyle}"
           onclick="toggleCompletedExpanded()"
           title="${isExpanded ? '点击折叠收起更多历史任务' : '点击展开全部历史任务'}">
        <span class="sidebar-subgroup-left">
          <span>历史完成</span>
          <span class="sidebar-subgroup-arrow">${isExpanded ? '▼' : '▶'}</span>
        </span>
        <span class="subgroup-badge completed">${completed.length}</span>
      </div>`;

    visibleCompleted.forEach(w => {
      const isActive = w.workflow_id === state.workflowId;
      const title = (typeof workflowSubject==='function'?workflowSubject(w):'') || w.title || w.workflow_id;
      html += `
        <div class="sidebar-item ${isActive ? 'active' : ''}" data-wf-id="${esc(w.workflow_id)}" onclick="openWorkflowTab('${esc(w.workflow_id)}')" title="${esc(title)} (${esc(w.workflow_id)})">
          <span class="status-dot completed"></span>
          <span class="item-text">${esc(title)}</span>
          <span class="item-meta">已完成</span>
        </div>`;
    });

    if(completed.length > limit){
      if(!isExpanded){
        const remainingCount = completed.length - visibleCompleted.length;
        html += `
          <button type="button" class="sidebar-completed-toggle" onclick="toggleCompletedExpanded()" title="展开所有历史完成任务">
            展开更多 (${remainingCount > 0 ? '剩余 ' + remainingCount + ' 项' : '全部'})
          </button>`;
      } else {
        html += `
          <button type="button" class="sidebar-completed-toggle" onclick="toggleCompletedExpanded()" title="收起超出的历史完成任务">
            收起历史任务 (仅显示前 ${limit} 项)
          </button>`;
      }
    }
  }

  container.innerHTML = html;
}
function toggleCompletedExpanded(){
  state.completedExpanded = !state.completedExpanded;
  const container = document.getElementById('sidebarWorkflowGroups');
  if(container) container.dataset.sig = '';
  renderSidebarWorkflows();
}
function openWorkflowTab(id){
  if(!id)return;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes(id))state.openWorkflowTabIds.push(id);
  state.workflowId=id;
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
  if(typeof renderWorkflowTabs==='function')renderWorkflowTabs();
  if(id==='__ctl__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('ctl');if(typeof paintCrumb==='function')paintCrumb();if(typeof openControllerCockpitModal==='function')openControllerCockpitModal();return;}
  if(id==='__templates__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('templates');if(typeof paintCrumb==='function')paintCrumb();if(typeof showTemplateLibrary==='function')showTemplateLibrary();return;}
  if(id==='__archive__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('archive');if(typeof paintCrumb==='function')paintCrumb();if(typeof showArchive==='function')showArchive();return;}
  if(id==='__logs__'){if(typeof setWorkspaceMode==='function')setWorkspaceMode('logs');if(typeof paintCrumb==='function')paintCrumb();if(typeof showLogs==='function')showLogs();return;}
  state.shellView='workbench';
  state.workflowView='flow';
  saveViewState();
  loadWorkflow(id);
}
function closeWorkflowTab(id,e){
  if(e){e.stopPropagation();if(e.preventDefault)e.preventDefault();}
  if(!state.openWorkflowTabIds)return;
  const idx=state.openWorkflowTabIds.indexOf(id);
  if(idx===-1)return;
  state.openWorkflowTabIds.splice(idx,1);
  if(id==='__ctl__'){const ctlP=document.getElementById('controllerTabView');if(ctlP)ctlP.hidden=true;}
  if(id==='__templates__'){const tplP=document.getElementById('templatesTabView');if(tplP)tplP.hidden=true;}
  if(id==='__archive__'){const arcP=document.getElementById('archiveTabView');if(arcP)arcP.hidden=true;}
  if(id==='__logs__'){const logP=document.getElementById('logsTabView');if(logP)logP.hidden=true;stopLogAutoRefresh();}
  if(state.workflowId===id){
    if(state.openWorkflowTabIds.length>0){
      const nextIdx=Math.min(idx,state.openWorkflowTabIds.length-1);
      const nextId=state.openWorkflowTabIds[nextIdx];
      openWorkflowTab(nextId);
    }else{
      state.workflowId=null;
      if(typeof setWorkspaceMode==='function')setWorkspaceMode('aux');
      clearWorkflow();
    }
  }else{
    renderWorkflowTabs();
  }
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
}
function renderWorkflowTabs(){
  const box=document.getElementById('workflowTabsList');
  if(!box)return;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(state.workflowId&&!state.openWorkflowTabIds.includes(state.workflowId)){
    state.openWorkflowTabIds.push(state.workflowId);
  }
  const ws=(state.project&&state.project.workflows)||[];
  const wfMap={};
  ws.forEach(w=>{wfMap[w.workflow_id]=w;});
  if(state.workflow&&state.workflow.workflow){
    wfMap[state.workflow.workflow.workflow_id]=state.workflow.workflow;
  }
  if(state.openWorkflowTabIds.length===0){
    box.innerHTML='<span class="no-tabs">无打开的工作流</span>';
    return;
  }
  box.innerHTML=state.openWorkflowTabIds.map(wid=>{
    const isActive=wid===state.workflowId;
    if(wid==='__ctl__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__ctl__')" title="Controller 调度与解卡控制台" role="tab" aria-selected="${isActive}">`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg><span class="tab-title">Controller 控制台</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__ctl__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    if(wid==='__templates__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__templates__')" title="工作流模板规范库" role="tab" aria-selected="${isActive}">`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg><span class="tab-title">模板规范库</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__templates__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    if(wid==='__archive__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__archive__')" title="任务归档与冷存储" role="tab" aria-selected="${isActive}">`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg><span class="tab-title">任务归档</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__archive__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    if(wid==='__logs__'){
      return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('__logs__')" title="调度审计日志" role="tab" aria-selected="${isActive}">`+
        `<svg class="tab-icon-svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/></svg><span class="tab-title">调度审计日志</span>`+
        `<button type="button" class="tab-close" onclick="closeWorkflowTab('__logs__',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
        `</div>`;
    }
    const w=wfMap[wid]||{workflow_id:wid,status:'waiting'};
    const title=workflowSubject(w)||w.title||wid;
    const st=w.status||'waiting';
    let dotClass='waiting';
    if(st==='working'||st==='running'||st==='active')dotClass='running';
    else if(st==='blocked'||st==='decision')dotClass='decision';
    else if(st==='completed'||st==='cleaned'||st==='archived')dotClass='completed';
    return `<div class="wf-tab ${isActive?'active':''}" onclick="openWorkflowTab('${esc(wid)}')" title="${esc(title)} (${esc(wid)})" role="tab" aria-selected="${isActive}">`+
      `<span class="tab-dot ${dotClass}" aria-hidden="true"></span>`+
      `<span class="tab-title">${esc(title)}</span>`+
      `<button type="button" class="tab-close" onclick="closeWorkflowTab('${esc(wid)}',event)" title="关闭页签" aria-label="关闭页签">×</button>`+
      `</div>`;
  }).join('');
}
function setWorkflowTabsRenderer(renderer){renderWorkflowTabs=renderer;}

return {
  toggleSidebar, renderSidebarWorkflows, toggleCompletedExpanded, openWorkflowTab,
  closeWorkflowTab, renderWorkflowTabs, setWorkflowTabsRenderer
};
})({
  state, saveViewState, toast,
  esc, showTemplateLibrary, workflowSubject,
  loadWorkflow, clearWorkflow, paintCrumb:(...args)=>paintCrumb(...args),
  setWorkspaceMode:(...args)=>setWorkspaceMode(...args), openControllerCockpitModal:(...args)=>openControllerCockpitModal(...args), showLogs:(...args)=>showLogs(...args),
  showArchive:(...args)=>showArchive(...args), stopLogAutoRefresh:(...args)=>stopLogAutoRefresh(...args)
});
