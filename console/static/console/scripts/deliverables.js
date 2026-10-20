/* Deliverable presentation, preview and download. */
var {
  workflowDeliverables, deliverableNoteHtml, deliverableRowHtml, renderDeliverables,
  openDeliverable, downloadDeliverable
} = (function({
  state, api, toast,
  esc, jsArg, openModal
}){

function workflowDeliverables(){
  const packed=state.workflow&&state.workflow.deliverables;
  if(packed&&Array.isArray(packed.items))return packed;
  return {items:[],counts:{present:0,missing:0,unavailable:0,verified:0,workspace:0,unconfirmed:0}};
}
function deliverableNoteHtml(packed){
  const bits=[];
  if(packed&&packed.error)bits.push('<details class="deliverables-notice"><summary>部分文件暂时无法展示</summary><div>'+esc(packed.error)+'</div></details>');
  if(packed&&packed.notice)bits.push('<div class="deliverables-notice">'+esc(packed.notice)+'</div>');
  return bits.join('');
}
function deliverableCountsText(counts){
  counts=counts||{};
  const parts=['已产生 '+(Number(counts.present)||0)+' 项'];
  if(counts.workspace)parts.push('工作区文件 '+counts.workspace+' 项');
  if(counts.historical)parts.push('历史产物 '+counts.historical+' 项');
  if(counts.missing)parts.push('未产生 '+counts.missing+' 项');
  if(counts.unconfirmed)parts.push('不可确认 '+counts.unconfirmed+' 项');
  if(counts.unavailable)parts.push('不可读取 '+counts.unavailable+' 项');
  if(counts.verified)parts.push('已验证 '+counts.verified+' 项');
  return parts.join(' · ');
}
function deliverableActionsHtml(item){
  const actions=item.actions||[];
  const bits=[];
  if(actions.indexOf('view')>=0)bits.push('<button type="button" class="mini" onclick="openDeliverable('+jsArg(item.id)+','+jsArg(item.task_id)+')">查看</button>');
  if(actions.indexOf('download')>=0)bits.push('<button type="button" class="mini" onclick="downloadDeliverable('+jsArg(item.id)+','+jsArg(item.task_id)+','+jsArg(item.name)+')">下载</button>');
  if(actions.indexOf('open')>=0&&item.url)bits.push('<a class="mini" href="'+esc(item.url)+'" target="_blank" rel="noopener noreferrer">打开</a>');
  if(bits.length)return bits.join('');
  return '<span class="muted">'+esc(item.reason||(item.status==='missing'?'尚未生成':'当前不能打开'))+'</span>';
}
function deliverableRowHtml(item){
  const meta=[item.type_label||'文件'];
  if(item.node_label)meta.push(item.node_label);
  if(item.agent)meta.push(item.agent);
  if(item.kind==='git_commit'&&item.sha)meta.push(String(item.sha).slice(0,12));
  meta.push(item.status_label||'');
  let extra='';
  if(item.kind==='git_diff'&&Array.isArray(item.files)&&item.files.length){
    const shown=item.files.slice(0,8).join('、');
    const more=item.files_changed>item.files.length?(' 等 '+item.files_changed+' 个文件'):'';
    extra='<div class="deliverable-meta">'+esc(shown+more)+'</div>';
  }
  return '<div class="deliverable-row"><div class="deliverable-main"><div class="deliverable-name">'+esc(item.name||'成果')+'</div><div class="deliverable-meta">'+esc(meta.filter(Boolean).join(' · '))+'</div>'+extra+'</div><div class="deliverable-actions">'+deliverableActionsHtml(item)+'</div></div>';
}
function renderDeliverables(){
  const panel=document.getElementById('deliverablesPanel');
  const body=document.getElementById('deliverablesBody');
  const count=document.getElementById('deliverablesCount');
  if(!panel||!body)return;
  const mode=(document.querySelector('.shell')||{}).dataset?document.querySelector('.shell').dataset.workspace:'';
  if(!state.workflow||(mode!=='flow'&&mode!=='list')){panel.hidden=true;return;}
  panel.hidden=false;
  const packed=workflowDeliverables();
  if(packed.pending){
    if(count)count.textContent='正在加载';
    body.innerHTML='<div class="empty" role="status">正在读取交付成果…</div>';return;
  }
  if(count)count.textContent=deliverableCountsText(packed.counts);
  const note=deliverableNoteHtml(packed);
  if(!packed.items.length){
    body.innerHTML=note||'<div class="empty">这次工作流还没有交付成果。文件写进任务工作区后，会显示在这里。</div>';
    return;
  }
  body.innerHTML=note+packed.items.map(deliverableRowHtml).join('');
}
async function openDeliverable(id, taskId){
  try{
    const wid=state.workflowId||'';
    const data=await api('/api/workflow/deliverable?workflow_id='+encodeURIComponent(wid)+'&task_id='+encodeURIComponent(taskId||'')+'&id='+encodeURIComponent(id||''));
    const note=data.truncated?'内容较长，这里只显示开头部分。':'';
    const preview=data.html?'<div class="deliverable-preview">'+data.html+'</div>':'<pre>'+esc(data.text||data.reason||'没有可显示的内容')+'</pre>';
    openModal(data.name||'成果', (note?'<div class="muted" style="margin-bottom:8px">'+esc(note)+'</div>':'')+preview);
  }catch(e){
    toast(e.message||'无法查看这个成果', true);
  }
}
async function downloadDeliverable(id, taskId, name){
  try{
    const wid=state.workflowId||'';
    const url='/api/workflow/deliverable?workflow_id='+encodeURIComponent(wid)+'&task_id='+encodeURIComponent(taskId||'')+'&id='+encodeURIComponent(id||'')+'&download=1';
    const response=await fetch(url);
    if(!response.ok){
      let message='无法下载这个成果';
      try{const payload=await response.json(); if(payload&&payload.error)message=payload.error;}catch(err){}
      throw new Error(message);
    }
    const blob=await response.blob();
    const link=document.createElement('a');
    link.href=URL.createObjectURL(blob);
    link.download=name||'download';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function(){URL.revokeObjectURL(link.href);},1000);
  }catch(e){
    toast(e.message||'无法下载这个成果', true);
  }
}
return {
  workflowDeliverables, deliverableNoteHtml, deliverableRowHtml, renderDeliverables,
  openDeliverable, downloadDeliverable
};
})({
  state, api, toast,
  esc, jsArg, openModal
});
