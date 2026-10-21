/* 任务列表、关注操作与人工交互响应。 */
var {
  setTaskFilter, copyCliCommand, copyCliCommandByActionId, executeControllerAction,
  locateControllerTask, openTerminalForTask, submitInteractionResponse, renderStages,
  renderTasks, renderTaskRow
} = (function({
  state, api, toast,
  esc, humanReason, cleanStageLabel,
  taskBadge, deliveryWaitHtml, taskDisplayName,
  closeModal, openTaskDrawer, toggleDeepDrawer,
  switchDrawerTab, openControllerCockpit
}){

function setTaskFilter(f){state.taskFilter=f;['All','Decision','Attention','Active'].forEach(k=>{const el=document.getElementById('f'+k);if(el)el.classList.toggle('active',f.toLowerCase()===k.toLowerCase())});renderTasks()}
function isDecisionTask(t){
  if(t.status==='superseded'||t.status==='cleaned')return false;
  return t.stage_verdict==='blocked'||t.status==='blocked'||(t.node_type==='gate'&&['agent_done','completed'].includes(t.status)&&t.stage_verdict!=='pass');
}
function decisionSummary(t){
  const title=taskDisplayName(t);
  const blocker=Array.isArray(t.blocker)?t.blocker.join('; '):t.blocker;
  return {
    title,
    question:t.decision_question||`是否批准“${title}”继续推进？`,
    basis:humanReason(t.stage_verdict_note||blocker||t.blocked_reason||t.sentinel_reason||t.goal||'请核查成果后选择通过并放行或批注打回')
  };
}
function copyCliCommand(cmd){
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(()=>{toast('已复制 Controller 命令到剪贴板！')}).catch(()=>{toast('复制失败',true)});
  }else{
    const ta=document.createElement('textarea');
    ta.value=cmd;
    document.body.appendChild(ta);
    ta.select();
    try{document.execCommand('copy');toast('已复制 Controller 命令到剪贴板！')}catch(e){toast('命令: '+cmd)}
    document.body.removeChild(ta);
  }
}
function copyCliCommandByActionId(actId){
  const act=(state.controllerActionsMap&&state.controllerActionsMap[actId])||{};
  const cmd=act.command_line||actId;
  copyCliCommand(cmd);
}
async function executeControllerAction(actId,wid){
  const act=(state.controllerActionsMap&&state.controllerActionsMap[actId])||{};
  const target=act.target||{};
  return openControllerCockpit({workflowId:target.workflow_id||wid,nodeId:target.node_id||'',taskId:target.task_id||act.blocker_task_id||act.old_task_id||'',runId:target.run_id||'',paneId:target.pane_id||''});
}
function controllerBlockerTask(actionId){
  const act=(state.controllerActionsMap&&state.controllerActionsMap[actionId])||{};
  return act.blocker_task_id||act.old_task_id||'';
}
function locateControllerTask(taskId){
  if(!taskId)return;
  closeModal();
  try{if(state.taskFilter&&state.taskFilter!=='all'){setTaskFilter('all')}}catch(e){}
  setTimeout(()=>{try{openTaskDrawer(taskId)}catch(e){toast('任务详情打开失败: '+e.message,true)}},60);
}
function openTerminalForTask(paneId){
  const d=document.getElementById('deepDrawer');
  if(d&&d.classList.contains('collapsed')){
    toggleDeepDrawer();
  }
  switchDrawerTab('tty');
}

async function submitInteractionResponse(taskId, optionId, fingerprint, paneId, agent, terminalId){
  const actions=document.getElementById('iactions-'+taskId);
  const feedback=document.getElementById('ifeedback-'+taskId);
  if(actions){
    const btns=actions.querySelectorAll('button');
    btns.forEach(b=>{b.disabled=true;});
  }
  if(feedback){
    feedback.style.display='block';
    feedback.innerHTML='<span style="color:var(--primary)">⏳ 正在提交响应…</span>';
  }
  try{
    const res=await api('/api/task/interaction/respond',{
      method:'POST',
      body:JSON.stringify({
        task_id:taskId,
        option_id:optionId,
        expected_fingerprint:fingerprint,
        expected_pane_id:paneId,
        expected_agent:agent,
        expected_terminal_id:terminalId,
      })
    });
    if(res&&res.ok){
      if(res.status==='resolved'){
        if(feedback)feedback.innerHTML='<span style="color:var(--success)">✅ 已处理：选项已生效，执行者正在继续执行</span>';
        setTimeout(()=>{
          if(state.workflowId){
            refreshWorkflowInteractions(state.workflowId);
          }
        },1000);
      }else{
        if(feedback)feedback.innerHTML='<span style="color:var(--warning)">⏳ 已发送，等待确认执行者状态更新…</span>';
        setTimeout(()=>{
          if(state.workflowId){
            refreshWorkflowInteractions(state.workflowId);
          }
        },1500);
      }
    }else{
      const err=(res&&res.reason)||'处理失败或结果未知';
      if(feedback)feedback.innerHTML=`<span style="color:var(--danger)">⚠️ ${esc(err)}。可点击右侧“打开终端”入口排查</span>`;
      if(actions){
        const btns=actions.querySelectorAll('button');
        btns.forEach(b=>{b.disabled=false;});
      }
    }
  }catch(e){
    if(feedback)feedback.innerHTML=`<span style="color:var(--danger)">❌ 提交失败: ${esc(e.message)}。可点击右侧“打开终端”入口排查</span>`;
    if(actions){
      const btns=actions.querySelectorAll('button');
      btns.forEach(b=>{b.disabled=false;});
    }
  }
}

function refreshWorkflowInteractions(wid){
  if(!wid||state.opsMode)return;
  api('/api/task/interaction?workflow_id='+encodeURIComponent(wid)).then(d=>{
    state.activeInteractions=d.interactions||[];
    updateAttentionHub();
  }).catch(()=>{state.activeInteractions=[];});
}

function updateAttentionHub(){
  const ts=(state.workflow&&state.workflow.tasks)||[];
  const cntAll=ts.length;
  const decisionTasks=ts.filter(isDecisionTask);
  const attentionTasks=ts.filter(t=>['failed','interrupted','rework'].includes(t.status)||(t.blocker&&t.blocker.length));
  const activeTasks=ts.filter(t=>['dispatched','working','rework','paused'].includes(t.status));
  const elAll=document.getElementById('cntAll');if(elAll)elAll.textContent=cntAll;
  const elDec=document.getElementById('cntDecision');if(elDec)elDec.textContent=decisionTasks.length;
  const elAtt=document.getElementById('cntAttention');if(elAtt)elAtt.textContent=attentionTasks.length;
  const elAct=document.getElementById('cntActive');if(elAct)elAct.textContent=activeTasks.length;
  const ab=document.getElementById('attentionBanner');
  if(!ab)return;
  if(!state.workflowId||state.opsMode){ab.style.display='none';return}
  ab.style.display='flex';
  const readyCnt=Math.max(0,cntAll-decisionTasks.length-attentionTasks.length);
  const stall=state.workflow&&state.workflow.stall;
  const w=state.workflow&&state.workflow.workflow;
  if(stall&&stall.is_stalled){
    ab.style.background='var(--warning-bg)';
    ab.style.borderColor='rgba(217,119,6,0.25)';
    const actBtn=`<button class="btn primary" onclick="openControllerCockpit(${esc(JSON.stringify({workflowId:state.workflowId,taskId:stall.target_task_id||''}))})">在 Controller 中处理</button>`;
    ab.innerHTML=`<div style="display:flex;align-items:center;gap:8px"><span class="att-badge" style="background:var(--warning);color:#fff">推进停滞告警</span><span class="att-text" style="color:var(--text-primary)">⚠️ ${esc(stall.message)}</span></div><div>${actBtn}</div>`;
  }else if(w&&(w.status==='completed'||w.outcome==='delivered')){
    ab.style.background='var(--success-bg)';
    ab.style.borderColor='rgba(22,163,74,0.25)';
    ab.innerHTML=`<div style="display:flex;align-items:center;gap:8px"><span class="att-badge" style="background:var(--success);color:#fff">已交付</span><span class="att-text" style="color:var(--text-primary);font-weight:500">🎉 工作流已顺利完成全流程闭环并交付归档</span></div><div></div>`;
  }else{
    ab.style.background='var(--bg-surface)';
    ab.style.borderColor='var(--border-default)';
    ab.style.borderLeft='3px solid var(--primary)';
    const detailRows=decisionTasks.slice(0,3).map(t=>{
      const d=decisionSummary(t);
      const target={workflowId:t.workflow_id||state.workflowId,nodeId:t.node||t.stage||'',taskId:t.task_id,runId:t.run_id||'',paneId:t.pane_id||''};
      const actionsHtml=`<button class="mini" onclick="openControllerCockpit(${esc(JSON.stringify(target))})">在 Controller 中处理</button>`;
      return `<div class="decision-item"><strong>${esc(d.title)}</strong><span>${esc(d.question)}</span><small>依据：${esc(d.basis)}</small>${actionsHtml}</div>`;
    }).join('');
    const more=decisionTasks.length>3?`<div class="decision-more">还有 ${decisionTasks.length-3} 项，请查看全部决策项。</div>`:'';
    const details=decisionTasks.length?`<div class="decision-list"><div class="decision-list-label">待决策事项</div>${detailRows}${more}</div>`:'';

    const interactions=state.activeInteractions||[];
    let interactionHtml='';
    if(interactions.length>0){
      const cardRows=interactions.map(it=>{
        const hasOptions=it.options&&it.options.length>0;
        const btns=hasOptions?it.options.map(opt=>`
          <button class="btn primary" id="btn-opt-${esc(it.task_id)}-${esc(opt.option_id)}"
            style="padding:4px 12px;font-size:12px;font-weight:600"
            onclick="submitInteractionResponse('${esc(it.task_id)}','${esc(opt.option_id)}','${esc(it.prompt_fingerprint)}','${esc(it.pane_id)}','${esc(it.agent)}','${esc(it.terminal_id)}')">
            ${esc(opt.label)}
          </button>
        `).join(''):'<span class="muted" style="font-size:11px">未能自动识别结构化选项，请使用终端处理</span>';

        return `
          <div class="interaction-card" id="icard-${esc(it.task_id)}" style="margin-top:8px;padding:8px 12px;background:rgba(217,119,6,0.06);border:1px solid rgba(217,119,6,0.3);border-left:4px solid var(--warning);border-radius:4px">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px">
              <div style="display:flex;align-items:center;gap:8px">
                <span class="badge warning" style="font-size:11px;font-weight:600">⚡ ${esc(it.agent)} 正在等待你的决定</span>
                <strong style="font-size:12px;color:var(--text-primary)">${esc(it.task_title || it.task_id)}</strong>
              </div>
              <span class="muted" style="font-size:11px">工位: <code>${esc(it.pane_id)}</code></span>
            </div>
            <div style="font-size:12px;line-height:1.5;margin-bottom:8px;padding:8px 12px;background:rgba(0,0,0,0.03);border-radius:4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;color:var(--text-primary)">${esc(it.raw_prompt || '终端等待确认')}</div>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
              <div id="iactions-${esc(it.task_id)}" style="display:flex;gap:8px;align-items:center">
                ${btns}
              </div>
              <button class="mini" style="padding:4px 8px;font-size:11px" onclick="openTerminalForTask('${esc(it.pane_id)}')">🖥️ 打开终端现场</button>
            </div>
            <div id="ifeedback-${esc(it.task_id)}" style="margin-top:8px;font-size:11px;color:var(--text-secondary);display:none"></div>
          </div>
        `;
      }).join('');
      interactionHtml=`<div class="interaction-list" style="margin-top:8px">${cardRows}</div>`;
      ab.style.borderLeft='3px solid var(--warning)';
    }

    ab.innerHTML=`<div style="min-width:0;flex:1"><div style="display:flex;align-items:center;gap:8px"><span class="att-badge">人机协同态势</span><span class="att-text">${activeTasks.length} 个执行者正在协同 · ${readyCnt} 个正常推进 · ${attentionTasks.length} 个需关注 · <strong style="color:${decisionTasks.length?'var(--accent)':'var(--text)'}">${decisionTasks.length} 个待你拍板</strong>${interactions.length?` · <strong style="color:var(--warning)">⚡ ${interactions.length} 个终端等待交互</strong>`:''}</span></div>${interactionHtml}${details}</div><div>${decisionTasks.length?`<button class="btn primary" style="padding:4px 12px;font-size:12px" onclick="setTaskFilter('decision')">查看决策项</button>`:''}</div>`;
  }
}
function renderStages(){
  if(state.shellView&&state.shellView!=='workbench')return;
  if(state.opsMode||state.dashMode||state.workflowId==='__templates__')return;
  const ss=(state.workflow&&state.workflow.stages)||[];
  const e=document.getElementById('stages');
  if(!e)return;
  if(!ss.length){e.style.display='none';e.innerHTML='';return}
  e.style.display='flex';
  const actIdx=ss.findIndex(s=>['working','finalizing','failed','blocked'].includes(s.status));
  let nextIdx=-1;
  if(actIdx===-1){
    nextIdx=ss.findIndex(s=>!['cleaned','completed','committed','integrated'].includes(s.status));
  }
  e.innerHTML=ss.map((s,i)=>{
    const done=['cleaned','completed','committed','integrated'].includes(s.status);
    const isAct=(i===actIdx);
    const isNext=(i===nextIdx);
    const stIcon=done?'✓':(isAct||isNext)?'●':'○';
    const stCls=done?'stage-done':isAct?'stage-running':isNext?'stage-next':'stage-pending';
    let badgeHtml='';
    if(isAct){
      if(s.status==='blocked')badgeHtml='<span class="stage-badge blocked">阻塞</span>';
      else if(s.status==='failed')badgeHtml='<span class="stage-badge blocked">失败</span>';
      else badgeHtml='<span class="stage-badge running">进行中</span>';
    }else if(isNext){
      badgeHtml='<span class="stage-badge next">下一阶段</span>';
    }
    return `<div class="stage-step ${stCls}">`+
      `<span class="stage-indicator">${stIcon}</span>`+
      `<span class="stage-name">${esc(cleanStageLabel(s.label))}</span>`+
      badgeHtml+
      `<span class="stage-meta">${s.count} 任务</span>`+
    `</div>`+(i<ss.length-1?'<div class="stage-connector"></div>':'');
  }).join('')
}
function renderTasks(){
  if(state.shellView&&state.shellView!=='workbench')return;
  if(state.opsMode||state.dashMode||state.workflowId==='__templates__')return;
  const e=document.getElementById('tasks');
  const allTs=(state.workflow&&state.workflow.tasks)||[];
  updateAttentionHub();
  if(state.workflowId&&allTs.some(t=>t.status==='blocked'||(t.blocker&&t.blocker.length))){
    refreshWorkflowInteractions(state.workflowId);
  }
  let ts=allTs;
  const f=state.taskFilter||'all';
  if(f==='decision'){
    ts=ts.filter(isDecisionTask);
  }else if(f==='attention'){
    ts=ts.filter(t=>['failed','interrupted','rework'].includes(t.status)||(t.blocker&&t.blocker.length));
  }else if(f==='active'){
    ts=ts.filter(t=>['dispatched','working','rework','paused'].includes(t.status));
  }
  if(!ts.length){e.innerHTML=`<div class="empty">${f==='all'?'暂无任务':'当前筛选无匹配任务'}</div>`;return}
  e.innerHTML=ts.map(renderTaskRow).join('')
}
function renderTaskRow(t){
  const target={workflowId:t.workflow_id||state.workflowId,nodeId:t.node||t.stage||'',taskId:t.task_id,runId:t.run_id||'',paneId:t.pane_id||''};
  const isDone=['completed','committed','integrated','cleaned'].includes(t.status);
  const isAct=['working','dispatched','rework','in_progress'].includes(t.status);
  const isBlk=['blocked','failed'].includes(t.status);
  const iconCls=isDone?'done':isAct?'working':isBlk?'blocked':'pending';
  const iconChar=isDone?'✓':isAct?'●':isBlk?'!':'○';
  return `
  <div class="task" data-task-id="${esc(t.task_id)}" onclick="onTaskRowClick(event, '${esc(t.task_id)}')">
    <span class="task-icon ${iconCls}">${iconChar}</span>
    <div class="task-main">
      <span class="task-id">${esc(t.task_id)}</span>
      <span class="task-name">${esc(taskDisplayName(t))}</span>
    </div>
    <span class="task-agent"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="opacity:0.6"><circle cx="12" cy="8" r="4"/><path d="M6 20v-2a6 6 0 0 1 12 0v2"/></svg>${esc(t.agent||'未分配')}</span>
    <div class="task-status">${deliveryWaitHtml(t)}${taskBadge(t)}</div>
    <div class="task-menu">
      <button class="task-menu-btn" title="更多操作" onclick="toggleTaskMenu(event)">···</button>
      <div class="task-dropdown-menu" onclick="event.stopPropagation()">
        <button class="task-dropdown-item primary" onclick="closeAllTaskMenus();openSignoffChamber('${esc(t.task_id)}')">成果会签</button>
        <button class="task-dropdown-item" onclick="closeAllTaskMenus();showPane('${esc(t.pane_id||'')}','${esc(t.task_id)}')">查看工位</button>
        <button class="task-dropdown-item" onclick="closeAllTaskMenus();openControllerCockpit(${esc(JSON.stringify(target))})">在 Controller 中处理</button>
      </div>
    </div>
  </div>`;
}
return {
  setTaskFilter, copyCliCommand, copyCliCommandByActionId, executeControllerAction,
  locateControllerTask, openTerminalForTask, submitInteractionResponse, renderStages,
  renderTasks, renderTaskRow
};
})({
  state, api, toast,
  esc, humanReason, cleanStageLabel,
  taskBadge, deliveryWaitHtml, taskDisplayName,
  closeModal, openTaskDrawer:(...args)=>openTaskDrawer(...args), toggleDeepDrawer:(...args)=>toggleDeepDrawer(...args),
  switchDrawerTab:(...args)=>switchDrawerTab(...args), openControllerCockpit:(...args)=>openControllerCockpit(...args)
});
