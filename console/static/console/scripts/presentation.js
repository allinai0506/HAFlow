/* Shared status, delivery and display helpers; no dependency on view capabilities. */
var {
  esc, jsArg, humanStatus, humanReason,
  humanNodeStatus, badge, cleanStageLabel, deliveryOf,
  deliveryReason, deliveryOverridesStatus, taskStatusLabel, taskBadge,
  deliveryWaitHtml, deliveryPipelineLine, relationText, relationClass,
  taskDisplayName, visibleAlerts, formatElapsed, opsHealthClass,
  healthLabel, agentStatusLabel, authHintLabel, taskDrawerNumTs
} = (function({
  state
}){

function esc(s){
  return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
}
// Emit a value as a single-quoted JS literal for use inside a double-quoted
// inline handler. JSON.stringify would emit double quotes and truncate the
// attribute at the first one.
function jsArg(v){
  const s=String(v??'').replace(/[\r\n]/g,'');
  return "'"+s.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/&/g,'&amp;').replace(/"/g,'&quot;')+"'";
}
function humanStatus(s){
  return ({
    waiting:'等待',pending:'待派发',dispatched:'已派发',working:'运行中',
    blocked:'已阻塞',agent_done:'执行者已完成',completed:'待收尾',
    committed:'已提交',integrated:'已集成',cleanup_ready:'待归档',
    cleaned:'已完成',failed:'失败',rework:'返工中',
    superseded:'已取代',in_progress:'运行中',empty:'无任务',
    finalizing:'收尾中',mixed:'处理中',active:'运行中'
  })[s]||s||'未知'
}
function humanReason(value){
  const text=String(value??'').trim();
  const labels={
    inner_loop_exhausted:'工位内循环已耗尽全部重试次数，无法自愈，需要总指挥仲裁'
  };
  return Object.prototype.hasOwnProperty.call(labels, text)?labels[text]:text;
}
function humanNodeStatus(s){
  return s==='completed'?'已完成':humanStatus(s);
}
function badge(s){return `<span class="badge ${esc(s)}">${esc(humanStatus(s))}</span>`}
function cleanStageLabel(s){
  return String(s??'').replace(/^\d+\s*(?=[\u4e00-\u9fff])/, '')
}

// Delivery pipeline (issue #253). A committed git task the scheduler still
// owns is NOT terminal: the badge has to name the stage it is waiting at, and
// the reason, instead of the terminal-sounding raw status label「已提交」.
// Every field below comes from `herdr.delivery_pipeline` — never re-derived
// here, so the console cannot disagree with the scheduler about ownership.
// Tasks the scheduler does not own keep their existing status label.
function deliveryOf(t){return (t&&t.delivery)||null;}
function deliveryGates(t){
  const d=deliveryOf(t);if(!d)return [];
  return (d.gates||[]).map(g=>cleanStageLabel(g.label||g.id)).filter(Boolean);
}
function deliveryReason(t){
  const d=deliveryOf(t);if(!d)return '';
  const gates=deliveryGates(t);
  if(gates.length)return '等待门禁: '+gates.join('、');
  return d.wait_kind==='owner'?String(d.owner_note||''):'';
}
function deliveryOverridesStatus(t){
  const d=deliveryOf(t);
  return Boolean(d&&d.scheduler_owned&&d.stage_label);
}
function taskStatusLabel(t){
  const d=deliveryOf(t);
  return deliveryOverridesStatus(t)?d.stage_label:humanStatus((t||{}).status);
}
function taskStatusClass(t){
  if(deliveryOverridesStatus(t))return 'working';
  return ((t||{}).status)||'waiting';
}
function taskBadge(t){
  const reason=deliveryOverridesStatus(t)?deliveryReason(t):'';
  return `<span class="badge ${esc(taskStatusClass(t))}"${reason?` title="${esc(reason)}"`:''}>${esc(taskStatusLabel(t))}</span>`;
}
function deliveryWaitHtml(t){
  if(!deliveryOverridesStatus(t))return '';
  const reason=deliveryReason(t);
  return reason?`<span class="task-wait" title="${esc(reason)}">${esc(reason)}</span>`:'';
}
function deliveryClock(ts){
  const n=taskDrawerNumTs(ts);if(n===null)return '';
  const d=new Date(n>1e12?n:n*1000);if(isNaN(d.getTime()))return '';
  const p=v=>String(v).padStart(2,'0');
  return p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());
}
// commit → integration branch → candidate publication, all existing facts.
// Only git tasks belong here; the commit segment is what makes the row a
// delivery statement rather than noise on a non-git or uncommitted task.
function deliveryPipelineLine(t){
  const d=deliveryOf(t);if(!d||d.integration_mode!=='git')return '';
  const parts=[];
  if(d.commit_short)parts.push('commit='+d.commit_short);
  if(d.integration_branch)parts.push(d.integration_branch);
  if(!parts.length)return '';
  parts.push('候选发布 '+(d.candidate_published_at?deliveryClock(d.candidate_published_at):'时间未记录'));
  return parts.join(' → ');
}

function relationText(s){
  return s.relation==='current_factory'?'当前工厂':
         s.relation==='historical'?'历史空间':'独立终端'
}
function relationClass(s){
  return s.relation==='current_factory'?'good-text':
         s.relation==='historical'?'muted':'warn-text'
}
function taskSequence(t){
  const m=String(t.task_id||'').match(/-(?:req|plan|impl|test|fix|rev|wrap)-([0-9]+)$/i);
  return m?parseInt(m[1],10):null
}
function taskKind(t){
  const id=String(t.task_id||'').toLowerCase();
  const st=String(t.stage||'').toLowerCase();
  if(id.includes('-fix-'))return '修复';
  if(id.includes('-test-')){
    const n=taskSequence(t);
    return n&&n>1?'回归测试':'测试'
  }
  if(id.includes('-req-')||st==='requirements')return '需求分析';
  if(id.includes('-plan-')||st==='plan')return '计划';
  if(id.includes('-impl-')||st==='implementation')return '实现';
  if(id.includes('-rev-')||st==='review')return '评审';
  if(id.includes('-wrap-')||st==='wrapup')return '收尾';
  return t.stage_label||t.stage||'任务'
}
function taskDisplayName(t){
  const n=taskSequence(t);
  return n?`${taskKind(t)} #${n}`:taskKind(t)
}
function visibleAlerts(){
  const all=(state.overview&&state.overview.alerts)||[];
  if(!state.projectId)return [];
  let rows=all.filter(a=>a.project_id===state.projectId);
  if(state.workflowId){
    const exact=rows.filter(a=>a.workflow_id===state.workflowId);
    if(exact.length)rows=exact
  }
  return rows
}

function formatElapsed(seconds){
  if(seconds==null)return '—';
  const n=Math.max(0,Math.round(seconds));
  if(n<60)return n+'s';
  const m=Math.floor(n/60),s=n%60;
  if(m<60)return m+'m '+String(s).padStart(2,'0')+'s';
  return Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m';
}
function opsHealthClass(s){return ['BLOCKED','FAILED','STALE'].includes(s)?'danger-text':s==='BUSY'?'warn-text':'good-text'}
function healthLabel(s){
  return ({BUSY:'忙碌',IDLE:'空闲',BLOCKED:'阻塞',FAILED:'失败',STALE:'失联',UNKNOWN:'未知'})[s]||s||'未知'
}
function agentStatusLabel(s){
  return ({ready:'可用',disabled:'已禁用',missing:'未安装',working:'运行中'})[s]||s||'未知'
}
function authHintLabel(s){
  return ({present:'已配置',missing:'未配置',unknown:'未知'})[s]||s||'未知'
}
function taskDrawerNumTs(v){const n=parseFloat(v);return Number.isFinite(n)?n:null}

return {
  esc, jsArg, humanStatus, humanReason,
  humanNodeStatus, badge, cleanStageLabel, deliveryOf,
  deliveryReason, deliveryOverridesStatus, taskStatusLabel, taskBadge,
  deliveryWaitHtml, deliveryPipelineLine, relationText, relationClass,
  taskDisplayName, visibleAlerts, formatElapsed, opsHealthClass,
  healthLabel, agentStatusLabel, authHintLabel, taskDrawerNumTs
};
})({
  state
});
