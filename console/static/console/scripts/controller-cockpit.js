/* Workflow controls, human decisions, recovery and controller action presentation. */
var {
  showAgentOverride, saveAgentOverride, openSignoffChamber, submitSignoffDecision,
  forceReviewTask, retryStageAdvance, toggleDeepDrawer, switchDrawerTab,
  refreshDeepDrawer, showSteerModal, submitSteer, haltTaskPrompt,
  toggleWorkflowPause, stepWorkflow, showRollbackModal, executeRollback,
  forcePassTask, showCheckpointsModal, createWorkflowCheckpoint, restoreWorkflowCheckpoint,
  createCandidate, openDecisionPanel,
  submitDecision, openControllerCockpitModal, currentRecovery, dispatchConfirmationHtml,
  recoveryUnavailable, recoveryCard, openRecoveryDecision, decideRecovery,
  advanceStage, openControllerCockpit, returnFromControllerCockpit, renderControllerReceipt, executeCockpitAction
} = (function({
  state, showConfirmModal, showPromptModal,
  api, toast, esc,
  humanStatus, humanReason, badge,
  cleanStageLabel, deliveryOf, deliveryReason,
  deliveryOverridesStatus, taskDisplayName, refreshAll,
  loadWorkflow, renderWorkflowTabs, flowCardFoot,
  setWorkspaceMode, renderFlowWorkbench, renderTasks, switchTaskDrawerTab, openModal, closeModal
}){

function showAgentOverride(){if(!state.workflowId)return toast('当前没有工作流',true);const cur=state.workflow.agent_override||'auto';openModal('指定后续任务执行者',`<div class="form"><label for="overrideAgent">执行者策略</label><select id="overrideAgent">${['auto','opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>`<option ${a===cur?'selected':''}>${a}</option>`).join('')}</select><button class="btn primary" onclick="saveAgentOverride()">保存</button><div class="muted">只影响后续新建任务。</div></div>`)}async function saveAgentOverride(){try{await api('/api/workflow/agent',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,agent:document.getElementById('overrideAgent').value})});closeModal();await loadWorkflow(state.workflowId);toast('执行者策略已更新')}catch(e){toast(e.message,true)}}
async function openSignoffChamber(taskId){
  const task=(state.workflow?.tasks||[]).find(t=>t.task_id===taskId)||(state.controllerCockpit?.nodes||[]).flatMap(n=>n.tasks||[]).find(t=>t.task_id===taskId);
  return openControllerCockpit({workflowId:task?.workflow_id||cockpitWorkflowId(),...(task?.node||task?.stage?{nodeId:task.node||task.stage}:{taskId})});
}
async function submitSignoffDecision(taskId,wid,node){return openControllerCockpit({workflowId:wid,nodeId:node});}
async function forceReviewTask(tid){
  try{
    toast('正在唤醒评审…');
    await api('/api/task/force-review',{method:'POST',body:JSON.stringify({task_id:tid})});
    toast('已将工位推进至 agent_done 并促醒协调器验收');
    refreshAll();
  }catch(e){toast('唤醒评审失败: '+e.message,true)}
}
async function retryStageAdvance(wid){
  try{
    toast('正在尝试推进阶段…');
    await api('/api/workflow/retry-advance',{method:'POST',body:JSON.stringify({workflow_id:wid||state.workflowId})});
    toast('已触发阶段推进');
    refreshAll();
  }catch(e){toast('推进失败: '+e.message,true)}
}
function toggleDeepDrawer(){const d=document.getElementById('deepDrawer');if(!d)return;const isCollapsed=d.classList.contains('collapsed');d.classList.toggle('collapsed');const txt=document.getElementById('drawerToggleText');if(txt)txt.textContent=isCollapsed?'▼ 折叠收起':'▲ 展开抽屉';const btxt=document.getElementById('bottomDrawerText');if(btxt)btxt.textContent=isCollapsed?'▼ 收起现场':'▲ 底层现场';if(isCollapsed)refreshDeepDrawer()}

