/* Selected workflow reads, request coalescing and projection identity checks. */
var {
  workflowSubject, workflowDisplayName, copyWorkflowId, editWorkflowTitle,
  renderWorkflowSwitcher, loadWorkflow, loadControllerCockpit, clearWorkflow
} = (function({
  state, toast, showPromptModal,
  api, renderWorkflowTabs, renderSidebarWorkflows,
  esc, paintCrumb, saveViewState,
  renderStages, renderTasks, destroyFlowGraph,
  openControllerCockpitModal, renderFlowWorkbench, renderDeliverables,
  renderNodeInspector, setWorkspaceMode
}){
function workflowSubject(w){return (w&&(w.title||w.requirement_subject))||''}
function workflowDisplayName(w){
  if(!w)return '';
  const subj=workflowSubject(w);
  return subj?`${subj} (${w.workflow_id})`:w.workflow_id;
}
function copyWorkflowId(wid){
  const id=wid||state.workflowId;
  if(!id||id.startsWith('__'))return;
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(id).then(()=>{
      toast('已复制工作流编号: '+id);
    }).catch(()=>{
      toast('复制失败，请手动复制: '+id, true);
    });
  }else{
    toast('工作流编号: '+id);
  }
}
async function editWorkflowTitle(wid){
  const id=wid||state.workflowId;
  if(!id||id.startsWith('__'))return toast('当前工作流不可重命名',true);
  const w=(state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id===id)?state.workflow.workflow:((state.project&&state.project.workflows)||[]).find(x=>x.workflow_id===id);
  const curTitle=w?(w.title||w.requirement_subject||''):'';
  showPromptModal({
    title:'修改工作流名称',
    label:'工作流名称 / 业务主题',
    defaultValue:curTitle,
    confirmText:'保存',
    onConfirm:async(newTitle)=>{
      const trimmed=(newTitle||'').trim();
      if(trimmed===curTitle)return;
      try{
        await api('/api/workflow/rename',{
          method:'POST',
          body:JSON.stringify({workflow_id:id,title:trimmed})
        });
        toast('工作流名称已更新');
        if(state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id===id){
          state.workflow.workflow.title=trimmed;
          state.workflow.workflow.requirement_subject=trimmed;
        }
        if(state.project&&state.project.workflows){
          const target=state.project.workflows.find(x=>x.workflow_id===id);
          if(target){
            target.title=trimmed;
            target.requirement_subject=trimmed;
          }
        }
        if(state.workflow&&state.workflow.workflow&&state.workflow.workflow.workflow_id===id){
          renderWorkflowHead(state.workflow.workflow);
        }
        renderWorkflowTabs();
        if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
      }catch(err){
        toast('更新工作流名称失败: '+(err.message||err),true);
      }
    }
  });
}
function renderWorkflowMetaBadge(w){
  const box=document.getElementById('canvasWfMeta');
  if(!box)return;
  if(!w||!w.workflow_id||String(w.workflow_id).startsWith('__')){
    box.style.display='none';
    box.innerHTML='';
    return;
  }
  const wid=w.workflow_id;
  const subj=workflowSubject(w)||wid;
  box.style.display='inline-flex';
  box.innerHTML=`
    <div class="canvas-wf-title-wrap">
      <span class="canvas-wf-title" onclick="editWorkflowTitle('${esc(wid)}')" title="点击修改工作流名称">${esc(subj)}</span>
      <button class="canvas-wf-edit-btn" onclick="editWorkflowTitle('${esc(wid)}')" title="修改工作流名称" aria-label="修改工作流名称">
        <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
        </svg>
      </button>
    </div>
    <span class="canvas-wf-id-pill" onclick="copyWorkflowId('${esc(wid)}')" title="点击复制工作流编号 (${esc(wid)})">
      <span>${esc(wid)}</span>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>
    </span>
  `;
}
function renderWorkflowHead(w){
  const subj=workflowSubject(w);
  document.getElementById('workflowSubject').textContent=subj||w.workflow_id;
  const a=state.workflow.agent_override||'auto';
  const parts=[];
  parts.push('工作流 '+w.workflow_id);
  parts.push('执行者 '+(a==='auto'?'自动分配':a));
  if(w.candidate_branch)parts.push('候选分支 '+w.candidate_branch);
  document.getElementById('workflowSub').textContent=parts.join(' · ');
  renderWorkflowMetaBadge(w);
  paintCrumb();
}
function renderWorkflowSwitcher(){
  const box=document.getElementById('workflowSwitcher'),ws=(state.project&&state.project.workflows)||[];
  const sig=state.workflowId+'#'+ws.map(x=>x.workflow_id+':'+workflowDisplayName(x)).join('|');
  if(box.dataset.sig===sig){box.style.display=ws.length<2?'none':'flex';return}
  box.dataset.sig=sig;
  if(ws.length<2){box.style.display='none';box.innerHTML='';return}
  box.style.display='flex';
  box.innerHTML='<label>工作流</label><select id="wfSelect" onchange="state.workflowId=this.value;loadWorkflow(this.value)">'+ws.map(x=>`<option value="${esc(x.workflow_id)}"${x.workflow_id===state.workflowId?' selected':''}>${esc(workflowDisplayName(x))}</option>`).join('')+'</select>'
}
async function loadWorkflow(id,prefetched=null){
  const prevWfId=state.flowGraphWfId||state.workflowId;
  const loadSequence=state.workflowLoadSequence=(state.workflowLoadSequence||0)+1;
  const refreshingController=state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===id;
  if(!refreshingController)state.workflowId=id;
  state.flowSelectedNodeId=null;
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(id&&!state.openWorkflowTabIds.includes(id))state.openWorkflowTabIds.push(id);
  const wfRes=prefetched||await api('/api/workflow?id='+encodeURIComponent(id)+'&lightweight=1');
  if(loadSequence!==state.workflowLoadSequence)return;
  const keepController=state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===id;
  state.workflow=wfRes;
  if(state.controllerCockpit?.workflow?.workflow_id!==id){state.controllerCockpit=null;state.controllerReceipt=null;}
  state.controllerActionsData={pending:true};
  state.decisionData={pending:true};
  const w=state.workflow.workflow;
  saveViewState();
  renderWorkflowTabs();
  renderWorkflowHead(w);
  if(state.shellView==='workbench'||!state.shellView){
    renderStages();
    renderTasks();
    if(prevWfId!==id){
      destroyFlowGraph();
    }
    if(keepController)openControllerCockpitModal();else renderFlowWorkbench();
  }
  if(typeof renderDeliverables==='function')renderDeliverables();
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
  state.workflowRenderedAt=performance.now();
  loadWorkflowExtras(id,loadSequence);
}
const workflowExtraRequests=new Map();
function workflowExtraRequest(path,sequence){
  const key=path.split('?')[0];
  const previous=workflowExtraRequests.get(key);
  if(previous&&previous.path===path){previous.sequence=sequence;return previous.promise;}
  const entry={path,sequence};
  entry.promise=(previous?previous.promise.catch(()=>{}):Promise.resolve()).then(()=>{
    if(entry.sequence!==state.workflowLoadSequence)throw new Error('工作流已切换');
    return api(path);
  }).finally(()=>{if(workflowExtraRequests.get(key)===entry)workflowExtraRequests.delete(key);});
  workflowExtraRequests.set(key,entry);
  return entry.promise;
}
async function loadControllerCockpit(workflowId){
  const sequence=state.controllerCockpitSeq=(state.controllerCockpitSeq||0)+1;
  const current=()=>sequence===state.controllerCockpitSeq&&(state.workflowId===workflowId||(state.workflowId==='__ctl__'&&(state.controllerTarget?.workflowId||state.workflow?.workflow?.workflow_id)===workflowId));
  try{
    const data=await api('/api/workflow/controller-cockpit?workflow_id='+encodeURIComponent(workflowId));
    if(!current())return;
    if(data.workflow?.workflow_id!==workflowId)throw new Error('工作流身份不匹配');
    state.controllerCockpit=data;
    const receipt=state.controllerReceipt;
    if(receipt?.workflow_id===workflowId&&receipt.status==='accepted_waiting'){
      const updated=receipt.operation_id!=null&&(data.receipts||[]).find(r=>r.action_id===receipt.action_id&&r.operation_id===receipt.operation_id);
      if(updated)state.controllerReceipt=updated;
      else if(receipt.operation_id!=null&&(data.recovery||[]).some(o=>o.id===receipt.operation_id&&['resolved','superseded'].includes(o.status)))state.controllerReceipt=null;
    }
  }catch(error){
    if(!current())return;
    const cached=state.controllerCockpit?.workflow?.workflow_id===workflowId?state.controllerCockpit:{nodes:[],tasks:[],actions:[],recovery:[],todos:[],receipts:[],timeline:[]};
    state.controllerCockpit={...cached,workflow:{...cached.workflow,workflow_id:workflowId},read_state:'unknown',controller:{status:'unknown'},errors:{cockpit:error.message},judgment:cached.judgment||{now:'unknown',blocked_reason:'read_unknown',next:'refresh'}};
  }
  if(state.workflowId==='__ctl__')openControllerCockpitModal();
}
function loadWorkflowExtras(id,sequence){
  loadControllerCockpit(id);
  const current=()=>sequence===state.workflowLoadSequence&&state.workflow?.workflow?.workflow_id===id&&(state.workflowId===id||(state.workflowId==='__ctl__'&&state.controllerTarget?.workflowId===id));
  const apply=(path,success,failure)=>workflowExtraRequest(path,sequence).then(data=>{
    if(current())success(data);
  }).catch(()=>{if(current())failure();});
  apply('/api/workflow/deliverables?id='+encodeURIComponent(id),data=>{
    state.workflow.deliverables=data;renderDeliverables();renderNodeInspector();
  },()=>{
    state.workflow.deliverables={items:[],counts:{},error:'成果目录读取失败'};renderDeliverables();renderNodeInspector();
  });
  apply('/api/workflow/controller-actions?workflow_id='+encodeURIComponent(id),data=>{
    state.controllerActionsData=data;if(state.workflowId!=='__ctl__'){renderTasks();renderFlowWorkbench();}
  },()=>{state.controllerActionsData=null;if(state.workflowId!=='__ctl__')renderFlowWorkbench();});
  apply('/api/workflow/decisions?workflow_id='+encodeURIComponent(id),data=>{
    state.decisionData=data.workflow_id===id?data:{error:'裁决身份不匹配'};if(state.workflowId==='__ctl__')openControllerCockpitModal();else renderTasks();
  },()=>{state.decisionData={error:'决策读取失败'};if(state.workflowId==='__ctl__')openControllerCockpitModal();else renderTasks();});
  apply('/api/workflow/runtime?id='+encodeURIComponent(id),data=>{
    state.workflow.coordinator=data.coordinator;state.workflow.coordinator_pending=false;
  },()=>{state.workflow.coordinator_pending=false;state.workflow.coordinator_error='运行状态读取失败';});
}

function clearWorkflow(){
  destroyFlowGraph();
  state.controllerCockpitSeq=(state.controllerCockpitSeq||0)+1;
  state.controllerCockpit=null;state.controllerTarget=null;state.controllerReceipt=null;
  state.workflow=null;
  state.workflowId=null;
  state.flowSelectedNodeId=null;
  saveViewState();
  renderWorkflowTabs();
  renderWorkflowMetaBadge(null);
  document.getElementById('workflowSubject').textContent='暂无工作流';
  document.getElementById('workflowSub').textContent='';
  document.getElementById('stages').innerHTML='';
  const ab=document.getElementById('attentionBanner');if(ab)ab.style.display='none';
  setWorkspaceMode('aux');
  const deliverableBody=document.getElementById('deliverablesBody');if(deliverableBody)deliverableBody.innerHTML='';
  const deliverableCount=document.getElementById('deliverablesCount');if(deliverableCount)deliverableCount.textContent='';
  document.getElementById('tasks').innerHTML='<div class="empty">暂无任务</div>';
  paintCrumb();
  if(typeof renderSidebarWorkflows==='function')renderSidebarWorkflows();
}
return {
  workflowSubject, workflowDisplayName, copyWorkflowId, editWorkflowTitle,
  renderWorkflowSwitcher, loadWorkflow, loadControllerCockpit, clearWorkflow
};
})({
  state, toast, showPromptModal,
  api, renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args), renderSidebarWorkflows:(...args)=>renderSidebarWorkflows(...args),
  esc, paintCrumb:(...args)=>paintCrumb(...args), saveViewState:(...args)=>saveViewState(...args),
  renderStages:(...args)=>renderStages(...args), renderTasks:(...args)=>renderTasks(...args), destroyFlowGraph:(...args)=>destroyFlowGraph(...args),
  openControllerCockpitModal:(...args)=>openControllerCockpitModal(...args), renderFlowWorkbench:(...args)=>renderFlowWorkbench(...args), renderDeliverables:(...args)=>renderDeliverables(...args),
  renderNodeInspector:(...args)=>renderNodeInspector(...args), setWorkspaceMode:(...args)=>setWorkspaceMode(...args)
});
