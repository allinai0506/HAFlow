/* Workflow controls, human decisions, recovery and controller action presentation. */
var {
  showAgentOverride, saveAgentOverride, openSignoffChamber, submitSignoffDecision,
  forceReviewTask, retryStageAdvance, toggleDeepDrawer, switchDrawerTab,
  refreshDeepDrawer, showSteerModal, submitSteer, haltTaskPrompt,
  toggleWorkflowPause, stepWorkflow, showRollbackModal, executeRollback,
  forcePassTask, showCheckpointsModal, createWorkflowCheckpoint, restoreWorkflowCheckpoint,
  createCandidate, controllerActionCard, catMeta, openDecisionPanel,
  submitDecision, openControllerCockpitModal, currentRecovery, dispatchConfirmationHtml,
  recoveryUnavailable, recoveryCard, openRecoveryDecision, decideRecovery,
  advanceStage
} = (function({
  state, showConfirmModal, showPromptModal,
  api, toast, esc,
  humanStatus, humanReason, badge,
  cleanStageLabel, deliveryOf, deliveryReason,
  deliveryOverridesStatus, taskDisplayName, refreshAll,
  loadWorkflow, renderWorkflowTabs, flowCardFoot,
  setWorkspaceMode, openModal, closeModal
}){

function showAgentOverride(){if(!state.workflowId)return toast('当前没有工作流',true);const cur=state.workflow.agent_override||'auto';openModal('指定后续任务执行者',`<div class="form"><label for="overrideAgent">执行者策略</label><select id="overrideAgent">${['auto','opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>`<option ${a===cur?'selected':''}>${a}</option>`).join('')}</select><button class="btn primary" onclick="saveAgentOverride()">保存</button><div class="muted">只影响后续新建任务。</div></div>`)}async function saveAgentOverride(){try{await api('/api/workflow/agent',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,agent:document.getElementById('overrideAgent').value})});closeModal();await loadWorkflow(state.workflowId);toast('执行者策略已更新')}catch(e){toast(e.message,true)}}
async function openSignoffChamber(taskId){try{const [td,proj]=await Promise.all([api('/api/task?id='+encodeURIComponent(taskId)).catch(()=>null),api('/api/task/projection?id='+encodeURIComponent(taskId)).catch(()=>null)]);const d=proj||(td&&td.task)||{};const t=(td&&td.task)||{};window._signoffTask=t;const wid=t.workflow_id||state.workflowId;const node=t.node||t.stage||'';const arts=Array.isArray(d.artifacts)?d.artifacts:[];const isBlocked=t.stage_verdict==='blocked'||t.status==='blocked';let artCards='<div class="empty">暂无生成产物</div>';if(arts.length){artCards=arts.map(a=>`<div class="proj-sec" style="margin-bottom:8px"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px"><strong>${esc(a.name||a.kind)}</strong><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind)}</span></div><div class="task-meta" style="margin-bottom:8px">${esc(a.path||'')}</div><div class="proj-txt ctl-pre-light">${esc(a.content||a.summary||'（文件产物记录正常）')}</div></div>`).join('')}const html=`<div class="signoff-box"><div class="signoff-head"><div><div style="font-size:16px;font-weight:700">${esc(taskDisplayName(t))}</div><div class="task-meta">任务 ID: ${esc(taskId)} · 执行者: <b>${esc(t.agent||'-')}</b> · 节点: <b>${esc(node)}</b> · 版本: <b>v${esc(t.version!=null?t.version:'-')}</b></div></div><div>${badge(t.status)}</div></div>${isBlocked?'<div class="proj-blk"><strong>⚠️ 门禁会签等待:</strong> 当前节点触发门禁阻断，需要人类总指挥核查产物并决策放行或打回。</div>':''}<div class="proj-sec"><div class="proj-lbl">核心交付物与成果列表</div>${artCards}</div><div class="form"><label for="signoffFeedback">审批意见 / 批注说明（通过必填原因，打回可选）</label><input id="signoffFeedback" placeholder="例如：经核验核心成果符合要求，特批放行"></div><div class="signoff-actions"><button class="btn" onclick="closeModal()">暂不处理</button><button class="btn danger-btn" onclick="submitSignoffDecision(\'${esc(taskId)}\',\'${esc(wid)}\',\'${esc(node)}\',\'reject\')">批注打回</button><button class="btn primary" onclick="submitSignoffDecision(\'${esc(taskId)}\',\'${esc(wid)}\',\'${esc(node)}\',\'approve\')">通过并放行</button></div></div>`;openModal('成果交付会签室 (Artifact Signoff Chamber)',html)}catch(e){toast(e.message,true)}}
async function submitSignoffDecision(taskId,wid,node,act){const feedback=(document.getElementById('signoffFeedback')?.value||'').trim();if(act==='approve'&&!feedback){return toast('通过并放行必须填写审批说明原因',true)}closeModal();try{toast(act==='approve'?'正在通过并放行…':'正在批注打回…');const body={task_id:taskId,workflow_id:wid,node:node,action:act,feedback:feedback,reason:feedback,operator:'总指挥'};if(act==='approve'){body.confirmed=true;const curT=window._signoffTask||{};if(curT.task_id===taskId){if(curT.version!==undefined&&curT.version!==null)body.expected_version=curT.version;if(curT.pane_id)body.expected_pane_id=curT.pane_id}}const res=await api('/api/task/signoff',{method:'POST',body:JSON.stringify(body)});if(res.ok){await loadWorkflow(wid);toast(act==='approve'?'已通过并放行门禁！':'已完成批注打回，已回退至上游重新推进')}else{toast('操作失败: '+(res.error||'未知错误'),true)}}catch(e){toast(e.message,true)}}
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
async function toggleWorkflowPause(){if(!state.workflowId)return toast('当前没有工作流',true);const curSt=state.workflow&&state.workflow.workflow&&state.workflow.workflow.status;const isPaused=curSt==='paused';const act=isPaused?'resume':'pause';const label=isPaused?'恢复自动调度':'暂停自动调度';showConfirmModal({title:label,message:isPaused?'确认恢复该工作流的自动调度推进？':'确认暂停该工作流的自动推进？当前运行中的工位不会被强制终止。',confirmText:label,onConfirm:async()=>{try{await api('/api/kernel/'+act,{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});await loadWorkflow(state.workflowId);toast('工作流已'+(isPaused?'恢复':'暂停'))}catch(e){toast(e.message,true)}}})}
async function stepWorkflow(){if(!state.workflowId)return toast('当前没有工作流',true);try{toast('正在单步推进…');const res=await api('/api/kernel/step',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});if(res.ok){await loadWorkflow(state.workflowId);toast('单步已推进: '+res.stepped_label+' ('+res.stepped_node+')')}else{toast('无法单步推进: '+(res.reason==='no_ready_nodes'?'当前无就绪节点':res.reason),true)}}catch(e){toast(e.message,true)}}
function showRollbackModal(){if(!state.workflowId)return toast('当前没有工作流',true);const stages=(state.workflow&&state.workflow.stages)||[];if(!stages.length)return toast('工作流暂无节点',true);const options=stages.map(s=>`<option value="${esc(s.key)}">${esc(cleanStageLabel(s.label))} (${esc(s.key)})</option>`).join('');openModal('节点回溯 (Rollback)',`<div class="form"><label for="rbTarget">回溯目标节点（该节点及所有下游任务将被重置作废）</label><select id="rbTarget">${options}</select><label for="rbReason">回溯原因</label><input id="rbReason" type="text" value="人工核验需求变更或发现重大缺陷"><button class="btn primary" style="background:#dc2626;border-color:#ef4444" onclick="executeRollback()">确认回溯</button><div class="muted">警告：此操作不可撤销，下游所有产物与任务将被标记为作废。</div></div>`)}
async function executeRollback(){const target=document.getElementById('rbTarget').value;const reason=document.getElementById('rbReason').value;closeModal();try{toast('正在执行回溯…');const res=await api('/api/kernel/rollback',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,target_node_id:target,reason:reason})});await loadWorkflow(state.workflowId);toast('已成功回溯至 '+target+'，作废 '+res.invalidated_tasks.length+' 个任务')}catch(e){toast(e.message,true)}}
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
async function showCheckpointsModal(){if(!state.workflowId)return toast('当前没有工作流',true);try{const cps=await api('/api/kernel/checkpoints?workflow_id='+encodeURIComponent(state.workflowId));let listHtml='<div class="muted" style="margin-bottom:12px">暂无历史快照</div>';if(cps&&cps.length){listHtml=cps.map(c=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)"><div><strong>${esc(c.tag||'无标签')}</strong><div class="task-meta">${esc(c.checkpoint_id)} · ${c.task_count} 任务 · ${new Date(c.created_at*1000).toLocaleString()}</div></div><button class="btn" style="padding:3px 8px;font-size:12px;color:var(--danger)" onclick="restoreWorkflowCheckpoint('${esc(c.checkpoint_id)}')">恢复此快照</button></div>`).join('')}openModal('工作流快照中心',`<div class="form"><div style="display:flex;gap:8px;margin-bottom:16px"><input id="cpTagInput" type="text" placeholder="快照标签（如：修改前基准）" style="flex:1"><button class="btn primary" onclick="createWorkflowCheckpoint()">创建快照</button></div><h4>历史快照</h4><div style="max-height:240px;overflow-y:auto">${listHtml}</div></div>`)}catch(e){toast(e.message,true)}}
async function createWorkflowCheckpoint(){const tag=document.getElementById('cpTagInput')?.value||'';try{toast('正在保存快照…');await api('/api/kernel/checkpoint',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,tag:tag})});toast('快照已保存');showCheckpointsModal()}catch(e){toast(e.message,true)}}
async function restoreWorkflowCheckpoint(cpid){showConfirmModal({title:'恢复工作流快照',message:'确定要将工作流恢复到快照 '+cpid+' 吗？当前未保存的节点状态将被覆盖。',confirmText:'确认恢复',danger:true,onConfirm:async()=>{try{toast('正在恢复快照…');await api('/api/kernel/checkpoint/restore',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,checkpoint_id:cpid})});closeModal();await loadWorkflow(state.workflowId);toast('已恢复至快照 '+cpid)}catch(e){toast(e.message,true)}}})}
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
function controllerActionCard(act,tid,index){
  const m=catMeta(act.category);
  const fx=act.effect||act.description||'';
  const target=tid||act.blocker_task_id||act.old_task_id||'';
  return `<article class="ctl-card${act.recommended?' is-recommended':''}${act.is_destructive?' is-destructive':''}"><div class="ctl-card-head"><span class="ctl-index">操作 ${index}</span><span class="ctl-card-title">${act.recommended?'⭐ ':''}${esc(act.title)}</span><span class="ctl-badges">${act.recommended?'<span class="ctl-rec">推荐</span>':''}<span class="badge ${m[1]}">${esc(m[0])}</span></span></div><p class="ctl-card-desc">${esc(act.description)}</p>${fx?`<div class="ctl-effect">点按钮后：${esc(fx)}</div>`:''}${act.new_task_id||act.new_agent?`<div class="ctl-meta-chips">${act.old_task_id?`<span class="ctl-chip">旧任务 <strong>${esc(act.old_task_id)}</strong></span>`:''}${act.new_task_id?`<span class="ctl-chip">新任务 <strong>${esc(act.new_task_id)}</strong></span>`:''}${act.new_agent?`<span class="ctl-chip">执行者 <strong>${esc(act.new_agent)}</strong></span>`:''}</div>`:''}<div class="ctl-card-foot">${target?`<button class="btn" onclick="locateControllerTask('${esc(target)}')">查看任务</button>`:''}<button class="btn primary" onclick="executeControllerAction('${esc(act.action_id)}','${esc(state.workflowId||'')}')">一键执行</button></div>${act.command_line?`<details class="ctl-tech"><summary>工程师技术详情（可选）</summary><div class="ctl-tech-body"><div class="ctl-cheat-cmd"><code>${esc(act.command_line)}</code></div><div style="display:flex;justify-content:flex-end;margin-top:8px"><button class="mini" onclick="copyCliCommandByActionId('${esc(act.action_id)}')">复制命令</button></div></div></details>`:''}</article>`;
}
// Category → [label, badge class].  Module scope: the action-card renderer is
// shared by both the blocker and pipeline sections.
const CTL_CAT_META={fix:['修复','working'],rework:['换人重派','rework'],bypass:['豁免推进','cleaned'],recovery:['恢复','waiting'],pipeline:['推进交付','working']};
function catMeta(c){return CTL_CAT_META[c]||[c||'操作','waiting'];}
// Pipeline actions must render even when there are zero blockers: a healthy task
// that still owes `integrate` is exactly the case that used to render nothing.
// Tasks the scheduler already owns have no action at all — they are shown as an
// attribution instead, so nobody hunts for a button that would race the daemon (#253).
function renderSchedulerAttribution(){
  const ts=((state.workflow&&state.workflow.tasks)||[]).filter(deliveryOverridesStatus);
  if(!ts.length)return {count:0,html:''};
  const html=ts.map(t=>{
    const note=String((deliveryOf(t)||{}).owner_note||'');
    const reason=deliveryReason(t);
    const detail=reason&&reason!==note?` · ${esc(reason)}`:'';
    return `<div class="ctl-blocker" style="margin-bottom:8px"><div class="ctl-blocker-head"><div style="min-width:0"><div class="ctl-blocker-title">${esc(taskDisplayName(t)||t.task_id)}</div><div class="ctl-blocker-id">${esc(t.task_id||'')}</div></div><div style="display:flex;gap:8px;flex-shrink:0"><button class="mini" onclick="locateControllerTask('${esc(t.task_id||'')}')">查看任务详情</button></div></div><div class="ctl-effect">${esc(note)}${detail}</div></div>`;
  }).join('');
  return {count:ts.length,html};
}
function renderPipelineActions(acts){
  const pipelineActs=acts.filter(a=>a.group==='pipeline');
  const attributed=renderSchedulerAttribution();
  if(!pipelineActs.length&&!attributed.count)return '';
  const resumeAct=pipelineActs.find(a=>a.action_id.endsWith(':resume_workflow'));
  const taskActs=pipelineActs.filter(a=>a!==resumeAct);
  const byTask={};
  taskActs.forEach(a=>{const k=a.blocker_task_id||String(a.action_id).split(':')[0];(byTask[k]=byTask[k]||[]).push(a)});
  const groups=Object.keys(byTask).map(k=>{
    const items=byTask[k].slice().sort((a,b)=>(b.recommended?1:0)-(a.recommended?1:0));
    return `<div class="ctl-blocker" style="margin-bottom:8px"><div class="ctl-blocker-head"><div style="min-width:0"><div class="ctl-blocker-title">${esc(taskDisplayName({task_id:k})||k)}</div><div class="ctl-blocker-id">${esc(k)}</div></div><div style="display:flex;gap:8px;flex-shrink:0"><button class="mini" onclick="locateControllerTask('${esc(k)}')">查看任务详情</button></div></div></div><div class="ctl-actions">${items.map((a,i)=>controllerActionCard(a,k,i+1)).join('')}</div>`;
  }).join('');
  const resumeHtml=resumeAct?`<div class="ctl-actions" style="margin-bottom:12px">${controllerActionCard(resumeAct,'',1)}</div>`:'';
  const count=pipelineActs.length+attributed.count;
  return `<section><div class="ctl-section-title">继续推进 <span class="ctl-count">${count}</span></div><div class="ctl-sub">这些任务本身没有报错，只是还差一步就能收尾（保存版本 / 并入目标分支 / 归档），或者工位需要人推一把。点按钮即可。由调度器接管的任务不需要你操作，只列出当前等待原因。</div>${resumeHtml}${groups}${attributed.html}</section>`;
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
  const decision=(document.getElementById('decChosen')?.value||'').trim();
  const note=(document.getElementById('decNote')?.value||'').trim();
  if(!decision)return toast('请填写你的结论',true);
  closeModal();
  try{
    toast('正在记录裁决…');
    await api('/api/workflow/decision',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId,decision_id:decisionId,decision:decision+(note?('（'+note+'）'):''),operator:'human_console'})});
    toast('裁决已落库，总指挥下一轮即可读到');
    await loadWorkflow(state.workflowId);
  }catch(e){toast('记录失败: '+e.message,true)}
}
function openControllerCockpitModal(){
  const w=state.workflow&&state.workflow.workflow;
  const picked=(w&&w.workflow_id)||state.workflowId||'';
  const wid=picked&&!String(picked).startsWith('__')?picked:'未选择工作流';
  const stall=state.workflow&&state.workflow.stall;
  const allActs=(state.controllerActionsData&&state.controllerActionsData.actions)||[];
  const blockers=(state.controllerActionsData&&state.controllerActionsData.blockers)||[];
  const recoveryCount=currentRecovery().length;
  const acts=allActs.filter(a=>a.group!=='pipeline');
  if(!state.controllerActionsMap)state.controllerActionsMap={};
  allActs.forEach(a=>{state.controllerActionsMap[a.action_id]=a;});
  const isWfDone=['completed','cleaned','archived'].includes(w&&w.status);
  const curStage=isWfDone?null:(state.workflow&&state.workflow.stages?state.workflow.stages.find(s=>['working','failed','blocked'].includes(s.status)):null);
  const stageName=isWfDone?'已全部完成':(curStage?(curStage.label||curStage.key):'就绪/空闲');
  const isStalled=Boolean(stall&&stall.is_stalled);

  let statusCard=`<section class="ctl-status${isStalled?' is-stalled':''}"><div class="ctl-status-head"><span class="ctl-status-title"><span class="ctl-dot"></span>调度状态与等待条件</span><span class="badge ${isStalled?'failed':'cleaned'}">${isStalled?'推进停滞':'调度运转中'}</span></div><div class="ctl-status-grid"><div class="ctl-status-item"><span class="ctl-k">当前关注阶段</span><strong>${esc(stageName)}</strong></div><div class="ctl-status-item"><span class="ctl-k">活跃卡点</span><strong>${recoveryUnavailable()?'未确认':blockers.length+recoveryCount+' 项'}</strong></div><div class="ctl-status-item is-full"><span class="ctl-k">${isStalled?'停滞原因':'轮询状态'}</span><span class="ctl-note">${isStalled?`⚠️ ${esc(stall.message)}`:'✓ Controller 后台轮询正常，正在监控 DAG 拓扑门禁'}</span></div></div></section>`;

  let unblockSection='';
  if(blockers.length||acts.length){
    const actionKey=a=>a.blocker_task_id||a.old_task_id||String(a.action_id||'').split(':')[0]||'';
    const groups=blockers.length?blockers.map(b=>({blocker:b,items:acts.filter(a=>actionKey(a)===b.task_id)})):[{blocker:null,items:acts}];
    if(!blockers.length&&acts.length)groups[0].items=acts;
    const sections=groups.map((g,gi)=>{
      const b=g.blocker||{};
      const tid=b.task_id||(g.items[0]?actionKey(g.items[0]):'')||'';
      const reason=humanReason(b.stage_verdict_note||b.blocker||b.blocked_reason||b.sentinel_reason||'');
      const st=b.status||'';
      const ag=b.agent||'';
      const stageLabel=b.stage_label||b.stage||b.node||'';
      const head=tid?`<div class="ctl-blocker" style="margin-bottom:8px"><div class="ctl-blocker-head"><div style="min-width:0"><div class="ctl-blocker-title">卡点任务 ${gi+1} · ${esc(taskDisplayName(b)||stageLabel||'任务')}</div><div class="ctl-blocker-id">${esc(tid)}${ag?` · 执行者 ${esc(ag)}`:''}${st?` · ${esc(humanStatus(st))}`:''}${stageLabel?` · ${esc(stageLabel)}`:''}</div></div><div style="display:flex;gap:8px;flex-shrink:0"><button class="mini" onclick="locateControllerTask('${esc(tid)}')">查看任务详情</button></div></div>${reason?`<div class="ctl-blocker-reason">卡点原因：${esc(reason)}</div>`:''}</div>`:`<div class="ctl-sub">以下为当前可执行的一键解卡操作（已按推荐度排序）。</div>`;
      const cards=(g.items.length?g.items:[]).map((act,i)=>controllerActionCard(act,tid,i+1)).join('')||'<div class="muted" style="font-size:12px">该卡点暂无自动解卡按钮，请查看任务详情人工处理。</div>';
      return `<section style="margin-bottom:16px">${head}<div class="ctl-actions">${cards}</div></section>`;
    }).join('');
    unblockSection=`<section><div class="ctl-section-title">按卡点任务一键解卡 <span class="ctl-count">${blockers.length||acts.length}</span></div><div class="ctl-sub">每个卡点任务独立分组：先看“卡点原因”，再点对应按钮即可执行，无需手动敲命令。</div>${sections}</section>`;
  } else if(!recoveryCount&&!recoveryUnavailable()) {
    unblockSection=`<div class="ctl-empty">✓ 当前没有报错卡点。<div class="muted">若只是还没收尾（保存版本 / 并入目标分支 / 归档），见下方“继续推进”一节。</div></div>`;
  }
  const resources=(state.workflow?.graph?.nodes||[]).filter(n=>n.resource_usage);
  const resourceSection=resources.length?'<section><div class="ctl-section-title">节点任务与工位占用</div><div class="ctl-sub">累计包含已归档任务；工位计数为持久引用数，待回收引用单列。</div>'+resources.map(n=>{
    const u=n.resource_usage;
    return '<div class="ctl-status-item"'+(u.overflow?' style="color:var(--danger);font-weight:600"':'')+'><strong>'+esc(n.label||n.id)+'</strong><span>'+esc(flowCardFoot(n))+' · 待回收 '+u.orphan_pane_count+'</span></div>';
  }).join('')+'</section>':'';
  const pipelineSection=renderPipelineActions(allActs);
  const decisionSection=renderDecisionPanel();
  const recoverySection=renderRecoveryPanel(wid);

  const cheatRows=[
    {name:'原工位返工',cmd:`bin/herdr-task rework <task-id> --prompt '<修复指引>'`,desc:'保留 task_id 与工位，就地修复与自测'},
    {name:'换执行者重派（例外）',cmd:`bin/herdr-task launch --task-id <new-task-id> --workflow-id ${wid} --stage <stage> --source . --agent <agent> --goal <goal> --prompt <prompt> --supersedes <old-task-id> --supersede-reason '<替换理由>'`,desc:'作废指定卡点旧任务，换执行者重派新任务'},
    {name:'强制推进阶段',cmd:`bin/herdr-task advance ${wid}`,desc:'检查并强制推进工作流至下一阶段'},
    {name:'解除升级锁定',cmd:'bin/herdr-task clear-escalation <task-id>',desc:'撤销机器终化升级锁，解除阻断重新流转'},
    {name:'立刻推工位一把',cmd:`herdr agent prompt <pane-id> "提示内容" --wait --timeout 180000`,desc:'马上把指令送到工位并等回执（立即生效）'},
    {name:'留话指导',cmd:'bin/herdr-task steer <task-id> "提示内容"',desc:'把指导放进留言队列，下一轮才生效（不打断当前执行）'},
  ];
  let cheatSheet=`<details class="ctl-tech"><summary>工程师命令参考（可选展开，终端用户无需使用）</summary><div class="ctl-tech-body"><section class="ctl-cheatsheet" style="box-shadow:none"><div class="ctl-sub">以下命令仅供审计与排查；日常使用请点上面的“一键执行”按钮。</div><div class="ctl-cheat-list">${cheatRows.map((r,i)=>`<div class="ctl-cheat-row"><span class="ctl-cheat-num">${i+1}</span><div><div class="ctl-cheat-name"><span>${esc(r.name)}</span><button class="mini" onclick="copyCliCommand(document.getElementById('ctlCheat${i}').textContent)">复制</button></div><div class="ctl-cheat-cmd"><code id="ctlCheat${i}">${esc(r.cmd)}</code></div><div class="ctl-cheat-desc">${esc(r.desc)}</div></div></div>`).join('')}</div></section></div></details>`;

  const html=`<div class="ctl-wrap">${statusCard}${resourceSection}${recoverySection}${decisionSection}${unblockSection}${pipelineSection}${cheatSheet}</div>`;
  const _ctlEl=document.getElementById('controllerTabView');
  if(_ctlEl){_ctlEl.innerHTML=`<div class="ctl-panel-header"><strong>Controller 调度与解卡控制台</strong><span class="muted" style="margin-left:8px;font-weight:400">· ${esc(wid)}</span></div>`+html;_ctlEl.hidden=false;}
  setWorkspaceMode('ctl');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__ctl__'))state.openWorkflowTabIds.push('__ctl__');
  state.workflowId='__ctl__';
  renderWorkflowTabs();
}
function currentRecovery(nodeId){
  const w=state.workflow?.workflow||state.workflow;
  if(w&&(w.status==='completed'||w.status==='failed'||w.status==='halted'||w.outcome==='delivered'||w.outcome==='abandoned'||w.completed_at)){
    return [];
  }
  return (state.controllerActionsData?.recovery||[]).filter(o=>!['resolved','superseded'].includes(o.status)&&(!nodeId||o.payload?.node_id===nodeId));
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
  const ownsForm=()=>form.isConnected&&document.getElementById('recoveryForm')===form&&document.getElementById('modal')?.classList.contains('open')&&(state.workflowId===wid||(state.workflowId==='__ctl__'&&state.workflow?.workflow?.workflow_id===wid));
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
async function advanceStage(){
  showConfirmModal({
    title:'进入下一阶段',
    message:'让总指挥检查当前阶段门禁并尝试推进至下一阶段？',
    confirmText:'确认推进',
    onConfirm:async()=>{
      try{
        toast('正在请求阶段推进…');
        await api('/api/workflow/advance',{method:'POST',body:JSON.stringify({workflow_id:state.workflowId})});
        toast('阶段推进请求已完成');
      }catch(e){toast(e.message,true)}
    }
  });
}
return {
  showAgentOverride, saveAgentOverride, openSignoffChamber, submitSignoffDecision,
  forceReviewTask, retryStageAdvance, toggleDeepDrawer, switchDrawerTab,
  refreshDeepDrawer, showSteerModal, submitSteer, haltTaskPrompt,
  toggleWorkflowPause, stepWorkflow, showRollbackModal, executeRollback,
  forcePassTask, showCheckpointsModal, createWorkflowCheckpoint, restoreWorkflowCheckpoint,
  createCandidate, controllerActionCard, catMeta, openDecisionPanel,
  submitDecision, openControllerCockpitModal, currentRecovery, dispatchConfirmationHtml,
  recoveryUnavailable, recoveryCard, openRecoveryDecision, decideRecovery,
  advanceStage
};
})({
  state, showConfirmModal, showPromptModal,
  api, toast, esc,
  humanStatus, humanReason, badge,
  cleanStageLabel, deliveryOf, deliveryReason,
  deliveryOverridesStatus, taskDisplayName, refreshAll,
  loadWorkflow, renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args), flowCardFoot,
  setWorkspaceMode, openModal, closeModal
});
