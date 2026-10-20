/* Archive filtering, pagination and stale-response protection; query state stays here. */
var {
  onArchiveProjectChange, onArchiveWorkflowChange, filterArchiveByWorkflow, applyArchiveFilters,
  archivePage, showArchive
} = (function({
  state, api, pageRead,
  esc, badge, formatElapsed,
  workflowDisplayName, renderWorkflowTabs, paintCrumb,
  setWorkspaceMode
}){

const ARCHIVE_STATUS_CHOICES=[['archived','已归档'],['all','全部状态'],['active','进行中'],['cleaned','已完成'],['superseded','已取代'],['failed','失败'],['completed','待收尾']];
let archiveQuery={page:0,size:50,project_id:'',workflow_id:'',agent:'',status:'archived',q:''};
function getKnownWorkflowsForProject(pid){
  if(!pid)return null;
  if(state.project && (state.projectId===pid || (state.project.project && state.project.project.project_id===pid))){
    return state.project.workflows||[];
  }
  return null;
}
function archiveFiltersHtml(){
  const ps=(state.overview&&state.overview.projects)||[];
  const knownWfs=getKnownWorkflowsForProject(archiveQuery.project_id);
  let wfOptions='<option value="">全部工作流</option>';
  if(knownWfs && knownWfs.length){
    let found=false;
    knownWfs.forEach(w=>{
      const isSel=(archiveQuery.workflow_id===w.workflow_id);
      if(isSel)found=true;
      wfOptions+=`<option value="${esc(w.workflow_id)}"${isSel?' selected':''}>${esc(workflowDisplayName(w))}</option>`;
    });
    if(archiveQuery.workflow_id && !found){
      wfOptions+=`<option value="${esc(archiveQuery.workflow_id)}" selected>${esc(archiveQuery.workflow_id)}</option>`;
    }
  }else if(archiveQuery.workflow_id){
    wfOptions+=`<option value="${esc(archiveQuery.workflow_id)}" selected>${esc(archiveQuery.workflow_id)}</option>`;
  }
  return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:8px">`
    +`<select id="arcProject" onchange="onArchiveProjectChange()"><option value="">全部项目</option>${ps.map(p=>`<option value="${esc(p.project_id)}"${archiveQuery.project_id===p.project_id?' selected':''}>${esc(p.project_name||p.project_id)}</option>`).join('')}</select>`
    +`<select id="arcWorkflow" onchange="onArchiveWorkflowChange()">${wfOptions}</select>`
    +`<select id="arcAgent" onchange="applyArchiveFilters()"><option value="">全部执行者</option>${['opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>`<option${archiveQuery.agent===a?' selected':''}>${esc(a)}</option>`).join('')}</select>`
    +`<select id="arcStatus" onchange="applyArchiveFilters()">${ARCHIVE_STATUS_CHOICES.map(c=>`<option value="${c[0]}"${archiveQuery.status===c[0]?' selected':''}>${c[1]}</option>`).join('')}</select>`
    +`<input id="arcQ" placeholder="关键词：任务/目标/节点" value="${esc(archiveQuery.q)}" onkeydown="if(event.key==='Enter')applyArchiveFilters()">`
    +`</div><div style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:8px"><button class="btn primary" onclick="applyArchiveFilters()">查询</button></div><div id="archiveList"><div class="empty">正在加载…</div></div>`;
}
function archiveRowsHtml(d){
  const items=d.items||[];
  if(!items.length)return '<div class="empty">没有匹配的任务</div>';
  const size=Math.max(1,d.limit||50);
  const pages=Math.max(1,Math.ceil(d.total/size));
  const page=Math.floor(d.offset/size)+1;
  const rows=items.map(t=>{
    const when=t.updated_at?new Date(t.updated_at*1000).toLocaleString():'—';
    const wfId=t.workflow_id||'';
    const wfLink=wfId?`<a href="javascript:void(0)" style="color:var(--accent);text-decoration:underline;cursor:pointer" onclick="filterArchiveByWorkflow('${esc(wfId)}','${esc(t.project_id||'')}')" title="按此工作流筛选">${esc(wfId)}</a>`:'';
    return `<div class="task"><div><div class="task-name">${esc(t.node_label||t.node||'任务')} <span class="task-id">${esc(t.task_id)}</span> ${badge(t.status)}</div>`
      +`<div class="task-id">${wfLink}${t.project_name?' · '+esc(t.project_name):''} · 执行者 ${esc(t.agent||'-')}${t.stage_verdict?' · 验收 '+esc(t.stage_verdict):''}${t.superseded_by?' · 取代者 '+esc(t.superseded_by):''}</div>`
      +`<div class="task-meta">${esc((t.goal||'').slice(0,140))}</div>`
      +`<div class="task-meta">更新于 ${esc(when)} · 历时 ${esc(formatElapsed(t.duration_seconds))}</div></div>`
      +`<div class="task-actions"><button class="mini" onclick="openTaskDrawer('${esc(t.task_id)}')">详情</button></div></div>`;
  }).join('');
  return rows+`<div class="task-meta" style="padding:12px 16px;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap"><span>共 ${d.total} 条 · 第 ${page}/${pages} 页</span><span><button class="mini" onclick="archivePage(-1)"${page<=1?' style="opacity:.45;pointer-events:none"':''}>上一页</button> <button class="mini" onclick="archivePage(1)"${page>=pages?' style="opacity:.45;pointer-events:none"':''}>下一页</button></span></div>`;
}
async function populateArchiveWorkflowSelect(projectId,selectedWorkflowId){
  const sel=document.getElementById('arcWorkflow');
  if(!sel)return;
  const knownWfs=getKnownWorkflowsForProject(projectId);
  let wfs=knownWfs;
  if(!wfs){
    try{
      const url='/api/workflows'+(projectId?'?project_id='+encodeURIComponent(projectId):'');
      wfs=await api(url);
    }catch(e){wfs=[]}
  }
  let options='<option value="">全部工作流</option>';
  let found=false;
  (wfs||[]).forEach(w=>{
    const name=workflowDisplayName(w)||w.workflow_id;
    const isSel=(selectedWorkflowId && w.workflow_id===selectedWorkflowId);
    if(isSel)found=true;
    options+=`<option value="${esc(w.workflow_id)}"${isSel?' selected':''}>${esc(name)}</option>`;
  });
  if(selectedWorkflowId && !found){
    options+=`<option value="${esc(selectedWorkflowId)}" selected>${esc(selectedWorkflowId)}</option>`;
  }
  sel.innerHTML=options;
}
async function onArchiveProjectChange(){
  const pVal=document.getElementById('arcProject')?.value||'';
  archiveQuery.project_id=pVal;
  archiveQuery.workflow_id='';
  archiveQuery.page=0;
  await populateArchiveWorkflowSelect(pVal,'');
  loadArchive();
}
function onArchiveWorkflowChange(){
  archiveQuery.workflow_id=(document.getElementById('arcWorkflow')?.value||'').trim();
  archiveQuery.page=0;
  loadArchive();
}
async function filterArchiveByWorkflow(wid,pid){
  if(pid && archiveQuery.project_id!==pid){
    archiveQuery.project_id=pid;
    const pSel=document.getElementById('arcProject');
    if(pSel)pSel.value=pid;
    await populateArchiveWorkflowSelect(pid,wid);
  }else{
    const wSel=document.getElementById('arcWorkflow');
    if(wSel){
      if(!Array.from(wSel.options).some(o=>o.value===wid)){
        const opt=document.createElement('option');
        opt.value=wid;
        opt.textContent=wid;
        wSel.appendChild(opt);
      }
      wSel.value=wid;
    }
  }
  archiveQuery.workflow_id=wid;
  archiveQuery.page=0;
  loadArchive();
}
async function loadArchive(){
  const sequence=state.archiveSequence=(state.archiveSequence||0)+1;
  const list=document.getElementById('archiveList');
  if(list)list.innerHTML='<div class="empty">正在加载…</div>';
  try{
    const qs=new URLSearchParams({status:archiveQuery.status,limit:String(archiveQuery.size),offset:String(archiveQuery.page*archiveQuery.size)});
    if(archiveQuery.project_id)qs.set('project_id',archiveQuery.project_id);
    if(archiveQuery.workflow_id)qs.set('workflow_id',archiveQuery.workflow_id);
    if(archiveQuery.agent)qs.set('agent',archiveQuery.agent);
    if(archiveQuery.q)qs.set('q',archiveQuery.q);
    const d=await pageRead('archive','/api/archive?'+qs.toString());
    if(!d||sequence!==state.archiveSequence)return;
    const el=document.getElementById('archiveList');
    if(el)el.innerHTML=archiveRowsHtml(d);
  }catch(e){
    if(sequence!==state.archiveSequence)return;
    const el=document.getElementById('archiveList');
    if(el)el.innerHTML='<div class="empty">加载失败：'+esc(e.message)+'</div>';
  }
}
function applyArchiveFilters(){
  archiveQuery.project_id=document.getElementById('arcProject')?.value||'';
  archiveQuery.workflow_id=(document.getElementById('arcWorkflow')?.value||'').trim();
  archiveQuery.agent=document.getElementById('arcAgent')?.value||'';
  archiveQuery.status=document.getElementById('arcStatus')?.value||'archived';
  archiveQuery.q=(document.getElementById('arcQ')?.value||'').trim();
  archiveQuery.page=0;
  loadArchive();
}
function archivePage(delta){archiveQuery.page=Math.max(0,archiveQuery.page+delta);loadArchive()}
async function showArchive(){
  if(!state.overview){try{state.overview=await api('/api/overview')}catch(e){}}
  if(state.projectId){
    archiveQuery.project_id=state.projectId;
  }
  if(state.workflowId&&state.workflowId!=='__ctl__'&&state.workflowId!=='__templates__'&&state.workflowId!=='__archive__'&&state.workflowId!=='__logs__'){
    archiveQuery.workflow_id=state.workflowId;
  }
  archiveQuery.page=0;
  const arcEl=document.getElementById('archiveTabView');
  if(arcEl){
    arcEl.innerHTML=`<div class="ctl-panel-header"><div style="display:flex;justify-content:space-between;align-items:center;width:100%"><div><strong>任务归档与冷存储</strong><span class="muted" style="margin-left:8px;font-weight:400">· 历史已完成或已归档工作流任务检索</span></div></div></div>`+archiveFiltersHtml();
  }
  setWorkspaceMode('archive');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__archive__'))state.openWorkflowTabIds.push('__archive__');
  state.workflowId='__archive__';
  renderWorkflowTabs();
  paintCrumb();
  const knownWfs=getKnownWorkflowsForProject(archiveQuery.project_id);
  if(!knownWfs){
    await populateArchiveWorkflowSelect(archiveQuery.project_id,archiveQuery.workflow_id);
  }
  await loadArchive();
}
return {
  onArchiveProjectChange, onArchiveWorkflowChange, filterArchiveByWorkflow, applyArchiveFilters,
  archivePage, showArchive
};
})({
  state, api, pageRead,
  esc, badge, formatElapsed,
  workflowDisplayName, renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args), paintCrumb,
  setWorkspaceMode
});
