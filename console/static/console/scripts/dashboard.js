/* Dashboard scope selection, rendering and its refresh lifecycle. */
var {
  stopDashTimer, toggleLinearSelectMenu, closeLinearSelectMenu, togglePopoverGroup,
  handleLinearSelectSearch, handleLinearSelectKeydown, selectLinearWorkflow, showDashboard,
  exitDashboard, loadDashboard, dashSelectWorkflow, dashOpenWorkflowOf,
  dashOpenWorkflow, dashSignoff, dashExec, renderDashboard
} = (function({
  state, saveViewState, syncOpsUi,
  api, pageRead, toast,
  esc, jsArg, refreshAll,
  loadWorkflow, paintCrumb, setWorkspaceMode,
  openSignoffChamber, openControllerCockpit
}){

let dashTimer=null;
let dashNavigationSequence=0;
function stopDashTimer(){if(dashTimer){clearInterval(dashTimer);dashTimer=null}}
let dashLinearKeyboardIdx = 0;

function toggleLinearSelectMenu(e){
  if(e) e.stopPropagation();
  const sel=document.getElementById('dashLinearSelect');
  if(!sel) return;
  const isOpen = sel.classList.toggle('open');
  if(isOpen){
    state.dashLinearSearch = '';
    dashLinearKeyboardIdx = 0;
    const inp = document.getElementById('dashLinearSearch');
    if(inp){ inp.value = ''; setTimeout(() => inp.focus(), 50); }
    renderLinearPopoverList();
  }
}

function closeLinearSelectMenu(){
  const sel=document.getElementById('dashLinearSelect');
  if(sel) sel.classList.remove('open');
}

function togglePopoverGroup(key, e){
  if(e) e.stopPropagation();
  if(!state.collapsedGroups) state.collapsedGroups = new Set();
  if(state.collapsedGroups.has(key)) state.collapsedGroups.delete(key);
  else state.collapsedGroups.add(key);
  renderLinearPopoverList();
}

function handleLinearSelectSearch(val){
  state.dashLinearSearch = (val || '').trim().toLowerCase();
  dashLinearKeyboardIdx = 0;
  renderLinearPopoverList();
}

function handleLinearSelectKeydown(e){
  const listEl=document.getElementById('dashLinearList');
  if(!listEl) return;
  const items = Array.from(listEl.querySelectorAll('.popover-item')).filter(it => !it.closest('.popover-group.collapsed'));
  if(!items.length) return;
  if(e.key==='ArrowDown'){
    e.preventDefault();
    dashLinearKeyboardIdx = (dashLinearKeyboardIdx + 1) % items.length;
    items.forEach((it, idx) => it.classList.toggle('keyboard-focus', idx === dashLinearKeyboardIdx));
    items[dashLinearKeyboardIdx].scrollIntoView({ block: 'nearest' });
  } else if(e.key==='ArrowUp'){
    e.preventDefault();
    dashLinearKeyboardIdx = (dashLinearKeyboardIdx - 1 + items.length) % items.length;
    items.forEach((it, idx) => it.classList.toggle('keyboard-focus', idx === dashLinearKeyboardIdx));
    items[dashLinearKeyboardIdx].scrollIntoView({ block: 'nearest' });
  } else if(e.key==='Enter'){
    e.preventDefault();
    if(items[dashLinearKeyboardIdx]) items[dashLinearKeyboardIdx].click();
  } else if(e.key==='Escape'){
    e.preventDefault();
    closeLinearSelectMenu();
    const btn = document.getElementById('dashLinearTrigger');
    if(btn) btn.focus();
  }
}

function selectLinearWorkflow(wid){
  state.dashWorkflowId = wid || null;
  const sel=document.getElementById('dashWfSel');
  if(sel) sel.value = wid || '';
  closeLinearSelectMenu();
  saveViewState();
  loadDashboard();
}

function renderLinearPopoverList(){
  const listEl = document.getElementById('dashLinearList');
  if(!listEl) return;
  const wfs = state.dashWorkflows || [];
  const cur = state.dashWorkflowId || '';
  const kw = state.dashLinearSearch || '';
  if(!state.collapsedGroups) state.collapsedGroups = new Set();

  const filtered = wfs.filter(w => {
    if(!kw) return true;
    return (w.title && w.title.toLowerCase().includes(kw)) ||
           (w.workflow_id && w.workflow_id.toLowerCase().includes(kw));
  });

  let html = '';
  if(!kw || '全部工作流'.includes(kw)){
    const isSel = !cur;
    html += `<div class="popover-item ${isSel ? 'selected' : ''}" onclick="selectLinearWorkflow('')" role="option">
      <span class="item-check"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg></span>
      <div class="item-main">
        <div class="item-header">
          <span class="item-title">全部工作流</span>
          <span class="item-badges"><span class="badge-pill nodot" style="background:var(--bg-subtle);color:var(--text-secondary)">聚合</span></span>
        </div>
        <div class="item-sub">查看所有活跃工作流的聚合看板</div>
      </div>
    </div><div style="height:1px;background:var(--border-subtle);margin:4px 0"></div>`;
  }

  if(!filtered.length){
    listEl.innerHTML = html + `<div class="popover-empty">未找到匹配的工作流 “${esc(kw)}”</div>`;
    return;
  }

  const activeGroup = filtered.filter(w => (w.active > 0 || w.attention > 0));
  const idleGroup = filtered.filter(w => (w.active === 0 && w.attention === 0));

  function renderGroup(key, label, items){
    if(!items.length) return '';
    const isCollapsed = !kw && state.collapsedGroups.has(key);
    const chevronSvg = `<svg class="popover-group-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>`;
    let gHtml = `<div class="popover-group ${isCollapsed ? 'collapsed' : ''}" data-group="${key}">
      <div class="popover-group-header" onclick="togglePopoverGroup('${key}', event)">
        <span class="popover-group-title">${label} (${items.length})</span>
        ${chevronSvg}
      </div>
      <div class="popover-group-items">`;
    items.forEach(w => {
      const isSel = (w.workflow_id === cur);
      const title = w.title || w.workflow_id;
      let pills = '';
      if(w.active > 0) pills += `<span class="badge-pill active">${w.active} 活跃</span>`;
      if(w.attention > 0) pills += `<span class="badge-pill attention">${w.attention} 需决策</span>`;
      if(!pills) pills = `<span class="badge-pill completed">已完成</span>`;

      function hl(txt){
        if(!kw || !txt) return esc(txt);
        const re = new RegExp(`(${kw.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')})`, 'gi');
        return esc(txt).replace(re, '<span class="highlight">$1</span>');
      }

      gHtml += `<div class="popover-item ${isSel ? 'selected' : ''}" onclick="selectLinearWorkflow(${jsArg(w.workflow_id)})" role="option">
        <span class="item-check"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg></span>
        <div class="item-main">
          <div class="item-header">
            <span class="item-title">${hl(title)}</span>
            <span class="item-badges">${pills}</span>
          </div>
          <div class="item-sub">
            <span class="item-id">${hl(w.workflow_id)}</span>
          </div>
        </div>
      </div>`;
    });
    gHtml += `</div></div>`;
    return gHtml;
  }

  html += renderGroup('active', '进行中 / 需决策', activeGroup);
  html += renderGroup('idle', '已完成 / 闲置', idleGroup);
  listEl.innerHTML = html;

  const renderedItems = Array.from(listEl.querySelectorAll('.popover-item')).filter(it => !it.closest('.popover-group.collapsed'));
  renderedItems.forEach((it, idx) => it.classList.toggle('keyboard-focus', idx === dashLinearKeyboardIdx));
}

function updateLinearWorkflowTrigger(workflows, curWid){
  const contentEl = document.getElementById('dashLinearTriggerContent');
  if(!contentEl) return;
  if(!curWid){
    contentEl.innerHTML = `<span class="trigger-title">全部工作流</span>`;
    return;
  }
  const cw = (workflows || []).find(w => w.workflow_id === curWid);
  if(!cw){
    contentEl.innerHTML = `<span class="trigger-title">${esc(curWid)}（已归档或未知）</span><span class="trigger-mono">${esc(curWid)}</span>`;
    return;
  }
  const title = cw.title || cw.workflow_id;
  let pills = '';
  if(cw.active > 0) pills += `<span class="badge-pill active">${cw.active} 活跃</span>`;
  if(cw.attention > 0) pills += `<span class="badge-pill attention">${cw.attention} 需决策</span>`;

  contentEl.innerHTML = `<span class="trigger-title">${esc(title)}</span>`
    + `<span class="trigger-mono">${esc(cw.workflow_id)}</span>`
    + (pills ? `<span class="trigger-pills">${pills}</span>` : '');
}

function showDashboard(){
  setWorkspaceMode('aux');
  state.dashMode=true;state.opsMode=false;state.shellView='dashboard';saveViewState();syncOpsUi();stopDashTimer();
  document.getElementById('projectTitle').textContent='我的仪表板';
  document.getElementById('workflowSubject').textContent='任务 · 工位实时 · 等你决策 · 每 10 秒自动刷新';
  document.getElementById('workflowSub').textContent='';
  const sw=document.getElementById('workflowSwitcher');
  delete sw.dataset.sig;
  sw.style.display='flex';
  sw.innerHTML='<label>工作流</label>'
    +'<div class="linear-select" id="dashLinearSelect">'
      +'<button class="linear-trigger" id="dashLinearTrigger" onclick="toggleLinearSelectMenu(event)" aria-haspopup="listbox">'
        +'<span class="trigger-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="6" y1="3" x2="6" y2="15"></line><circle cx="18" cy="6" r="3"></circle><circle cx="6" cy="18" r="3"></circle><path d="M18 9a9 9 0 0 1-9 9"></path></svg></span>'
        +'<div class="trigger-content" id="dashLinearTriggerContent"><span class="trigger-title">全部工作流</span></div>'
        +'<span class="trigger-chevron"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg></span>'
      +'</button>'
      +'<div class="linear-popover" id="dashLinearPopover" onclick="event.stopPropagation()">'
        +'<div class="popover-search-wrap">'
          +'<span class="popover-search-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg></span>'
          +'<input type="text" class="popover-search-input" id="dashLinearSearch" placeholder="搜索工作流名称、ID (如 0929 / 待办)..." autocomplete="off" oninput="handleLinearSelectSearch(this.value)" onkeydown="handleLinearSelectKeydown(event)" />'
          +'<span class="popover-shortcut">ESC</span>'
        +'</div>'
        +'<div class="popover-list" id="dashLinearList" role="listbox"></div>'
      +'</div>'
      +'<select id="dashWfSel" style="display:none" onchange="dashSelectWorkflow(this.value)"><option value="">全部工作流</option></select>'
    +'</div>'
    +'<button class="btn primary" onclick="dashOpenWorkflow()">进入该工作流</button>';
  document.getElementById('stages').innerHTML='';
  document.getElementById('attentionBanner').style.display='none';
  document.getElementById('tasks').innerHTML='<div class="empty">正在加载仪表板…</div>';
  paintCrumb();
  loadDashboard();
  dashTimer=setInterval(()=>{if(state.dashMode&&!document.hidden)loadDashboard()},10000);
}
async function exitDashboard(){
  state.dashMode=false;state.shellView='workbench';saveViewState();syncOpsUi();stopDashTimer();await refreshAll()
}
async function loadDashboard(){
  const scope=state.dashWorkflowId;
  try{
    const qs=scope?('?workflow_id='+encodeURIComponent(scope)):'';
    const d=await pageRead('dashboard','/api/dashboard'+qs);
    if(!d||!state.dashMode||state.dashWorkflowId!==scope)return;
    state.dash=d;renderDashboard();
  }catch(e){
    if(state.dashMode&&state.dashWorkflowId===scope){
      toast(e.message,true);
      const el=document.getElementById('tasks');
      if(el)el.innerHTML=`<div class="panel" style="padding:32px 20px;text-align:center;margin:16px"><div style="font-size:16px;font-weight:700;margin-bottom:8px">仪表板数据加载失败</div><div class="task-meta" style="margin-bottom:16px">${esc(e.message)}</div><button class="btn primary" onclick="loadDashboard()">点击重试</button></div>`;
    }
  }
}
function dashSelectWorkflow(v){state.dashWorkflowId=v||null;saveViewState();loadDashboard()}
async function dashOpenWorkflowOf(wid,target={}){
  if(!wid)return dashOpenWorkflow();
  const navigation=++dashNavigationSequence;
  const destination={...target,workflowId:wid};
  state.dashWorkflowId=wid;
  if(!await dashOpenWorkflow(navigation)||navigation!==dashNavigationSequence||state.workflow?.workflow?.workflow_id!==wid)return;
  return openControllerCockpit(destination);
}
async function dashOpenWorkflow(navigation=++dashNavigationSequence){
  const wid=state.dashWorkflowId;
  if(!wid)return toast('请先在下拉框选择一个工作流',true);
  const target=state.controllerTarget;
  const current=()=>navigation===dashNavigationSequence&&state.workflowId===wid&&state.controllerTarget===target&&!state.dashMode&&!state.opsMode;
  state.workflowId=wid;state.dashMode=false;state.shellView='workbench';state.dashWorkflowId=null;stopDashTimer();saveViewState();syncOpsUi();
  try{
    const workflow=await api('/api/workflow?id='+encodeURIComponent(wid)+'&lightweight=1');
    if(!workflow||!current())return false;
    await loadWorkflow(wid,workflow);
  }catch(e){if(current())toast(e.message,true);return false;}
  if(!current())return false;
  await refreshAll();
  return current();
}
function dashPillFor(t){
  const s=t.status||'';
  if(s==='cleaned'||s==='completed'||s==='committed'||s==='integrated')return '<span class="badge cleaned">已完成</span>';
  if(s==='working'||s==='dispatched'||s==='agent_done'||s==='rework')return '<span class="badge working">'+esc(t.status_text)+'</span>';
  if(s==='blocked'||s==='failed')return '<span class="badge blocked">'+esc(t.status_text)+'</span>';
  return '<span class="badge waiting">'+esc(t.status_text)+'</span>';
}
function renderDashboard(){
  const d=state.dash||{tasks:[],attention:[],deliveries:[],stuck:[],counts:{tasks:0,attention:0,deliveries:0,stuck:0},generated_at_text:'—',scope:'all',workflows:[]};
  const doneCount=d.tasks.filter(t=>['cleaned','completed','committed','integrated'].indexOf(t.status)>=0).length;
  const workT=d.tasks.filter(t=>['working','dispatched','agent_done','rework'].indexOf(t.status)>=0)[0];
  const bad=d.attention.length+d.stuck.length;
  document.getElementById('mProjects').textContent=d.counts.tasks;
  document.getElementById('mWorkflows').textContent=d.tasks.filter(t=>['working','dispatched','agent_done','rework','blocked','failed'].indexOf(t.status)>=0).length;
  document.getElementById('mAgents').textContent=d.attention.length;
  document.getElementById('mAlerts').textContent=d.stuck.length;
  const mSpans=document.querySelectorAll('.metrics .metric span');
  if(mSpans[0])mSpans[0].textContent='任务总数';if(mSpans[1])mSpans[1].textContent='活跃任务';if(mSpans[2])mSpans[2].textContent='等你决策';if(mSpans[3])mSpans[3].textContent='卡住';
  const sel=document.getElementById('dashWfSel');
  if(sel){
    const cur=state.dashWorkflowId||'';
    sel.innerHTML='<option value="">全部工作流</option>'+(d.workflows||[]).map(w=>'<option value="'+esc(w.workflow_id)+'"'+(w.workflow_id===cur?' selected':'')+'>'+esc(w.title||w.workflow_id)+'（活 '+w.active+' / 等 '+w.attention+'）</option>').join('');
    if(cur&&!(d.workflows||[]).some(w=>w.workflow_id===cur)){const o=document.createElement('option');o.value=cur;o.textContent=cur+'（已归档或未知）';o.selected=true;sel.appendChild(o)}
    sel.value=cur;
    state.dashWorkflows = d.workflows || [];
    updateLinearWorkflowTrigger(d.workflows || [], cur);
    renderLinearPopoverList();
  }
  const kpis='<div class="dash-kpis">'
    +'<div class="dash-kpi"><div class="k">任务完成</div><div class="v">'+doneCount+' <small>/ '+d.counts.tasks+'</small></div></div>'
    +'<div class="dash-kpi"><div class="k">进行中</div><div class="v" style="font-size:15px">'+esc(workT?workT.task_id:'—')+'</div><div class="s">'+esc(workT?(workT.agent+' · 工位 '+(workT.pane_id||'—')):'当前无执行中任务')+'</div></div>'
    +'<div class="dash-kpi"><div class="k">等你的问题</div><div class="v'+(d.attention.length?' warn':'')+'">'+d.attention.length+' <small>个</small></div><div class="s">默认动作已就绪</div></div>'
    +'<div class="dash-kpi"><div class="k">卡住</div><div class="v'+(d.stuck.length?' warn':'')+'">'+d.stuck.length+'</div><div class="s">'+(d.stuck.length?'需要处理':'Clear')+'</div></div>'
    +'<div class="dash-kpi"><div class="k">交付物</div><div class="v">'+d.deliveries.length+'</div><div class="s">最新有效交付</div></div></div>';
  const taskRows=d.tasks.length?d.tasks.map((t,i)=>'<div class="dash-task"><span class="n">'+(i+1)+'</span><div><div class="t">'+esc(t.task_id)+'</div><div class="d">'+esc(t.goal||t.node)+'</div><div class="m">工位 '+esc(t.pane_id||'—')+' · 执行者 '+esc(t.agent)+(t.runtime_status?(' · '+esc(t.runtime_status)):'')+' · 更新于 '+esc(t.updated_at_text)+'</div></div><div>'+dashPillFor(t)+'</div></div>').join(''):'<div class="empty">当前没有任务</div>';
  const attRows=d.attention.length?d.attention.map(a=>{
    if(a.decision_id)return '<div class="dash-q"><div class="qt"><span>待你裁决 · '+esc(a.decision_id)+'</span><span class="dash-default">'+esc(a.default_action)+'</span></div><div class="qd"><b>说明</b><span>'+esc(a.reason||a.default_action_text)+'</span></div><div class="qm">'+esc(a.workflow_id)+' · '+esc(a.node||'—')+' · '+esc(a.updated_at_text)+'</div><div class="dash-btns"><button class="btn primary" onclick="dashOpenWorkflowOf('+jsArg(a.workflow_id)+')">进入工作流裁决</button></div></div>';
    const target={nodeId:a.node||'',taskId:a.task_id||'',runId:a.run_id||'',paneId:a.pane_id||'',operationId:a.operation_id||null};
    return '<div class="dash-q"><div class="qt"><span>'+esc(a.reason||a.task_id)+'</span><span class="dash-default">默认: '+esc(a.default_action)+'</span></div><div class="qd"><b>说明</b><span>'+esc(a.default_action_text)+'</span></div><div class="qm">'+esc(a.task_id)+' · 工位 '+esc(a.pane_id||'—')+' · '+esc(a.workflow_id)+' · '+esc(a.updated_at_text)+'</div><div class="dash-btns"><button class="btn primary" onclick="dashOpenWorkflowOf('+jsArg(a.workflow_id)+','+esc(JSON.stringify(target))+')">在 Controller 中处理</button></div></div>';
  }).join(''):'<div class="dash-ok"><span class="dash-dot green"></span>没有等你的问题</div>';
  const decRows=(d.decisions||[]).length?d.decisions.map(x=>'<div class="dash-q"><div class="qt"><span>'+esc(x.decision_id)+'</span><span class="dash-default">'+esc(x.node||'—')+'</span></div><div class="qd"><b>问题</b><span>'+esc(x.question||x.title)+'</span></div>'+((x.options||[]).length?'<div class="qm">可选：'+esc((x.options||[]).join(' / '))+(x.recommended?('（建议：'+esc(x.recommended)+'）'):'')+'</div>':'')+'<div class="qm">'+esc(x.workflow_id)+' · '+esc(x.raised_at_text)+'</div><div class="dash-btns"><button class="btn primary" onclick="dashOpenWorkflowOf('+jsArg(x.workflow_id)+')">去拍板</button></div></div>').join(''):'<div class="dash-ok"><span class="dash-dot green"></span>没有待裁决项</div>';
  const dlvRows=d.deliveries.length?d.deliveries.map(x=>'<div class="dash-task"><span class="n">✓</span><div><div class="t">'+esc(x.title||x.candidate_sha)+'</div><div class="m">'+esc(x.workflow_id)+' · '+esc(x.updated_at_text)+'</div></div><div><span class="badge cleaned">交付</span></div></div>').join(''):'<div class="empty">暂无交付物</div>';
  const stuckRows=d.stuck.length?d.stuck.map(s=>'<div class="dash-task"><span class="n">!</span><div><div class="t">'+esc(s.task_id||s.workflow_id)+'</div><div class="d">'+esc(s.reason)+'</div><div class="m">'+esc(s.workflow_id)+' · '+esc(s.last_event_text||'')+'</div></div><div><span class="badge blocked">卡住</span></div></div>').join(''):'<div class="dash-ok"><span class="dash-dot green"></span>一切正常，没有卡住</div>';
  document.getElementById('tasks').innerHTML=kpis
    +'<div class="dash-layout"><div class="dash-main">'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>任务及其状态</span><span class="count">'+d.tasks.length+' / '+d.counts.tasks+'</span></div>'+taskRows+'</div>'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>最新交付物</span><span class="count">'+d.deliveries.length+' 个</span></div>'+dlvRows+'</div>'
    +'</div><div class="dash-rail">'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>待你裁决</span><span class="count">'+(d.decisions||[]).length+' 个</span></div>'+decRows+'</div>'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>等你的问题</span><span class="count">'+d.attention.length+' 个</span></div>'+attRows+'</div>'
    +'<div class="dash-sec"><div class="dash-sec-h"><span>卡住的东西</span><span class="count">'+d.stuck.length+' 项</span></div>'+stuckRows+'</div>'
    +'</div></div>';
  document.getElementById('agents').innerHTML='<div class="empty">工位实时已合并到任务列表。</div>';
  document.getElementById('slots').innerHTML='<div class="empty">点击任务可进入详情。</div>';
  document.getElementById('alerts').innerHTML='<div class="empty">卡住事项见上方。</div>';
}
async function dashSignoff(taskId,wid,node,act){return dashOpenWorkflowOf(wid,{nodeId:node,taskId})}
async function dashExec(endpoint,payload){payload=payload||{};return dashOpenWorkflowOf(payload.workflow_id||state.dashWorkflowId,{nodeId:payload.node_id||payload.node||'',taskId:payload.task_id||'',runId:payload.run_id||'',paneId:payload.pane_id||'',operationId:payload.operation_id||null})}
return {
  stopDashTimer, toggleLinearSelectMenu, closeLinearSelectMenu, togglePopoverGroup,
  handleLinearSelectSearch, handleLinearSelectKeydown, selectLinearWorkflow, showDashboard,
  exitDashboard, loadDashboard, dashSelectWorkflow, dashOpenWorkflowOf,
  dashOpenWorkflow, dashSignoff, dashExec, renderDashboard
};
})({
  state, saveViewState, syncOpsUi,
  api, pageRead, toast,
  esc, jsArg, refreshAll,
  loadWorkflow, paintCrumb:(...args)=>paintCrumb(...args), setWorkspaceMode:(...args)=>setWorkspaceMode(...args),
  openSignoffChamber:(...args)=>openSignoffChamber(...args), openControllerCockpit:(...args)=>openControllerCockpit(...args)
});