function switchDrawerTab(tab){state.drawerTab=tab;['tty','logs','raw'].forEach(t=>{const el=document.getElementById('dtab'+t.charAt(0).toUpperCase()+t.slice(1));if(el)el.classList.toggle('active',t===tab)});refreshDeepDrawer()}
async function refreshDeepDrawer(){const pre=document.getElementById('drawerPre');if(!pre)return;pre.textContent='正在拉取底层现场数据…';try{if(state.drawerTab==='tty'){const ts=(state.workflow&&state.workflow.tasks)||[];const activeTask=ts.find(t=>['working','dispatched','rework','paused'].includes(t.status))||ts[0];const paneId=activeTask?activeTask.pane_id:(state.project&&state.project.slots&&state.project.slots[0]&&state.project.slots[0].pane_id);if(!paneId){pre.textContent='当前暂无活动工位 TTY';return}const d=await api('/api/pane/read?id='+encodeURIComponent(paneId));pre.textContent=`[工位 ${paneId} 实时终端现场]\n`+(d.output||'（工位暂无输出）')}else if(state.drawerTab==='logs'){const d=await api('/api/logs?kind=controller');pre.textContent='[调度器内核日志 Controller Log]\n'+(d.output||'（暂无日志）')}else if(state.drawerTab==='raw'){if(!state.workflowId){pre.textContent='请先选择一个工作流';return}const d=await api('/api/workflow/projection?id='+encodeURIComponent(state.workflowId));pre.textContent=JSON.stringify(d,null,2)}}catch(e){pre.textContent='拉取失败: '+e.message}}
function showSteerModal(tid){openModal('留话指导',`<div class="form"><div class="muted" style="margin-bottom:8px">任务 ID：${esc(tid)}</div><label for="steerInput">想告诉它的话（排队等它空闲时读到，不打断当前执行）</label><textarea id="steerInput" rows="3" placeholder="例如：优先使用标准库，不要引入外部第三方包" style="width:100%;box-sizing:border-box;margin-bottom:8px"></textarea><div style="display:flex;align-items:center;gap:8px;margin-bottom:12px"><input type="checkbox" id="steerUrgent" style="width:auto"><label for="steerUrgent" style="margin:0;cursor:pointer"><strong>现在就打断它，马上送进去</strong></label></div><button class="btn primary" onclick="submitSteer('${esc(tid)}')">发送指令</button></div>`)}
async function submitSteer(tid){const inst=(document.getElementById('steerInput')?.value||'').trim();const urgent=!!document.getElementById('steerUrgent')?.checked;if(!inst)return toast('请输入指令内容',true);closeModal();try{toast('正在留言…');const res=await api('/api/task/steer',{method:'POST',body:JSON.stringify({task_id:tid,instruction:inst,urgent:urgent})});if(res.ok===false){toast(res.reason==='soft_steer_not_supported'?'当前工位不支持不打断的留言，指令已保留；可选择立即打断发送。':res.reason==='unknown_agent_no_adapter_registered'?'当前工位没有可用的指令适配能力，指令已保留，请检查工位配置。':'指令未送达，请查看任务现场后重试。',true);await loadWorkflow(state.workflowId);return}toast(res.status==='dispatched'?'已经打断它并送到手上了':'已放进留言队列，它空闲时会读到');await loadWorkflow(state.workflowId)}catch(e){toast(e.message,true)}}
function haltTaskPrompt(tid){showConfirmModal({title:'紧急叫停这个任务',message:'确定要立刻中断任务 '+tid+' 吗？正在做的事会停下，但代码和现场都会保留，之后你可以决定重新派人还是作废。',confirmText:'确认叫停',danger:true,onConfirm:async()=>{try{toast('正在叫停…');await api('/api/task/halt',{method:'POST',body:JSON.stringify({task_id:tid,reason:'人工在控制台紧急叫停'})});toast('工位已安全打断');await loadWorkflow(state.workflowId)}catch(e){toast(e.message,true)}}})}
async function toggleWorkflowPause(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
async function stepWorkflow(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
function showRollbackModal(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
async function executeRollback(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
function forcePassTask(wid,nodeId,options){
  options=options||{};
  const widEsc=esc(wid||'');
  const nodeEsc=esc(nodeId||'');
  const tid=options.task_id||'';
  const tidEsc=esc(tid);
  let expVer=(options.expected_version!==undefined&&options.expected_version!==null)?options.expected_version:(options.version!==undefined&&options.version!==null?options.version:'');
  let expPane=options.expected_pane_id||options.pane_id||'';
  if(tid&&(expVer===''||!expPane)){
    const allTasks=(state.workflow&&state.workflow.tasks)||[];
    const matchedTask=allTasks.find(t=>String(t.task_id)===String(tid));
    if(matchedTask){
      if(expVer===''&&matchedTask.version!==undefined){expVer=matchedTask.version;}
      if(!expPane&&matchedTask.pane_id){expPane=matchedTask.pane_id;}
    }
  }
  openModal('人工强制放行门禁确认 (高风险)',`
    <div style="line-height:1.6">
      <div style="margin-bottom:8px"><strong>目标工作流:</strong> <code>${widEsc}</code></div>
      <div style="margin-bottom:8px"><strong>目标门禁节点:</strong> <span class="badge warning">${nodeEsc||'未指定'}</span></div>
      ${tid?`<div style="margin-bottom:8px"><strong>关联任务:</strong> <code>${tidEsc}</code></div>`:''}
      <div class="ctl-effect" style="margin:12px 0;padding:12px;background:var(--warning-bg);border:1px solid var(--warning);border-radius:8px;color:var(--text-primary)">
        <strong>⚠️ 影响范围警告:</strong><br>
        本操作为<b>人工强制豁免</b>，将直接把该节点的门禁判定标记为 <code>pass</code>，并在工作流持久化记录豁免审计凭证。<br>
        <b>此入口仅执行门禁放行，不自动推进工作流。绝不能替代自动修复，必须由工程师人工核验产物后方可放行。</b>
      </div>
      <div style="margin-top:12px">
        <label for="kernelForcePassReason" style="font-weight:600;font-size:12px;color:var(--text-primary);display:block;margin-bottom:4px">放行原因与核验结论（必填）:</label>
        <textarea id="kernelForcePassReason" class="input" style="width:100%;height:72px;box-sizing:border-box" placeholder="请详细填写人工豁免原因、核验人及结论..."></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn" onclick="closeModal()">取消</button>
        <button id="btnSubmitKernelForcePass" class="btn warning">确认强制放行</button>
      </div>
    </div>
  `);
  const btn=document.getElementById('btnSubmitKernelForcePass');
  if(btn){
    btn.onclick=async()=>{
      const el=document.getElementById('kernelForcePassReason');
      const reason=(el?el.value:'').trim();
      if(!reason){
        toast('必须填写强制放行原因',true);
        return;
      }
      btn.disabled=true;
      closeModal();
      try{
        toast('正在强制放行…');
        const body={
          workflow_id:wid,
          gate_node_id:nodeId,
          node:nodeId,
          note:reason,
          reason:reason,
          confirmed:true,
          operator:'human'
        };
        if(tid){
          body.task_id=tid;
          if(expVer!==''&&expVer!==undefined&&expVer!==null){
            body.expected_version=parseInt(expVer,10);
          }
          if(expPane){
            body.expected_pane_id=expPane;
            body.pane_id=expPane;
          }
        }else{
          const allTasks=(state.workflow&&state.workflow.tasks)||[];
          const nodeTasks=allTasks.filter(t=>(t.node===nodeId||t.stage===nodeId)&&t.status!=='superseded');
          const taskMap={};
          for(const t of nodeTasks){
            if(t&&t.task_id){
              taskMap[t.task_id]=t.version!==undefined&&t.version!==null?t.version:1;
            }
          }
          body.expected_task_versions=taskMap;
        }
        await api('/api/kernel/force-pass',{
          method:'POST',
          body:JSON.stringify(body)
        });
        await loadWorkflow(wid);
        toast('门禁已强制放行');
      }catch(e){
        toast('强制放行失败: '+e.message,true);
      }
    };
  }
}
async function showCheckpointsModal(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
async function createWorkflowCheckpoint(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
async function restoreWorkflowCheckpoint(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
async function createCandidate(){
  showConfirmModal({
    title:'创建工作流候选分支',
    message:'确定要创建/更新工作流候选分支吗？此操作将汇总实现阶段已通过验收的交付代码，但绝不会直接合入 main。',
    confirmText:'开始构建',
    onConfirm:async()=>{
      try{
        toast('正在构建候选分支…');
        const d=await api('/api/workflow/candidate',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});
        await loadWorkflow(state.workflowId);
        toast('候选分支：'+d.candidate_branch);
      }catch(e){toast(e.message,true)}
    }
  });
}
// Open decision asks must be impossible to miss: they block whole waves and
// previously only existed as free text inside the coordinator's terminal.
function renderDecisionPanel(){
  const data=state.decisionData||{};
  const decisions=data.decisions||[];
  const advice=data.advice||[];
  let html='';
  if(decisions.length){
    html+=`<div class="ctl-section-title">待你裁决 <span class="ctl-count">${decisions.length}</span></div><div class="ctl-sub">总指挥把这些问题交给你决定了。在你拍板前，相关阶段不会开工。点选项即记录，总指挥下一轮就能读到你的结论。</div>`;
    html+=decisions.map(d=>{
      const opts=(d.options||[]).map(o=>`<span class="ctl-dec-opt${o===d.recommended?' is-rec':''}">${esc(o)}${o===d.recommended?' · 建议':''}</span>`).join('');
      return `<div class="ctl-decision"><div class="ctl-dec-head"><span class="ctl-dec-id">${esc(d.decision_id)}</span><span class="ctl-chip">${esc(d.node||'—')}</span><span class="ctl-chip">${esc(d.raised_at_text||'')}</span></div><div class="ctl-dec-q">${esc(d.question||d.title||d.decision_id)}</div>${opts?`<div class="ctl-dec-opts">${opts}</div>`:''}<div class="ctl-card-foot"><button class="btn primary" onclick="openDecisionPanel('${esc(d.decision_id)}')">已拍板，记录结论</button></div></div>`;
    }).join('');
  }
  if(advice.length){
    html+=`<div class="ctl-section-title" style="margin-top:16px">总指挥最新建议 <span class="ctl-count">${advice.length}</span></div><div class="ctl-sub">来自工作流的共享记录（只追加，不会被改写），不是模型口头说的话。</div>`;
    html+=advice.map(a=>`<div class="ctl-dec-advice${a.stale?' is-stale':''}"><div class="ctl-dec-advice-h"><span class="ctl-dec-advice-kind">${esc(a.kind)}</span><span class="ctl-dec-advice-t">${esc(a.title||'—')}</span>${a.stale?'<span class="ctl-chip">已作废</span>':''}</div>${a.summary?`<div class="ctl-dec-advice-s">${esc(a.summary)}</div>`:''}</div>`).join('');
  }
  if(!html)return `<div class="ctl-empty">✓ 当前没有待你裁决的问题。<div class="muted">总指挥交给你的问题会出现在这里；定了之后会自动消失。</div></div>`;
  return `<section>${html}</section>`;
}
function openDecisionPanel(decisionId){
  const decisions=((state.decisionData||{}).decisions)||[];
  const d=decisions.find(x=>x.decision_id===decisionId);
  if(!d)return toast('该裁决项已不在待办列表',true);
  // Single quotes only: the handler lives inside a double-quoted HTML
  // attribute, so JSON.stringify's double quotes would truncate it.
  // esc() escapes ' as &#39;, so the browser decodes back to the raw value.
  const opts=(d.options||[]).map(o=>`<div class="ctl-dec-opt${o===d.recommended?' is-rec':''}" style="cursor:pointer;padding:8px 12px" onclick="document.getElementById('decChosen').value=String.fromCharCode(${Array.from(o).map(ch=>ch.charCodeAt(0)).join(',')})">${esc(o)}${o===d.recommended?' · 建议':''}</div>`).join('');
  openModal('记录裁决 · '+d.decision_id,`<div class="ctl-wrap"><div class="ctl-dec-q">${esc(d.question||d.title)}</div>${opts?`<div class="ctl-dec-opts" style="flex-direction:column;align-items:stretch">${opts}</div>`:''}<div class="form"><label for="decChosen">你的结论（可直接选上面的选项，也可自己写）</label><input id="decChosen" value="${esc(d.recommended||'')}" placeholder="例如：MATCH 入 V1，但 T3 段需先过 G1c 的 DB/EXPLAIN 门禁"><label for="decNote">补充说明（可选）</label><input id="decNote" placeholder="给总指挥的执行约束"></div><div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="submitDecision('${esc(d.decision_id)}')">落库并通知总指挥</button></div></div>`);
}
async function submitDecision(decisionId){
  const wid=state.workflowId==='__ctl__'?cockpitWorkflowId():state.workflowId;
  if(state.decisionData?.workflow_id!==wid)return toast('裁决目标已变化，请刷新',true);
  const decision=(document.getElementById('decChosen')?.value||'').trim();
  const note=(document.getElementById('decNote')?.value||'').trim();
  if(!decision)return toast('请填写你的结论',true);
  closeModal();
  try{
    toast('正在记录裁决…');
    await api('/api/workflow/decision',{method:'POST',body:JSON.stringify({workflow_id:wid,decision_id:decisionId,decision:decision+(note?('（'+note+'）'):''),operator:'human_console'})});
    toast('裁决已落库，总指挥下一轮即可读到');
    await loadWorkflow(wid);
  }catch(e){toast('记录失败: '+e.message,true)}
}
function cockpitWorkflowId(){return (state.workflowId==='__ctl__'?state.controllerTarget?.workflowId:null)||state.workflow?.workflow?.workflow_id||'';}
function cockpitArg(value){return esc(JSON.stringify(value));}
function cockpitActionKey(action){
  const ordered=value=>value&&typeof value==='object'?(Array.isArray(value)?value.map(ordered):Object.fromEntries(Object.keys(value).sort().map(key=>[key,ordered(value[key])]))):value;
  return JSON.stringify(ordered({action_id:action?.action_id,target:action?.target,expected_identity:action?.expected_identity,api_payload:action?.api_payload,automation:action?.automation,risk:action?.risk,is_destructive:action?.is_destructive,category:action?.category}));
}
function isRejectedCockpitAction(action){
  const receipt=state.controllerReceipt;
  return receipt?.status==='rejected'&&receipt.stale_action&&receipt.workflow_id===cockpitWorkflowId()&&cockpitActionKey(action)===cockpitActionKey(receipt.stale_action);
}
function renderControllerHeader(data){
  const c=data.controller||{};
  const labels={running:'运行中',stopped:'已停止',unknown:'未确认'};
  return '<header class="controller-cockpit-header"><div><div class="controller-cockpit-eyebrow">工作流驾驶舱</div><h2>'+esc(data.workflow?.title||data.workflow?.workflow_id||cockpitWorkflowId())+'</h2>'+(state.controllerReturnContext?'<button class="mini" onclick="returnFromControllerCockpit()">返回原位置</button>':'')+'</div><div>调度服务 · '+esc(labels[c.status]||c.status||'未确认')+'<div class="muted">最后检查：'+esc(c.last_check_at||c.checked_at||'未提供')+'</div><button class="mini" onclick="loadControllerCockpit('+cockpitArg(cockpitWorkflowId())+')">刷新状态</button></div></header>';
}
function renderControllerJudgment(data){
  const j=data.judgment||{};
  return '<section class="controller-cockpit-judgment" aria-label="当前判断">'+[['现在',j.now],['阻塞原因',j.blocked_reason||'无'],['下一步',j.next]].map(([label,value])=>'<div><span class="muted">'+label+'</span><strong>'+esc(value||'未确认')+'</strong></div>').join('')+'<p class="muted">'+(data.read_state!=='confirmed'?'当前判断未确认，请刷新核验':j.requires_user?'需要人工确认':'按服务端判断推进')+'</p></section>';
}
function controllerTaskMatches(task,target){
  const fields=[['taskId',task.task_id],['runId',task.run_id],['paneId',task.pane_id||task.runtime?.pane_id]];
  return fields.some(([key])=>target[key])&&fields.every(([key,value])=>!target[key]||target[key]===value);
}
function controllerActionMatches(action,target){
  if(target.operationId!=null)return false;
  const fields=[['nodeId','node_id'],['taskId','task_id'],['runId','run_id'],['paneId','pane_id']];
  return fields.every(([key,field])=>!target[key]||target[key]===action.target?.[field]);
}
function controllerRecoveryMatches(operation,target){
  if(target.operationId!=null&&String(target.operationId)!==String(operation.id))return false;
  return [['nodeId','node_id'],['taskId','task_id'],['runId','run_id'],['paneId','pane_id']].every(([key,field])=>!target[key]||target[key]===operation.payload?.[field]);
}
function renderControllerTarget(data,target){
  const fields=['nodeId','taskId','runId','paneId','operationId'].filter(key=>target[key]!=null&&target[key]!=='');
  if(!fields.length)return '';
  const available=(data.actions||[]).some(a=>controllerActionMatches(a,target))||(data.todos||[]).some(o=>o.kind==='recovery'&&controllerRecoveryMatches(o,target));
  return '<section class="controller-cockpit-target-context" tabindex="-1" role="status"><strong>'+(!available||data.read_state!=='confirmed'?'只读定位':'当前定位')+'</strong><p>'+fields.map(key=>esc(target[key])).join(' · ')+'</p>'+(!available?'<p>'+(data.read_state!=='confirmed'?'读取未确认，无法确认当前操作。':'当前投影中没有匹配的操作；该对象可能来自历史记录或未包含在本次投影中。')+'</p>':'')+'</section>';
}
function renderControllerNodes(data,target){
  return '<section class="controller-cockpit-section"><h3>节点与任务</h3><div class="controller-cockpit-nodes">'+(data.nodes||[]).map(n=>'<article class="controller-cockpit-node'+(target.nodeId===n.id?' is-target':'')+'"><div class="controller-cockpit-row"><strong>'+esc(n.label||n.id)+'</strong><span>'+esc(humanStatus(n.status)||n.status||'未确认')+'</span></div>'+((n.tasks||[]).map(t=>'<div class="controller-cockpit-task'+(controllerTaskMatches(t,target)?' is-target':'')+'"><button class="mini" onclick="locateControllerTask('+cockpitArg(t.task_id)+')">'+esc(t.task_id)+'</button><div>'+esc(humanStatus(t.status)||t.status||'未确认')+'</div><dl><dt>运行</dt><dd>'+esc(t.run_id||'未确认')+'</dd><dt>工位</dt><dd>'+esc(t.pane_id||t.runtime?.pane_id||'未确认')+'</dd><dt>执行身份</dt><dd>'+esc(t.execution_id||'未确认')+'</dd></dl></div>').join('')||'<p class="muted">未登记任务</p>')+'</article>').join('')+'</div>'+(data.nodes_truncated||data.tasks_truncated?'<p class="muted">仅显示服务端返回的部分节点 / 任务。</p>':'')+'</section>';
}
function cockpitExecutionMode(action){
  const mode=action?.automation?.mode||'blocked';
  return mode==='automatic'&&action?.execution?.mode==='manual'?'manual':mode;
}
function cockpitActionCard(a,located=false){
  const mode=cockpitExecutionMode(a);
  const labels={automatic:'自动处理（由后台执行）',manual:'手动执行（后台未注册自动处理）',confirmation_required:'需要确认',blocked:'暂不可执行'};
  const stale=isRejectedCockpitAction(a);
  return '<article class="controller-cockpit-action'+(located?' is-target':'')+(stale?' is-outdated':'')+'" tabindex="-1" data-action-id="'+esc(a.action_id)+'"><div class="controller-cockpit-row"><strong>'+esc(a.title||a.action_id)+'</strong><span>'+esc(labels[mode]||'暂不可执行')+'</span></div><p>'+esc(a.effect||a.description||'')+'</p><p class="muted">'+esc(a.automation?.reason||'')+'</p>'+(stale?'<p role="alert">此操作卡片已过期，请刷新后核对。</p>':'')+(mode==='automatic'?'<p class="muted">页面不触发自动执行；请查看后台回执。</p>':'<button class="btn '+(a.is_destructive?'danger-btn':'primary')+'" '+(!['manual','confirmation_required'].includes(mode)||stale?'disabled':'')+' onclick="executeCockpitAction('+cockpitArg(a.action_id)+')">'+(mode==='blocked'?'当前不可执行':'查看并确认')+'</button>')+'</article>';
}
function renderControllerTodos(data,target){
  const recovery=(data.todos||[]).filter(t=>t.kind==='recovery'&&controllerRecoveryMatches(t,target));
  const actions=(data.actions||[]).filter(a=>controllerActionMatches(a,target));
  const rejected=state.controllerReceipt;
  if(rejected?.status==='rejected'&&rejected.workflow_id===data.workflow?.workflow_id&&rejected.stale_action&&controllerActionMatches(rejected.stale_action,target)&&!actions.some(a=>cockpitActionKey(a)===cockpitActionKey(rejected.stale_action)))actions.push(rejected.stale_action);
  const located=['nodeId','taskId','runId','paneId','operationId'].some(key=>target[key]!=null&&target[key]!=='');
  const body=recovery.map(o=>'<div class="controller-cockpit-recovery'+(located?' is-target':'')+'" tabindex="-1">'+recoveryCard(o,data.workflow?.workflow_id)+'</div>').join('')+actions.map(a=>cockpitActionCard(a,located)).join('');
  const decisions=state.decisionData?.workflow_id===data.workflow?.workflow_id?renderDecisionPanel():'<p class="muted">人工裁决状态尚未确认，请查看工作流详情。</p>';
  return '<section class="controller-cockpit-section"><h3>调度待办 · 继续推进</h3>'+(data.read_state!=='confirmed'?'<div role="alert">读取未确认，尚不能确认是否存在待办。</div>':body||'<p class="muted">暂无待办</p>')+(data.actions_truncated||data.recovery_truncated?'<p class="muted">待办已截断，请查看对应节点的详情。</p>':'')+decisions+'</section>';
}
function renderControllerReceipt(receipt){
  if(!receipt)return '';
  const labels={applied:'已生效',accepted_waiting:'已受理，等待核验',partial:'部分生效',rejected:'已拒绝'};
  return '<article class="controller-cockpit-receipt '+(receipt.status==='rejected'||receipt.status==='partial'?'is-warning':'')+'" role="status"><strong>'+esc(labels[receipt.status]||'结果未确认')+'</strong><span> · '+esc(receipt.action_id||'')+'</span><p>'+esc(receipt.message||receipt.reason_code||'')+'</p>'+(receipt.applied_steps?.length?'<p>已生效步骤：'+esc(receipt.applied_steps.join('、'))+'</p>':'')+(receipt.failed_step||receipt.failed_steps?.length?'<p>失败步骤：'+esc(receipt.failed_step||(receipt.failed_steps||[]).join('、'))+'</p>':'')+(receipt.status==='accepted_waiting'?'<p>已记录受理回执，实际结果尚未确认。刷新后继续核验。</p>':'')+(receipt.status==='rejected'?'<p>旧操作已过期，执行未获确认。</p>':'')+(['rejected','accepted_waiting','partial'].includes(receipt.status)?'<button class="mini" onclick="loadControllerCockpit('+cockpitArg(receipt.workflow_id||cockpitWorkflowId())+')">刷新核验</button>':'')+'</article>';
}
function renderControllerTimeline(data){
  return '<section class="controller-cockpit-section"><h3>执行记录</h3>'+(data.receipts||[]).map(renderControllerReceipt).join('')+'<ol class="controller-cockpit-timeline">'+(data.timeline||[]).map(e=>'<li><time>'+esc(e.timestamp||'时间未确认')+'</time><span>'+esc(e.event_type||e.type||'事件')+'</span><span class="muted">'+esc(e.task_id||e.node_id||'')+'</span></li>').join('')+'</ol>'+(data.timeline_truncated?'<p class="muted">仅显示最近记录。</p>':'')+'</section>';
}
function paintControllerCockpit(){
  const data=state.controllerCockpit;
  const box=document.getElementById('controllerTabView');
  if(!box)return;
  if(!data||data.workflow?.workflow_id!==cockpitWorkflowId()){
    box.innerHTML='<div class="controller-cockpit-wrap" role="status">正在读取调度状态…</div>';return;
  }
  const receipt=state.controllerReceipt?.workflow_id===data.workflow.workflow_id?state.controllerReceipt:null;
  box.innerHTML='<div class="controller-cockpit-wrap">'+renderControllerHeader(data)+(data.read_state!=='confirmed'?'<div class="controller-cockpit-error" role="alert">读取未确认：'+esc(Object.values(data.errors||{}).join('；')||'部分权威来源不可用')+'。请刷新核验。</div>':'')+renderControllerJudgment(data)+renderControllerTarget(data,state.controllerTarget||{})+renderControllerReceipt(receipt)+'<div class="controller-cockpit-columns">'+renderControllerNodes(data,state.controllerTarget||{})+renderControllerTodos(data,state.controllerTarget||{})+'</div>'+renderControllerTimeline(data)+'</div>';
  box.hidden=false;
  if(state.controllerFocusPending){
    state.controllerFocusPending=false;
    requestAnimationFrame(()=>{
      if(state.workflowId!=='__ctl__')return;
      const located=box.querySelector?.('.controller-cockpit-action.is-target')||box.querySelector?.('.controller-cockpit-recovery.is-target')||box.querySelector?.('.controller-cockpit-task.is-target')||box.querySelector?.('.controller-cockpit-node.is-target')||box.querySelector?.('.controller-cockpit-target-context')||box;
      located.setAttribute('tabindex','-1');located.focus({preventScroll:true});located.scrollIntoView?.({block:'nearest'});
    });
  }
}
function openControllerCockpit(target={}){
  const wid=target.workflowId||(state.workflowId==='__ctl__'?state.controllerTarget?.workflowId:null)||state.workflow?.workflow?.workflow_id||(state.workflowId&&!state.workflowId.startsWith('__')?state.workflowId:'');
  if(!wid){toast('请先选择工作流',true);return;}
  const changed=state.controllerTarget?.workflowId!==wid;
  const navigating=arguments.length>0||state.workflowId!=='__ctl__';
  if(state.workflowId!=='__ctl__'){
    state.controllerReturnContext={workflowId:state.workflow?.workflow?.workflow_id||wid,nodeId:state.flowSelectedNodeId,taskId:state.selectedTaskId,taskDrawerTab:state.taskDrawerTab,taskDetail:state.selectedTaskDetail,workflowView:state.workflowView||'flow',drawerHidden:document.getElementById('taskDrawer')?.hidden!==false,focusElement:document.activeElement};
  }
  if(navigating){
    state.controllerTarget={...Object.fromEntries(['nodeId','taskId','runId','paneId','operationId'].filter(key=>target[key]!=null&&target[key]!=='').map(key=>[key,target[key]])),workflowId:wid};
    state.controllerFocusPending=true;
    closeModal();
    const drawer=document.getElementById('taskDrawer');if(drawer)drawer.hidden=true;
  }
  if(changed&&state.controllerReceipt?.workflow_id!==wid)state.controllerReceipt=null;
  setWorkspaceMode('ctl');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__ctl__'))state.openWorkflowTabIds.push('__ctl__');
  state.workflowId='__ctl__';renderWorkflowTabs();paintControllerCockpit();
  if(state.controllerCockpit?.workflow?.workflow_id!==wid)return loadControllerCockpit(wid);
}
function openControllerCockpitModal(){return openControllerCockpit();}
async function returnFromControllerCockpit(){
  const context=state.controllerReturnContext;
  if(!context)return;
  closeModal();
  state.controllerReturnContext=null;state.controllerFocusPending=false;
  state.workflowId=context.workflowId;state.shellView='workbench';state.workflowView=context.workflowView;
  if(state.workflow?.workflow?.workflow_id!==context.workflowId){
    try{await loadWorkflow(context.workflowId)}catch(e){toast('返回工作流失败：'+e.message,true);return;}
    if(state.workflowId!==context.workflowId||state.workflow?.workflow?.workflow_id!==context.workflowId)return;
  }
  state.flowSelectedNodeId=context.nodeId;state.selectedTaskId=context.taskId;state.selectedTaskDetail=context.taskDetail;state.taskDrawerTab=context.taskDrawerTab;
  setWorkspaceMode(context.workflowView);renderWorkflowTabs();
  if(context.workflowView==='list')renderTasks();else renderFlowWorkbench();
  const drawer=document.getElementById('taskDrawer');
  if(drawer){drawer.hidden=context.drawerHidden;if(!context.drawerHidden&&context.taskId&&context.taskDetail)switchTaskDrawerTab(context.taskDrawerTab||'overview');}
  requestAnimationFrame(()=>{
    if(state.workflowId!==context.workflowId)return;
    const focus=context.focusElement?.isConnected?context.focusElement:(!context.drawerHidden?drawer:document.getElementById('flowInspectorBody'));
    if(focus){focus.setAttribute?.('tabindex','-1');focus.focus();}
  });
}
async function executeCockpitAction(actionId){
  const data=state.controllerCockpit;
  const action=(data?.actions||[]).find(a=>a.action_id===actionId);
  const wid=data?.workflow?.workflow_id;
  if(!action||!controllerActionMatches(action,state.controllerTarget||{})||data.read_state!=='confirmed'||!['manual','confirmation_required'].includes(cockpitExecutionMode(action))||!action.expected_identity||isRejectedCockpitAction(action)){toast('操作条件未确认，请刷新',true);return;}
  const snapshot=JSON.parse(JSON.stringify(action));
  const actionKey=cockpitActionKey(snapshot);
  const highRisk=action.is_destructive||action.category==='bypass'||['destructive','evidence_bypass'].includes(action.risk);
  const isSteer=action.api_payload?.type==='steer';
  openModal(action.title||'确认操作','<div class="form"><p>'+esc(action.effect||action.description||'')+'</p>'+(highRisk?'<p role="alert">高风险操作：请先人工核验产物和目标身份。</p>':'')+(isSteer?'<label for="cockpitInstruction">指导内容</label><textarea id="cockpitInstruction" rows="3"></textarea>':'')+'<label for="cockpitReason">处理依据</label><textarea id="cockpitReason" rows="3"></textarea><p id="cockpitActionError" role="alert"></p><button class="btn primary" id="cockpitActionConfirm">确认执行</button><button class="btn" onclick="closeModal()">取消</button></div>');
  const button=document.getElementById('cockpitActionConfirm');
  button.onclick=async()=>{
    if(!controllerActionMatches(action,state.controllerTarget||{})||state.workflowId!=='__ctl__'||state.controllerCockpit?.read_state!=='confirmed'||!['manual','confirmation_required'].includes(cockpitExecutionMode((state.controllerCockpit?.actions||[]).find(a=>a.action_id===actionId)))||cockpitWorkflowId()!==wid||cockpitActionKey((state.controllerCockpit?.actions||[]).find(a=>a.action_id===actionId))!==actionKey){document.getElementById('cockpitActionError').textContent='目标已变化，请刷新';return;}
    const reason=(document.getElementById('cockpitReason')?.value||'').trim();
    const instruction=isSteer?(document.getElementById('cockpitInstruction')?.value||'').trim():null;
    if(isSteer&&!instruction){document.getElementById('cockpitActionError').textContent='请输入指导内容';return;}
    if((highRisk)&&!reason){document.getElementById('cockpitActionError').textContent='必须填写人工核验依据';return;}
    button.disabled=true;
    try{
      const result=await api('/api/controller/execute-action',{method:'POST',body:JSON.stringify({workflow_id:wid,action_id:actionId,expected_identity:action.expected_identity,reason,...(isSteer?{instruction}:{}),confirmed:true,operator:'human_console'})});
      if(state.workflowId!=='__ctl__'||cockpitWorkflowId()!==wid)return;
      state.controllerReceipt=result.receipt?{...result.receipt,...(result.receipt.status==='rejected'?{stale_action:snapshot}:{})}:{workflow_id:wid,action_id:actionId,status:'accepted_waiting',message:'回执缺失，结果未确认'};
      closeModal();paintControllerCockpit();
      await loadControllerCockpit(wid);
    }catch(error){if(cockpitWorkflowId()===wid){document.getElementById('cockpitActionError').textContent=error.message;button.disabled=false;}}
  };
}
function currentRecovery(nodeId){
  const cockpit=state.workflowId==='__ctl__'&&state.controllerCockpit?.workflow?.workflow_id===cockpitWorkflowId()?state.controllerCockpit:null;
  const w=cockpit?.workflow||state.workflow?.workflow||state.workflow;
  if(w&&(w.status==='completed'||w.status==='failed'||w.status==='halted'||w.outcome==='delivered'||w.outcome==='abandoned'||w.completed_at)){
    return [];
  }
  return (cockpit?.recovery||state.controllerActionsData?.recovery||[]).filter(o=>!['resolved','superseded'].includes(o.status)&&(!nodeId||o.payload?.node_id===nodeId));
}
function dispatchConfirmations(nodeId){
  const w=state.workflow?.workflow||state.workflow;
  if(w&&(w.status==='completed'||w.status==='failed'||w.status==='halted'||w.outcome==='delivered'||w.outcome==='abandoned'||w.completed_at)){
    return [];
  }
  const nid=(typeof nodeId==='object'&&nodeId!==null)?nodeId.id:nodeId;
  const gNodes=(state.workflow?.graph?.nodes)||[];
  return (state.controllerActionsData?.recovery||[]).filter(o=>{
    if(!o.confirmation)return false;
    const targetNodeId=o.payload?.node_id;
    if(nid&&targetNodeId!==nid)return false;
    if(targetNodeId){
      const n=gNodes.find(item=>String(item.id)===String(targetNodeId));
      if(n&&(n.status==='completed'||n.status==='cleaned'||n.status==='superseded'))return false;
    }
    return true;
  });
}
function dispatchConfirmationHtml(nodeId){return dispatchConfirmations(nodeId).map(o=>'<div class="flow-attn" role="status"><span class="flow-attn-tag">派发已确认</span><span class="flow-attn-text">'+esc(o.confirmation.task_ids.join('、'))+' · 查看任务列表可跟踪实际执行</span></div>').join('');}
function recoveryUnavailable(){return Boolean(state.workflow&&!state.controllerActionsData);}
function recoveryCard(o,wid){
  const r=o.recovery;
  const labels={pending:'等待派发',running:'正在处理',waiting:'已暂缓',waiting_human:'等待人工确认',awaiting_result:'等待总指挥建立任务'};
  const reasons={dispatch_task_missing:'未确认目标任务登记，请核对已有派发并补足需求',dispatch_delivery_unknown:'派发结果未知，先核验已有任务',dispatch_awaiting_task:'通知已返回，正在核验实际任务登记',legacy_dispatch_unknown:'历史通知无登记证据，正在核验',coordinator_missing:'总指挥工位不可用，等待前置问题处理',dispatch_generation_changed:'工作流代次或配置已变化，旧派发需核对',coordinator_busy:'总指挥忙碌，等待下一次检查',source_wip_requires_decision:'源仓库存在未处理改动，请先确认归属和处理方式',delivery_unknown:'交付结果未知，请核对现有后继',successor_delivery_unconfirmed:'后继交付尚未确认',candidate_unknown:'候选身份尚未确认',candidate_mismatch:'验收候选与当前候选不一致',recovery_successor_failed:'修复任务仍未通过，需要人工处理',finalize_escalated:'交付需要人工排除前置障碍',workflow_execution_unknown:'工作流执行身份尚未确认'};
  const code=o.detail?.reason||o.payload?.reason;
  const reason=r?.summary||o.detail?.decision_needed||reasons[code]||humanReason(code)||'等待恢复处理';
  const controls=r?(r.actions||[]).map(a=>`<button class="mini" data-recovery-action="${esc(a.action)}" onclick="openRecoveryDecision(${o.id},'${esc(a.action)}')">${esc(a.label)}</button>`).join(''):(o.status==='waiting_human'?`<button class="mini" onclick="decideRecovery(${o.id},${o.version},'${o.started?'verify':'retry'}','${esc(wid)}')">${o.started?'核对现有任务':'前置问题已处理，重试'}</button><button class="mini" onclick="decideRecovery(${o.id},${o.version},'hold','${esc(wid)}')">暂缓一小时</button>`:'');
  return `<div class="ctl-blocker"><strong>${esc(cleanStageLabel(r?.node_label||o.payload?.node_id||'恢复待办'))} · ${esc(labels[o.status]||o.status)}</strong><div>${esc(reason)}</div>${r?`<p class="muted">${esc(r.explanation)}</p><p>${esc(r.next_step)}</p><div>${(r.resource_checks||[]).map(c=>`<p>${esc(c.task_id||'旧启动')}：${esc(c.message)}</p>`).join('')}</div><div class="muted">验收版本：${esc((r.candidate_sha||'未确认').slice(0,12))}</div>`:''}<div class="muted">负责人：总指挥 · 恢复：Controller · ${o.status!=='waiting_human'&&o.next_due_at?'下次检查：'+new Date(o.next_due_at*1000).toLocaleString():'等待人工决定'}${o.detail?.deadline_at?' · 截止：'+new Date(o.detail.deadline_at*1000).toLocaleString():''}</div><div>${controls}</div></div>`;
}
function renderRecoveryPanel(wid){
  if(state.controllerActionsData?.pending)return '<section role="status">正在读取恢复状态…</section>';
  if(recoveryUnavailable())return '<section role="alert">无法读取恢复状态，尚不能确认是否有卡点。<button class="mini" onclick="loadWorkflow(state.workflow.workflow.workflow_id)">刷新恢复状态</button></section>';
  const operations=currentRecovery();
  return (operations.length?'<section><div class="ctl-section-title">恢复待办</div>'+operations.map(o=>recoveryCard(o,wid)).join('')+'</section>':'')+dispatchConfirmationHtml();
}
function openRecoveryDecision(id,action){
  const o=currentRecovery().find(o=>o.id===id);
  const choice=o?.recovery?.actions?.find(a=>a.action===action);
  if(!choice){toast('恢复条件已变化，请刷新页面',true);return;}
  const wid=o.workflow_id;
  const resend=['restore_scope','confirm_absent'].includes(action);
  const bindLineage=action==='restore_scope'&&o.recovery.lineage?.some(t=>t.requires_binding);
  const partial=action==='abandon_partial_launch';
  openModal(choice.label,`<form id="recoveryForm" class="form"><p>${esc(o.recovery.summary)}</p><p>${esc(o.recovery.next_step)}</p><p class="muted">节点：${esc(cleanStageLabel(o.recovery.node_label||o.payload.node_id))} · 验收版本：${esc(o.recovery.candidate_sha||'未确认')}</p><label for="recoveryOperator">处理人</label><input id="recoveryOperator" required maxlength="128"><label for="recoveryReason">核查结果与处理依据</label><textarea id="recoveryReason" required maxlength="1000" rows="3"></textarea>${resend?'<label><input id="recoveryConfirmed" type="checkbox" required style="width:auto">我已查看任务列表及旧工位，确认没有旧任务执行，授权对当前版本重新派发。</label><p class="muted">这是人工核查回执，不代表业务验收通过。系统仍会复查启动记录、候选和依赖。</p>':''}${partial?`<p>旧工位：${esc(choice.pane_id)} · Terminal：${esc(choice.terminal_id)}</p><p>先在终端检查 <code>herdr pane read ${esc(choice.pane_id)}</code>，只允许归档明确停留在启动提示的现场；确认 transcript 未包含任务指令或工作产出后，将其 SHA-256 填入。</p><label for="recoveryTranscriptSha256">已复核 transcript SHA-256</label><input id="recoveryTranscriptSha256" required maxlength=64 pattern="[a-fA-F0-9]{64}"><label><input id="recoveryPartialConfirmed" type="checkbox" required style="width:auto">我已检查上方工位 transcript，确认仅有启动提示且没有开始执行任务。</label>`:''}${bindLineage?`<p>旧任务：${esc(o.recovery.lineage.map(t=>t.task_id+' / '+(t.run_id||'身份未确认')).join('；'))}</p><label><input id="recoveryLineage" type="checkbox" required style="width:auto">确认以上任务 / Run属于本次工作流执行，授权补齐其执行归属。</label>`:''}<p id="recoveryError" role="alert"></p><button id="recoverySubmit" type="submit" class="btn primary">${esc(choice.label)}</button><button type="button" class="btn" onclick="closeModal()">取消</button></form>`);
  const form=document.getElementById('recoveryForm');
  const ownsForm=()=>form.isConnected&&document.getElementById('recoveryForm')===form&&document.getElementById('modal')?.classList.contains('open')&&(state.workflowId===wid||(state.workflowId==='__ctl__'&&cockpitWorkflowId()===wid));
  form.onsubmit=async(event)=>{
    event.preventDefault();const button=document.getElementById('recoverySubmit');button.disabled=true;
    const body={workflow_id:wid,operation_id:o.id,expected_version:o.version,operator:document.getElementById('recoveryOperator').value.trim(),reason:document.getElementById('recoveryReason').value.trim(),action,candidate_sha:o.recovery.candidate_sha,confirmed_absent:document.getElementById('recoveryConfirmed')?.checked===true,confirmed_lineage:document.getElementById('recoveryLineage')?.checked===true,lineage_snapshot:(o.recovery.lineage||[]).map(t=>({task_id:t.task_id,run_id:t.run_id,version:t.version}))};
    if(action==='abandon_partial_launch')Object.assign(body,{prior_operation_id:choice.prior_operation_id,expected_prior_version:choice.prior_version,intent_id:choice.intent_id,task_id:choice.task_id,pane_id:choice.pane_id,terminal_id:choice.terminal_id,transcript_sha256:document.getElementById('recoveryTranscriptSha256').value.trim(),confirmed_startup_only:document.getElementById('recoveryPartialConfirmed')?.checked===true});
    if(action==='hold')body.until=Date.now()/1000+3600;
    try{
      const result=await api('/api/workflow/recovery/decision',{method:'POST',body:JSON.stringify(body)});
      if(!ownsForm())return;
      closeModal();await loadWorkflow(body.workflow_id);
      if(state.workflowId!==wid&&!(state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===wid))return;
      openControllerCockpitModal();
      if(action==='check_resources')toast(result.checks.every(c=>c.status==='resources_absent')?'已确认启动资源不存在，可继续恢复':'现场尚未确认资源不存在，仍禁止重发，请查看恢复待办');
      else toast(resend?'已建立恢复待办，Controller将派发；尚未确认任务登记':'已记录，等待核对和执行');
    }catch(error){if(ownsForm()){document.getElementById('recoveryError').textContent=error.message;button.disabled=false;}}
  };
}
function decideRecovery(id,version,action,wid){
  showPromptModal({title:'记录恢复决策',label:'本次处理人的名称',onConfirm:(operator)=>{
    if(!operator?.trim())return;
    showPromptModal({title:'记录处理依据',label:'重试前请先处理提示的前置问题',onConfirm:async(reason)=>{
      if(!reason?.trim())return;
      const body={workflow_id:wid,operation_id:id,expected_version:version,operator,reason,action};
      if(action==='hold')body.until=Date.now()/1000+3600;
      try{await api('/api/workflow/recovery/decision',{method:'POST',body:JSON.stringify(body)});toast('已记录，等待核对和执行');await loadWorkflow(wid)}catch(e){toast('处理失败: '+e.message,true)}
    }});
  }});
}
async function advanceStage(){return openControllerCockpit({workflowId:cockpitWorkflowId()});}
return {
  showAgentOverride, saveAgentOverride, openSignoffChamber, submitSignoffDecision,
  forceReviewTask, retryStageAdvance, toggleDeepDrawer, switchDrawerTab,
  refreshDeepDrawer, showSteerModal, submitSteer, haltTaskPrompt,
  toggleWorkflowPause, stepWorkflow, showRollbackModal, executeRollback,
  forcePassTask, showCheckpointsModal, createWorkflowCheckpoint, restoreWorkflowCheckpoint,
  createCandidate, openDecisionPanel,
  submitDecision, openControllerCockpitModal, currentRecovery, dispatchConfirmationHtml,
  recoveryUnavailable, recoveryCard, openRecoveryDecision, decideRecovery,
  advanceStage, openControllerCockpit, returnFromControllerCockpit, renderControllerReceipt, executeCockpitAction
};
})({
  state, showConfirmModal, showPromptModal,
  api, toast, esc,
  humanStatus, humanReason, badge,
  cleanStageLabel, deliveryOf, deliveryReason,
  deliveryOverridesStatus, taskDisplayName, refreshAll,
  loadWorkflow, renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args), flowCardFoot,
  setWorkspaceMode, renderFlowWorkbench:(...args)=>renderFlowWorkbench(...args), renderTasks:(...args)=>renderTasks(...args), switchTaskDrawerTab:(...args)=>switchTaskDrawerTab(...args), openModal, closeModal
});
