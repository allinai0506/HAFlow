/* Operations view, anomaly confirmation and refresh lifecycle. */
var {
  startOpsTimer, stopOpsTimer, triggerOpsRefresh, showOpsCenter,
  exitOpsCenter, loadOpsCenter, filterOpsWorkflows, toggleOpsCardNodes,
  runOpsAnomalyAction, confirmOpsForcePass, showOpsAnomaly
} = (function({
  state, saveViewState, syncOpsUi,
  api, pageRead, toast,
  esc, humanNodeStatus, cleanStageLabel,
  formatElapsed, opsHealthClass, healthLabel,
  stopDashTimer, openWorkflowFromOps, refreshAll,
  paintCrumb, setWorkspaceMode, openModal,
  closeModal
}){

let opsTimer=null;
let opsCountdown=10;
function startOpsTimer(){
  stopOpsTimer();
  opsCountdown=10;
  updateOpsTimerBadge();
  opsTimer=setInterval(()=>{
    if(!state.opsMode){stopOpsTimer();return}
    if(document.hidden)return;
    opsCountdown--;
    updateOpsTimerBadge();
    if(opsCountdown<=0){
      opsCountdown=10;
      loadOpsCenter();
    }
  },1000);
}
function stopOpsTimer(){
  if(opsTimer){clearInterval(opsTimer);opsTimer=null}
}
function updateOpsTimerBadge(){
  const b=document.getElementById('opsTimerBadge');
  if(b)b.textContent='自动刷新 · '+opsCountdown+'s';
}
async function triggerOpsRefresh(){
  opsCountdown=10;
  updateOpsTimerBadge();
  const btn=document.getElementById('opsRefreshBtn');
  if(btn){btn.disabled=true;btn.textContent='刷新中…'}
  await loadOpsCenter();
  if(btn){btn.disabled=false;btn.textContent='⟳ 立即刷新'}
}
function showOpsCenter(){
  state.opsMode=true;state.dashMode=false;state.shellView='ops';stopDashTimer();
  saveViewState();
  syncOpsUi();
  document.getElementById('projectTitle').textContent='运维驾驶舱';
  document.getElementById('workflowSubject').textContent='四层运维视图 · 全局驾驶舱';
  document.getElementById('workflowSub').textContent='';
  document.getElementById('workflowSwitcher').style.display='none';
  document.getElementById('stages').innerHTML='';
  setWorkspaceMode('aux');
  document.getElementById('tasks').innerHTML='<div class="empty">正在加载运维驾驶舱数据…</div>';
  paintCrumb();
  loadOpsCenter();
  startOpsTimer();
}
async function exitOpsCenter(){
  stopOpsTimer();
  state.opsMode=false;
  state.shellView='workbench';
  saveViewState();
  syncOpsUi();
  await refreshAll();
}
async function loadOpsCenter(){
  try{
    const ops=await pageRead('ops','/api/ops-center?include_tasks=1');
    if(!ops||!state.opsMode)return;
    state.ops=ops;
    renderOpsCenter();
  }catch(e){
    if(state.opsMode){
      toast(e.message,true);
      const tasksEl=document.getElementById('tasks');
      if(tasksEl){
        tasksEl.innerHTML=`<div class="panel" style="padding:32px 20px;text-align:center;margin:16px"><div style="font-size:16px;font-weight:700;color:var(--bad);margin-bottom:8px">运维驾驶舱数据加载失败</div><div class="task-meta" style="margin-bottom:16px">${esc(e.message)}</div><button class="btn primary" onclick="loadOpsCenter()">点击重试</button></div>`;
      }
    }
  }
}

function filterOpsWorkflows(filter){
  state.opsWfFilter=filter;
  renderOpsCenter();
}
function toggleOpsCardNodes(e,wid){
  if(e)e.stopPropagation();
  state.opsExpandedNodes=state.opsExpandedNodes||{};
  state.opsExpandedNodes[wid]=!state.opsExpandedNodes[wid];
  const el=document.getElementById('nodes-'+wid);
  const textEl=document.getElementById('toggleText-'+wid);
  if(el){
    const open=state.opsExpandedNodes[wid];
    el.style.display=open?'flex':'none';
    if(textEl)textEl.textContent=open?'收起详情 ▲':'节点明细 ▼';
  }
}
let _opsActionBusy=false;
async function runOpsAnomalyAction(actType,wid,tid,node,options){
  options=options||{};
  if(actType==='force_pass'||actType==='force_pass_advance'){
    if(!options.confirmed){
      confirmOpsForcePass(wid,tid,node,options);
      return;
    }
  }
  if(_opsActionBusy){
    toast('操作正在执行中，请勿重复点击',true);
    return;
  }
  _opsActionBusy=true;
  try{
    toast('正在执行操作: '+actType+' …');
    const payload={
      type:actType,
      action_type:actType,
      workflow_id:wid,
      task_id:tid,
      node:node,
      stage:node,
      gate_node_id:node,
      note:options.reason||options.note||'',
      reason:options.reason||options.note||'',
      confirmed:!!options.confirmed,
      operator:options.operator||'human_ops'
    };
    if(options.expected_version!==undefined&&options.expected_version!==null&&options.expected_version!==''){
      payload.expected_version=parseInt(options.expected_version,10);
    }else if(options.version!==undefined&&options.version!==null&&options.version!==''){
      payload.expected_version=parseInt(options.version,10);
    }
    if(options.expected_pane_id||options.pane_id){
      payload.expected_pane_id=options.expected_pane_id||options.pane_id;
      payload.pane_id=options.pane_id||options.expected_pane_id;
    }
    const cockpit=await api('/api/workflow/controller-cockpit?workflow_id='+encodeURIComponent(wid));
    const action=(cockpit.actions||[]).find(candidate=>{
      const candidateType=candidate.api_payload?.type||candidate.api_payload?.action_type;
      if(candidateType!==actType)return false;
      if(tid&&candidate.target?.task_id!==tid)return false;
      return !node||!candidate.target?.node_id||candidate.target.node_id===node;
    });
    if(!action)throw new Error('当前驾驶舱没有匹配的可执行操作，请刷新后在 Controller 中处理');
    payload.action_id=action.action_id;
    payload.expected_identity=action.expected_identity;
    const res=await api('/api/controller/execute-action',{
      method:'POST',
      body:JSON.stringify(payload)
    });
    if(res.partial){
      toast('人工豁免已写入但推进失败: '+(res.advance_error||res.error||'后续推进未完成'),true);
    }else if(res.ok){
      if(actType==='force_pass'||actType==='force_pass_advance'){
        if(res.advanced)toast('门禁已豁免，工作流推进至: '+((res.advanced&&res.advanced.next_stage)||'下一阶段'));
        else toast('人工豁免记录已写入，等待调度推进');
      }else if(actType==='ops_repair'||actType==='retry'||actType==='rework'||actType==='redrive'){
        toast('恢复命令已执行 ('+(res.action_id||actType)+')，已向工位派发');
      }else if(actType==='clear_escalation'){
        toast('升级标记已清除，控制器重新尝试终化');
      }else{
        toast('操作成功: '+(res.task_id||res.action_id||'已生效'));
      }
    }else toast('操作未完成: '+(res.error||'执行未生效'),true);
    await loadOpsCenter();
  }catch(e){
    toast('操作失败: '+e.message,true);
  }finally{
    _opsActionBusy=false;
  }
}
function confirmOpsForcePass(wid,tid,node,options){
  options=options||{};
  const widEsc=esc(wid||'');
  const tidEsc=esc(tid||'');
  const nodeEsc=esc(node||'');
  const expVer=(options.expected_version!==undefined&&options.expected_version!==null)?options.expected_version:(options.version!==undefined&&options.version!==null?options.version:'');
  const expPane=esc(options.expected_pane_id||options.pane_id||'');
  openModal('人工强制放行确认 (高风险)',`
    <div style="line-height:1.6">
      <div style="margin-bottom:8px"><strong>目标工作流:</strong> <code>${widEsc}</code></div>
      <div style="margin-bottom:8px"><strong>目标门禁节点:</strong> <span class="badge warning">${nodeEsc||'未指定'}</span></div>
      ${tid?`<div style="margin-bottom:8px"><strong>关联任务:</strong> <code>${tidEsc}</code></div>`:''}
      <div class="ctl-effect" style="margin:12px 0;padding:12px;background:var(--warning-bg);border:1px solid var(--warning);border-radius:8px;color:var(--text-primary)">
        <strong>⚠️ 影响范围警告:</strong><br>
        本操作为<b>人工强制豁免</b>，将直接把该节点的门禁判定标记为 <code>pass</code>，并在工作流持久化记录豁免审计凭证，随后尝试推进工作流到后续阶段。<br>
        <b>绝不能用于普通自动修复！必须由工程师人工核验产物后方可放行。</b>
      </div>
      <div style="margin-top:12px">
        <label for="forcePassReason" style="font-weight:600;font-size:12px;color:var(--text-primary);display:block;margin-bottom:4px">放行原因与核验结论（必填）:</label>
        <textarea id="forcePassReason" class="input" style="width:100%;height:72px;box-sizing:border-box" placeholder="请详细填写人工豁免原因、核验人及结论..."></textarea>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn" onclick="closeModal()">取消</button>
        <button id="btnSubmitForcePass" class="btn warning">确认强制放行</button>
      </div>
    </div>
  `);
  const btn=document.getElementById('btnSubmitForcePass');
  if(btn){
    btn.onclick=()=>{
      const el=document.getElementById('forcePassReason');
      const reason=(el?el.value:'').trim();
      if(!reason){
        toast('必须填写强制放行原因',true);
        return;
      }
      btn.disabled=true;
      closeModal();
      const opts={confirmed:true,reason:reason};
      if(expVer!==''&&expVer!=='undefined'&&expVer!=='null'){
        opts.expected_version=parseInt(expVer,10);
      }
      if(expPane){
        opts.expected_pane_id=expPane;
        opts.pane_id=expPane;
      }
      runOpsAnomalyAction('force_pass_advance',wid,tid,node,opts);
    };
  }
}
window.searchOpsWorkflows=function(val){state.opsWfSearch=val;renderOpsCenter()};
window.clearOpsSearch=function(){state.opsWfSearch='';renderOpsCenter()};
window.filterOpsAnomalies=function(f){state.opsAnomFilter=f;renderOpsCenter()};
window.setOpsViewLayout=function(l){state.opsViewLayout=l;renderOpsCenter()};
window.toggleOpsGuide=function(){state.opsGuideOpen=state.opsGuideOpen===false?true:false;renderOpsCenter()};

function friendlyKind(k){
  if(k==='FINALIZE_ESCALATED')return {tag:'待终化确认',cls:'warning',tip:'任务已在工位完成，等待终化确认后结项推进'};
  if(k==='FAILED')return {tag:'执行失败',cls:'failed',tip:'智能体执行命令报错或异常中断，建议一键重试修复'};
  if(k==='BLOCKED'||k==='UPDATE_BLOCKED')return {tag:'门禁卡点',cls:'blocked',tip:'自动化门禁或质量检查未通过，需人工核验放行'};
  return {tag:k||'异常',cls:'neutral',tip:'工作流异常停滞，需人工关注处置'};
}
function formatAgeZh(sec){
  if(sec==null)return '刚刚';
  const n=Math.max(0,Math.round(sec));
  if(n>=86400)return Math.floor(n/86400)+'天前 (历史待决)';
  if(n>=3600)return Math.floor(n/3600)+'小时前';
  if(n>=60)return Math.floor(n/60)+'分钟前';
  return n+'秒前';
}

function renderOpsCenter(){
  const d=state.ops||{},b=d.boss||{},cards=d.workflow_cards||[],fleet=d.agent_fleet||[],anoms=d.anomalies||[];
  const blockedWfIds=b.blocked_workflows||[];
  state.opsWfFilter=state.opsWfFilter||'all';
  state.opsAnomFilter=state.opsAnomFilter||'all';
  state.opsViewLayout=state.opsViewLayout||'cards';
  if(state.opsGuideOpen===undefined)state.opsGuideOpen=true;

  let activeWfs=0,attentionWfs=0,completedWfs=0;
  cards.forEach(c=>{
    const isFailed=(c.tasks.failed>0)||(c.nodes||[]).some(n=>n.status==='failed');
    const isBlocked=(c.tasks.blocked>0)||(c.nodes||[]).some(n=>n.status==='blocked')||blockedWfIds.includes(c.workflow_id);
    const isActive=(c.tasks.active>0)||(c.nodes||[]).some(n=>n.status==='working');
    if(isActive)activeWfs++;
    if(isFailed||isBlocked)attentionWfs++;
    if(!isActive&&!isFailed&&!isBlocked)completedWfs++;
  });

  document.getElementById('mProjects').textContent=b.running_workflows||activeWfs||0;
  document.getElementById('mWorkflows').textContent=attentionWfs||b.attention_required||anoms.length;
  document.getElementById('mAgents').textContent=fleet.length;
  document.getElementById('mAlerts').textContent=completedWfs;
  const mSpans=document.querySelectorAll('.metrics .metric span');
  if(mSpans[0])mSpans[0].textContent='运行中工作流';if(mSpans[1])mSpans[1].textContent='需人工关注';if(mSpans[2])mSpans[2].textContent='在线执行者';if(mSpans[3])mSpans[3].textContent='已顺利结项';

  const q=(state.opsWfSearch||'').trim().toLowerCase();
  const filteredCards=cards.filter(c=>{
    const isFailed=(c.tasks.failed>0)||(c.nodes||[]).some(n=>n.status==='failed');
    const isBlocked=(c.tasks.blocked>0)||(c.nodes||[]).some(n=>n.status==='blocked')||blockedWfIds.includes(c.workflow_id);
    const isActive=(c.tasks.active>0)||(c.nodes||[]).some(n=>n.status==='working');
    if(state.opsWfFilter==='active'&&!isActive)return false;
    if(state.opsWfFilter==='attention'&&!(isFailed||isBlocked))return false;
    if(state.opsWfFilter==='completed'&&!(!isActive&&!isFailed&&!isBlocked))return false;
    if(q){
      const name=(c.workflow_label||'').toLowerCase(), id=(c.workflow_id||'').toLowerCase();
      if(!name.includes(q)&&!id.includes(q))return false;
    }
    return true;
  });

  const cardsHtml=filteredCards.length?filteredCards.map(c=>{
    const nodes=c.nodes||[],totalNodes=nodes.length;
    const completedNodes=nodes.filter(n=>n.status==='completed'||n.status==='superseded').length;
    const failedNode=nodes.find(n=>n.status==='failed'),blockedNode=nodes.find(n=>n.status==='blocked'),workingNode=nodes.find(n=>n.status==='working');
    let statusClass='is-idle',statusText='就绪',summaryIcon='○',summaryText='等待调度推进',summaryClass='is-idle';
    const startupKey=workflowStatusKey(c);
    if(startupKey==='startup_pending'||startupKey==='startup_failed'||startupKey==='abandoned'){
      const tone=workflowStatusTone(c);
      statusClass=summaryClass=tone==='danger'?'is-failed':tone==='warning'?'is-blocked':'is-idle';
      statusText=workflowStatusLabel(c);summaryIcon=startupKey==='startup_pending'?'○':'!';summaryText=workflowStartupHint(c)||statusText;
    }else if(failedNode||c.tasks.failed>0){
      statusClass='is-failed';statusText='执行失败';summaryIcon='🚨';summaryClass='is-failed';summaryText='关键阶段失败: '+cleanStageLabel(failedNode?failedNode.node_label:'未命名')+'（任务异常中断）';
    }else if(blockedNode||c.tasks.blocked>0||blockedWfIds.includes(c.workflow_id)){
      statusClass='is-blocked';statusText='阻塞阻断';summaryIcon='⏸';summaryClass='is-blocked';summaryText='关键阶段阻塞: '+cleanStageLabel(blockedNode?blockedNode.node_label:'未命名')+'（等待人工/前置条件）';
    }else if(workingNode||c.tasks.active>0){
      statusClass='is-working';statusText='正在运行';summaryIcon='⚡';summaryClass='is-working';summaryText='正在推进阶段: '+cleanStageLabel(workingNode?workingNode.node_label:'执行中');
    }else if(totalNodes>0&&completedNodes===totalNodes){
      statusClass='is-completed';statusText='已完成';summaryIcon='✓';summaryClass='is-completed';summaryText='全部 '+totalNodes+' 个阶段已全部顺利完成';
    }
    const pct=totalNodes>0?Math.round((completedNodes/totalNodes)*100):(c.tasks.completed>0?100:0);
    const isExpanded=!!(state.opsExpandedNodes&&state.opsExpandedNodes[c.workflow_id]);
    if(state.opsViewLayout==='table'){
      return `<tr class="ops-table-row" onclick="openWorkflowFromOps('${esc(c.workflow_id)}')">
        <td><span class="ops-card-status-badge ${statusClass}">${statusText}</span></td>
        <td><div class="ops-table-name" title="${esc(c.workflow_label||c.workflow_id)}">${esc(c.workflow_label||c.workflow_id)}</div><div class="task-meta"><code>${esc(c.workflow_id)}</code></div></td>
        <td><div class="ops-progress-bar"><div class="ops-progress-fill" style="width:${pct}%;"></div></div><div class="ops-table-sub">${completedNodes}/${totalNodes} 阶段 (${pct}%)</div></td>
        <td><div class="ops-table-sub">${c.tasks.active} 运行 · ${c.tasks.completed} 完成 · ${c.tasks.failed} 失败</div></td>
        <td><div class="ops-table-sub">${formatElapsed(c.runtime_seconds)}</div></td>
        <td><button class="btn mini primary" onclick="event.stopPropagation();openWorkflowFromOps('${esc(c.workflow_id)}')">进入 ➔</button></td>
      </tr>`;
    }
    return `<div class="ops-card ${statusClass}" data-wf-id="${esc(c.workflow_id)}">
      <div class="ops-card-head"><div class="ops-card-title-group"><span class="ops-card-dot ${statusClass}"></span><strong class="ops-card-name" title="${esc(c.workflow_label||c.workflow_id)}">${esc(c.workflow_label||c.workflow_id)}</strong><span class="ops-card-status-badge ${statusClass}">${statusText}</span></div>
      <div class="ops-card-actions"><span class="ops-card-time">${formatElapsed(c.runtime_seconds)}</span><button class="btn mini primary" onclick="event.stopPropagation();openWorkflowFromOps('${esc(c.workflow_id)}')">进入工作流 ➔</button></div></div>
      <div class="ops-card-meta-line"><code>${esc(c.workflow_id)}</code> · ${c.tasks.active} 运行 · ${c.tasks.completed} 完成 · ${c.tasks.blocked} 阻塞 · ${c.tasks.failed} 失败</div>
      <div class="ops-card-progress-wrap"><div class="ops-progress-bar"><div class="ops-progress-fill" style="width:${pct}%;"></div></div><div class="ops-progress-meta"><span>阶段进度: <b>${completedNodes}/${totalNodes}</b> (${pct}%)</span><span>总计任务: ${c.tasks.active+c.tasks.completed+c.tasks.blocked+c.tasks.failed} 个</span></div></div>
      <div class="ops-stage-callout ${summaryClass}"><div class="ops-callout-content"><span class="ops-callout-icon">${summaryIcon}</span><span class="ops-callout-text">${esc(summaryText)}</span></div><button class="ops-toggle-details-btn" onclick="toggleOpsCardNodes(event,'${esc(c.workflow_id)}')"><span id="toggleText-${esc(c.workflow_id)}">${isExpanded?'收起详情 ▲':('节点明细 ('+totalNodes+') ▼')}</span></button></div>
      <div id="nodes-${esc(c.workflow_id)}" class="ops-card-nodes-detail" style="display:${isExpanded?'flex':'none'}">${nodes.map(n=>`<span class="badge ${esc(n.status)}">${esc(cleanStageLabel(n.node_label))} · ${esc(humanNodeStatus(n.status))}</span>`).join('')}</div>
    </div>`;
  }).join(''):(state.opsViewLayout==='table'?'<tr><td colspan="6" class="empty">当前筛选条件下暂无工作流</td></tr>':'<div class="empty">当前筛选条件下暂无工作流</div>');

  const wfCardsHtml=state.opsViewLayout==='table'?`<div class="ops-table-wrap"><table class="ops-wf-table"><thead><tr><th style="width:84px">状态</th><th>工作流名称 / ID</th><th style="width:130px">阶段进度</th><th style="width:140px">任务概况</th><th style="width:80px">耗时</th><th style="width:72px">操作</th></tr></thead><tbody>${cardsHtml}</tbody></table></div>`:cardsHtml;

  let escAnoms=0,failAnoms=0,blockAnoms=0;
  anoms.forEach(a=>{
    if(a.kind==='FINALIZE_ESCALATED')escAnoms++;
    else if(a.kind==='FAILED')failAnoms++;
    else blockAnoms++;
  });
  const filteredAnoms=anoms.filter(a=>{
    if(state.opsAnomFilter==='escalated')return a.kind==='FINALIZE_ESCALATED';
    if(state.opsAnomFilter==='failed')return a.kind==='FAILED';
    if(state.opsAnomFilter==='blocked')return a.kind==='BLOCKED'||a.kind==='UPDATE_BLOCKED';
    return true;
  });

  const anomsHtml=filteredAnoms.length?filteredAnoms.map(a=>{
    const isBlocked=a.kind==='BLOCKED'||a.kind==='UPDATE_BLOCKED'||a.kind==='FINALIZE_ESCALATED';
    const info=friendlyKind(a.kind);
    const aOpts=`{expected_version:${a.version!==undefined&&a.version!==null?a.version:'null'},expected_pane_id:'${esc(a.pane_id||'')}',pane_id:'${esc(a.pane_id||'')}'}`;
    const isStale=(a.last_event_seconds||0)>86400;
    return `<div class="ops-anomaly-card ${isBlocked?'is-blocked':''} ${a.kind==='FINALIZE_ESCALATED'?'is-escalated':''}">
      <div class="ops-anomaly-head"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span class="badge ${info.cls}">${info.tag}</span><strong class="ops-link" onclick="openWorkflowFromOps('${esc(a.workflow_id)}')">${esc(a.workflow_id||'未知工作流')}</strong>${isStale?`<span class="ops-stale-tag" title="发生超过 24 小时">⏳ 历史遗留</span>`:''}</div><span class="task-meta">${formatAgeZh(a.last_event_seconds)}</span></div>
      <div class="ops-anomaly-body"><div class="task-meta">阶段: <b>${esc(cleanStageLabel(a.node||'—'))}</b> · 执行者: <b>${esc(a.agent||'—')}</b> · 任务: <code>${esc(a.task_id||'—')}</code></div><div class="ops-anomaly-event">诊断: ${esc(info.tip)} · 最近事件: ${esc(a.last_event||'无详细事件记录')}</div></div>
      <div class="ops-anomaly-actions">
        ${a.kind==='FINALIZE_ESCALATED'?`<button class="btn mini primary" onclick="runOpsAnomalyAction('clear_escalation','${esc(a.workflow_id||'')}','${esc(a.task_id||'')}','${esc(a.node||'')}',${aOpts})" title="清除升级标记，让调度器自动进行结项与推进">清除阻断并推进</button>`:''}
        ${a.kind==='FAILED'?`<button class="btn mini primary" onclick="runOpsAnomalyAction('ops_repair','${esc(a.workflow_id||'')}','${esc(a.task_id||'')}','${esc(a.node||'')}',${aOpts})" title="重新派发该任务给智能体工位执行">一键重试修复</button>`:''}
        ${isBlocked&&a.kind!=='FINALIZE_ESCALATED'?`<button class="btn mini warning" onclick="confirmOpsForcePass('${esc(a.workflow_id||'')}','${esc(a.task_id||'')}','${esc(a.node||'')}',${aOpts})" title="人工确认产物合格后强制通过">人工核验放行</button>`:''}
        ${a.pane_id?`<button class="btn mini" onclick="showPane('${esc(a.pane_id)}')">🖥️ 工位终端</button>`:''}
        ${a.task_id?`<button class="btn mini" onclick="openTaskDrawer('${esc(a.task_id)}')">任务详情</button>`:''}
        <button class="btn mini" onclick="showOpsAnomaly(${JSON.stringify(a).replaceAll('"','&quot;')})">详细诊断 ···</button>
      </div>
    </div>`;
  }).join(''):`<div class="ops-empty-clean"><div class="ops-empty-icon">🟢</div><div class="ops-empty-title">当前无此类待处理异常</div><div class="ops-empty-sub">全集群工作流与执行者健康运行中</div></div>`;

  const fleetHtml=fleet.length?`<div class="fleet-table"><div class="fleet-header"><span>执行者</span><span>健康状态</span><span>当前任务</span><span>负载</span><span>耗时</span></div>
    ${fleet.map(a=>`<div class="fleet-row"><strong>${esc(a.agent)}</strong><span class="${opsHealthClass(a.health)}">${esc(healthLabel(a.health))}</span><span class="task-id" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${a.current_task?`<a onclick="openTaskDrawer('${esc(a.current_task)}')">${esc(a.current_task)}</a>`:'— 空闲待命'}</span><span>负载 ${a.load}</span><span>${formatElapsed(a.runtime_seconds)}</span></div>`).join('')}
  </div>`:'<div class="empty">暂无执行者数据</div>';

  document.getElementById('tasks').innerHTML=`
    <div class="ops-guide-banner">
      <div class="ops-guide-head">
        <div class="ops-guide-title"><span class="ops-guide-icon">💡</span><strong>运维操作速查指引</strong><span class="ops-guide-sub">遇到卡点不知道怎么操作？参考下方处理方法：</span></div>
        <button class="ops-toggle-btn" onclick="toggleOpsGuide()">${state.opsGuideOpen?'收起指引 ▲':'展开指引 ▼'}</button>
      </div>
      ${state.opsGuideOpen?`<div class="ops-guide-grid">
        <div class="ops-guide-item"><div class="ops-guide-badge-row"><span class="badge warning">待终化确认</span> <strong>点击「清除阻断并推进」</strong></div><p>智能体已在工位完成代码产出，等待确认后结项。点击按钮清除阻断，调度器会自动完成结项并推进下一阶段。</p></div>
        <div class="ops-guide-item"><div class="ops-guide-badge-row"><span class="badge failed">执行失败</span> <strong>点击「一键重试修复」</strong></div><p>智能体在工位报错或退出。点击一键重试可重新下发任务；也可点击「工位终端」进入现场查看排查日志。</p></div>
        <div class="ops-guide-item"><div class="ops-guide-badge-row"><span class="badge blocked">门禁卡点</span> <strong>点击「人工核验放行」</strong></div><p>自动化质量门禁未通过。若工程师确认产物或代码质量符合预期，点击填写放行结论后即可强制推进。</p></div>
      </div>`:''}
    </div>
    <div class="ops-dashboard-grid">
      <div class="ops-col-left">
        <div class="ops-section-head">
          <div class="ops-section-title"><span>工作流全景大盘</span><span class="ops-badge" id="opsWfCount">${filteredCards.length} / ${cards.length}</span></div>
          <div class="ops-head-tools">
            <div class="ops-search-box">
              <input type="text" class="ops-search-input" placeholder="🔍 搜索工作流..." value="${esc(state.opsWfSearch||'')}" oninput="searchOpsWorkflows(this.value)">
              ${state.opsWfSearch?`<button class="ops-clear-btn" onclick="clearOpsSearch()" title="清空搜索">✕</button>`:''}
            </div>
            <div class="ops-view-toggle">
              <button class="ops-toggle-btn ${state.opsViewLayout!=='table'?'active':''}" onclick="setOpsViewLayout('cards')" title="卡片视图">☷ 卡片</button>
              <button class="ops-toggle-btn ${state.opsViewLayout==='table'?'active':''}" onclick="setOpsViewLayout('table')" title="列表视图">☰ 列表</button>
            </div>
          </div>
        </div>
        <div class="ops-filter-pills" role="tablist">
          <button class="ops-filter-pill ${state.opsWfFilter==='all'?'active':''}" onclick="filterOpsWorkflows('all')">全部 (${cards.length})</button>
          <button class="ops-filter-pill ${state.opsWfFilter==='attention'?'active':''} ${attentionWfs>0?'has-alert':''}" onclick="filterOpsWorkflows('attention')">需关注 (${attentionWfs})</button>
          <button class="ops-filter-pill ${state.opsWfFilter==='active'?'active':''}" onclick="filterOpsWorkflows('active')">运行中 (${activeWfs})</button>
          <button class="ops-filter-pill ${state.opsWfFilter==='completed'?'active':''}" onclick="filterOpsWorkflows('completed')">已完成 (${completedWfs})</button>
        </div>
        <div id="opsWorkflowCards" class="ops-cards-container">${wfCardsHtml}</div>
      </div>
      <div class="ops-col-right">
        <div class="ops-panel-anomaly">
          <div class="ops-section-head">
            <div class="ops-section-title"><span class="ops-anomaly-icon">🚨</span><span>待处置异常中枢</span><span class="ops-badge ${anoms.length?'danger':''}" id="opsAnomalyCount">${anoms.length}</span></div>
            <div class="ops-anom-filter-pills">
              <button class="ops-filter-pill ${state.opsAnomFilter==='all'?'active':''}" onclick="filterOpsAnomalies('all')">全部 (${anoms.length})</button>
              ${escAnoms>0?`<button class="ops-filter-pill ${state.opsAnomFilter==='escalated'?'active':''}" onclick="filterOpsAnomalies('escalated')">待终化 (${escAnoms})</button>`:''}
              ${failAnoms>0?`<button class="ops-filter-pill ${state.opsAnomFilter==='failed'?'active':''}" onclick="filterOpsAnomalies('failed')">失败 (${failAnoms})</button>`:''}
              ${blockAnoms>0?`<button class="ops-filter-pill ${state.opsAnomFilter==='blocked'?'active':''}" onclick="filterOpsAnomalies('blocked')">阻塞 (${blockAnoms})</button>`:''}
            </div>
          </div>
          <div class="ops-anom-tip">💡 提示：点击卡片对应按钮直接执行处置。若为历史待决，点击推进或重试即可恢复。</div>
          <div id="opsAnomalyList" class="ops-anomaly-container">${anomsHtml}</div>
        </div>
        <div class="ops-panel-fleet">
          <div class="ops-section-head"><div class="ops-section-title"><span>🤖 智能体执行机队</span><span class="ops-badge" id="opsFleetCount">${fleet.length} 在线</span></div><span class="task-meta">${fleet.some(a=>a.health==='BUSY')?'⚡ 任务执行中':'🟢 全员就绪待命'}</span></div>
          <div id="opsFleetList" class="fleet-table-wrap">${fleetHtml}</div>
        </div>
      </div>
    </div>
  `;

  if(state.opsViewLayout!=='table'){
    document.querySelectorAll('.ops-card').forEach((el)=>{
      const wid=el.dataset.wfId;
      if(wid){el.style.cursor='pointer';el.onclick=()=>openWorkflowFromOps(wid);}
    });
  }

  const alertsEl=document.getElementById('alerts');
  if(alertsEl)alertsEl.innerHTML=anoms.length?anoms.map(a=>`<div class="alert-row"><div><strong>${esc(a.kind)}</strong><div class="task-meta">${esc(a.task_id||'')} · ${esc(a.agent||'')} · ${esc(a.last_event||'')}</div></div><button class="mini" onclick="showOpsAnomaly(${JSON.stringify(a).replaceAll('"','&quot;')})">处理</button></div>`).join(''):'<div class="empty">暂无异常</div>';
}

function showOpsAnomaly(a){
  const wid=a.workflow_id||'', tid=a.task_id||'', node=a.node||'';
  const info=friendlyKind(a.kind);
  const aOpts=`{expected_version:${a.version!==undefined&&a.version!==null?a.version:'null'},expected_pane_id:'${esc(a.pane_id||'')}',pane_id:'${esc(a.pane_id||'')}'}`;
  const actionsList=(a.actions||[]).map(x=>{
    let clickFn="toast('建议操作："+esc(x)+"')";
    let label=x;
    if(x==='重试'||x==='自动修复'){label='一键重试修复';clickFn="closeModal();runOpsAnomalyAction('ops_repair','"+esc(wid)+"','"+esc(tid)+"','"+esc(node)+"',"+aOpts+")";}
    else if(x==='打开 Pane'&&a.pane_id){label='打开工位终端';clickFn="closeModal();showPane('"+esc(a.pane_id)+"')";}
    else if(x==='让总指挥处理'){label='让总指挥处理推进';clickFn="closeModal();runOpsAnomalyAction('advance','"+esc(wid)+"','"+esc(tid)+"','"+esc(node)+"',"+aOpts+")";}
    else if(x==='强制放行'||x==='人工放行'){label='人工核验放行';clickFn="closeModal();confirmOpsForcePass('"+esc(wid)+"','"+esc(tid)+"','"+esc(node)+"',"+aOpts+")";}
    else if(x==='清除升级标记'||x==='清除升级'){label='清除阻断并推进';clickFn="closeModal();runOpsAnomalyAction('clear_escalation','"+esc(wid)+"','"+esc(tid)+"','"+esc(node)+"',"+aOpts+")";}
    return `<button class="btn mini primary" onclick="${clickFn}">${esc(label)}</button>`;
  }).join('');

  openModal('异常诊断与处置 · '+info.tag,`
    <div class="ops-modal-guide">
      <div style="background:var(--bg-subtle);border:1px solid var(--border-subtle);padding:12px;border-radius:6px;margin-bottom:12px;font-size:12.5px;line-height:1.6">
        <div style="margin-bottom:4px"><strong>问题诊断:</strong> <span class="badge ${info.cls}">${info.tag}</span> ${esc(info.tip)}</div>
        <div class="task-meta">工作流: <code>${esc(wid)}</code> · 阶段: <b>${esc(cleanStageLabel(node||'—'))}</b> · 执行者: <b>${esc(a.agent||'—')}</b> · 任务: <code>${esc(tid||'—')}</code></div>
        <div class="task-meta" style="margin-top:4px">最近事件: ${esc(a.last_event||'无详细事件记录')} · 发生时间: ${formatAgeZh(a.last_event_seconds)}</div>
      </div>
      <div style="font-size:12px;font-weight:600;margin:12px 0 8px">💡 快捷处置动作（点击直接生效）：</div>
      <div class="actions" style="display:flex;gap:8px;flex-wrap:wrap">
        ${actionsList}
        ${tid?`<button class="btn mini" onclick="closeModal();openTaskDrawer('${esc(tid)}')">查看任务详情</button>`:''}
      </div>
      <details style="margin-top:16px;background:var(--bg-subtle);padding:8px 12px;border-radius:6px;border:1px solid var(--border-subtle)">
        <summary style="cursor:pointer;font-size:11.5px;color:var(--text-secondary);font-weight:600">🛠️ 工程师底层 CLI 调试命令 (点击展开)</summary>
        <pre style="margin-top:8px;font-size:11px;max-height:140px;overflow:auto">${esc(JSON.stringify(a.links||{},null,2))}</pre>
      </details>
    </div>
  `);
}
return {
  startOpsTimer, stopOpsTimer, triggerOpsRefresh, showOpsCenter,
  exitOpsCenter, loadOpsCenter, filterOpsWorkflows, toggleOpsCardNodes,
  runOpsAnomalyAction, confirmOpsForcePass, showOpsAnomaly
};
})({
  state, saveViewState, syncOpsUi,
  api, pageRead, toast,
  esc, humanNodeStatus, cleanStageLabel,
  formatElapsed, opsHealthClass, healthLabel,
  stopDashTimer:(...args)=>stopDashTimer(...args), openWorkflowFromOps, refreshAll,
  paintCrumb:(...args)=>paintCrumb(...args), setWorkspaceMode:(...args)=>setWorkspaceMode(...args), openModal,
  closeModal
});
