/* 用户任务收件箱前端控制器与视图 */
var {
  showInbox, stopInboxTimer, inspectWorkflow, loadInboxData, loadMoreInboxTasks, showNewInboxTaskModal, showInboxTaskDetail,
  showConfirmScopeModal, changeTaskPriority, moveTaskOrder, cancelInboxTask,
  retryInboxTask, setInboxStatusFilter, setInboxProjectFilter, setInboxPriorityFilter,
  triggerInboxSweep, showChangePriorityModal, submitChangePriority,
  submitNewInboxTask, submitConfirmScope, onInboxProjectSelectChange,
  onInboxInstructionBlur, onConfirmScopeProjectChange
} = (function({
  state, api, esc, badge, formatElapsed, paintCrumb, setWorkspaceMode,
  openModal, closeModal, toast, showConfirmModal
}){

let inboxState = {
  tasks: [], total: 0, stats: null,
  filterStatus: 'ALL', filterProject: 'ALL', filterPriority: 'ALL',
  autoRefreshTimer: null, activeTaskId: null, pageSize: 50,
};
let inboxSubmitting = false;
const intake = typeof fetchIntakeActiveItems === 'function' ? {
  fetch: fetchIntakeActiveItems, renderCards: renderIntakeCards, renderPagination: renderIntakePagination,
  renderEmpty: renderIntakeEmpty, authButtonHtml: getIntakeAuthButtonHtml, bindEvents: bindIntakeEvents,
  count: getIntakeItemsCount,
} : {
  fetch: () => Promise.resolve(), renderCards: () => '', renderPagination: () => '',
  renderEmpty: () => '', authButtonHtml: () => '', bindEvents: () => {}, count: () => 0,
};
const pendingInboxSubmissions = new Map();
let recommendedScope = null;

function formatTime(ts){
  if(!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', {hour12:false, month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
}

function priorityLabel(p){
  const map = {URGENT:'紧急', HIGH:'高优', NORMAL:'常规', LOW:'低优'};
  return map[(p||'NORMAL').toUpperCase()] || p;
}

function statusBadgeHtml(t){
  const s = t.status || 'DRAFT';
  if(s === 'PENDING') return `<span class="status-pill pill-pending">排队中${t.queue_position ? ' #' + t.queue_position : ''}</span>`;
  if(s === 'CLAIMED' || s === 'RUNNING') return `<span class="status-pill pill-running"><span class="status-dot-pulse"></span>${s === 'CLAIMED' ? '已认领' : '执行中'}</span>`;
  if(s === 'WAITING') return `<span class="status-pill pill-waiting">等待拍板</span>`;
  if(s === 'COMPLETED') return `<span class="status-pill pill-completed">已完成</span>`;
  if(s === 'FAILED') return `<span class="status-pill pill-failed">执行失败</span>`;
  if(s === 'DRAFT') return `<span class="status-pill pill-draft">草稿待确认</span>`;
  if(s === 'CANCELLED') return `<span class="status-pill pill-cancelled">已取消</span>`;
  return `<span class="status-pill">${esc(s)}</span>`;
}

async function loadInboxData(isAppend, isReset){
  try{
    const offset = isAppend ? inboxState.tasks.length : 0;
    const currentCount = isReset ? (inboxState.pageSize || 50) : Math.max(inboxState.pageSize || 50, inboxState.tasks.length);
    const limit = isAppend ? (inboxState.pageSize || 50) : currentCount;
    const qParts = [`limit=${limit}`, `offset=${offset}`];
    const statusParam = inboxState.filterStatus === 'RUNNING' ? 'RUNNING,CLAIMED' : inboxState.filterStatus;
    if(statusParam && !['ALL','MINE','PENDING_ASSIGNMENT'].includes(statusParam)) qParts.push('status=' + encodeURIComponent(statusParam));
    if(inboxState.filterProject && inboxState.filterProject !== 'ALL') qParts.push('project_id=' + encodeURIComponent(inboxState.filterProject));
    if(inboxState.filterPriority && inboxState.filterPriority !== 'ALL') qParts.push('priority=' + encodeURIComponent(inboxState.filterPriority));

    const view = inboxState.filterStatus;
    const [tasksRes, statsRes] = await Promise.all([
      view === 'PENDING_ASSIGNMENT' ? Promise.resolve({ok:true,tasks:[],total:0}) : api('/api/inbox/tasks?' + qParts.join('&')),
      isAppend ? Promise.resolve(null) : api('/api/inbox/stats'),
      intake.fetch(qParts, view, isReset),
    ]);
    if(tasksRes && tasksRes.ok){
      inboxState.tasks = isAppend ? inboxState.tasks.concat(tasksRes.tasks || []) : (tasksRes.tasks || []);
      inboxState.total = tasksRes.total || 0;
    }
    if(statsRes && statsRes.ok) inboxState.stats = statsRes;
    renderInboxView();
    updateInboxRailBadge();
  }catch(e){
    console.error('Failed to load inbox data:', e);
  }
}

function loadMoreInboxTasks(){ return loadInboxData(true); }

function updateInboxRailBadge(){
  const dot = document.getElementById('railInboxDot');
  if(!dot) return;
  const counts = (inboxState.stats && inboxState.stats.counts) || {};
  const active = (counts.PENDING || 0) + (counts.RUNNING || 0) + (counts.CLAIMED || 0) + (counts.WAITING || 0);
  dot.style.display = active > 0 ? 'block' : 'none';
  if(active > 0) dot.textContent = active > 99 ? '99+' : String(active);
}

function renderInboxView(){
  const container = document.getElementById('inboxTabView');
  if(!container) return;
  const counts = (inboxState.stats && inboxState.stats.counts) || {};
  const total = inboxState.stats ? inboxState.stats.total : 0;
  const busyWs = (inboxState.stats && inboxState.stats.busy_workspaces) || {};
  const projects = (state.overview && state.overview.projects) || [];

  const metrics = [
    {k:'ALL', l:'全部任务', v:total, c:''},
    {k:'PENDING', l:'排队待执行', v:counts.PENDING||0, c:'#d97706'},
    {k:'RUNNING', l:'正在执行', v:(counts.RUNNING||0)+(counts.CLAIMED||0), c:'#2563eb'},
    {k:'WAITING', l:'等待拍板', v:counts.WAITING||0, c:'#7c3aed'},
    {k:'COMPLETED', l:'已完成', v:counts.COMPLETED||0, c:'#10b981'},
    {k:'FAILED', l:'执行失败', v:counts.FAILED||0, c:'#ef4444'},
    {k:'DRAFT', l:'草稿待确认', v:counts.DRAFT||0, c:'#6b7280'},
  ];

  let html = `<div class="inbox-view"><div class="inbox-metrics">` +
    metrics.map(m => `<div class="inbox-metric-card ${inboxState.filterStatus===m.k?'active':''}" onclick="setInboxStatusFilter('${m.k}')"><span class="inbox-metric-val"${m.c?' style="color:'+m.c+'"':''}>${m.v}</span><span class="inbox-metric-label">${m.l}</span></div>`).join('') +
    `</div><div class="inbox-toolbar"><div class="inbox-filters"><div class="inbox-filter-group">` +
    ['ALL:全部','MINE:我的任务','PENDING_ASSIGNMENT:待分配','DRAFT:草稿','PENDING:待执行','RUNNING:执行中','COMPLETED:已完成','FAILED:失败'].map(s => {
      const [k, l] = s.split(':');
      return `<button class="inbox-tab-btn ${inboxState.filterStatus===k?'active':''}" onclick="setInboxStatusFilter('${k}')">${l}</button>`;
    }).join('') +
    `</div><select class="inbox-select" onchange="setInboxProjectFilter(this.value)"><option value="ALL">全部项目</option>` +
    projects.map(p => `<option value="${esc(p.project_id)}"${inboxState.filterProject===p.project_id?' selected':''}>${esc(p.project_name||p.project_id)}</option>`).join('') +
    `</select><select class="inbox-select" onchange="setInboxPriorityFilter(this.value)"><option value="ALL">全部优先级</option>` +
    ['URGENT:紧急 (URGENT)','HIGH:高优 (HIGH)','NORMAL:常规 (NORMAL)','LOW:低优 (LOW)'].map(o => {
      const [k, l] = o.split(':');
      return `<option value="${k}"${inboxState.filterPriority===k?' selected':''}>${l}</option>`;
    }).join('') +
    `</select></div><div class="inbox-actions">` +
    intake.authButtonHtml() +
    `<button class="btn" onclick="triggerInboxSweep()" title="立即触发工作区调度扫描">调度扫描</button>` +
    `<button class="btn" onclick="loadInboxData()" title="刷新任务列表">刷新</button>` +
    `<button class="btn primary" onclick="showNewInboxTaskModal()"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><span>提交新任务</span></button>` +
    `</div></div><div class="inbox-task-list">`;

  html += intake.renderCards(inboxState.tasks);
  html += intake.renderPagination();
  const intakeCount = intake.count();
  if(!inboxState.tasks.length && !intakeCount && inboxState.filterStatus === 'PENDING_ASSIGNMENT'){
    html += intake.renderEmpty(inboxState.filterStatus);
  } else if(!inboxState.tasks.length && !intakeCount){
    html += `<div class="empty" style="padding:48px 0;background:var(--bg-surface,#fff);border:1px dashed var(--border-default,#e5e7eb);border-radius:8px"><strong>收件箱暂无任务</strong><span class="muted" style="margin-top:6px;font-size:13px">点击右上角【提交新任务】，随时向任何项目下达指令，系统将自动排队与接续调度。</span></div>`;
  } else {
    inboxState.tasks.forEach(t => {
      const prio = t.priority || 'NORMAL';
      const isPending = t.status === 'PENDING';
      const isDraft = t.status === 'DRAFT';
      const isFailed = t.status === 'FAILED';
      let busyNotice = '';
      if(isPending && t.workspace_id && busyWs[t.workspace_id]){
        const cur = busyWs[t.workspace_id];
        if(cur.task_id !== t.id) busyNotice = `<span style="color:#d97706;font-size:12px;margin-left:8px">（工作区当前执行中：${esc(cur.title)}）</span>`;
      }
      html += `<div class="inbox-card status-${(t.status||'draft').toLowerCase()}">` +
        `<div class="inbox-card-head"><div class="inbox-card-title-row"><span class="prio-pill prio-${prio.toLowerCase()}">${priorityLabel(prio)}</span><span class="inbox-card-title" title="${esc(t.title)}">${esc(t.title)}</span>${busyNotice}</div><div class="inbox-card-badges">${statusBadgeHtml(t)}</div></div>` +
        `<div class="inbox-card-instruction" title="${esc(t.instruction)}">${esc(t.instruction)}</div>` +
        `<div class="inbox-card-meta"><span class="inbox-meta-item">📁 <b>${esc(t.project_id||'未指定项目')}</b></span><span class="inbox-meta-item">🖥️ 空间: <code>${esc(t.workspace_id||'未绑定')}</code></span><span class="inbox-meta-item">⏱️ 创建: ${formatTime(t.created_at)}</span>` +
        (t.started_at ? `<span class="inbox-meta-item">🚀 启动: ${formatTime(t.started_at)}</span>` : '') +
        (t.completed_at ? `<span class="inbox-meta-item">🏁 结束: ${formatTime(t.completed_at)}</span>` : '') +
        (t.workflow_id ? `<span class="inbox-meta-item">🔗 <a href="#" onclick="event.preventDefault();inspectWorkflow('${esc(t.workflow_id)}')">${esc(t.workflow_id)}</a></span>` : '') +
        (isFailed && t.error ? `<span class="inbox-meta-item danger-text" title="${esc(t.error)}">⚠️ 失败: ${esc(t.error.slice(0,60))}</span>` : '') +
        `</div><div class="inbox-card-foot"><div class="inbox-foot-left">ID: <code>${esc(t.id)}</code> · 来源: ${esc(t.source_type||'manual')}</div><div class="inbox-foot-actions">` +
        (isDraft ? `<button class="btn-inbox-op primary-op" onclick="showConfirmScopeModal('${esc(t.id)}')">确认归属并排队</button>` : '') +
        (isPending ? `<button class="btn-inbox-op" onclick="moveTaskOrder('${esc(t.id)}','up')" title="排队前移">↑ 上移</button><button class="btn-inbox-op" onclick="moveTaskOrder('${esc(t.id)}','down')" title="排队后移">↓ 下移</button><button class="btn-inbox-op" onclick="showChangePriorityModal('${esc(t.id)}','${prio}')">调优先级</button><button class="btn-inbox-op danger" onclick="cancelInboxTask('${esc(t.id)}')">取消</button>` : '') +
        (isFailed ? `<button class="btn-inbox-op primary-op" onclick="retryInboxTask('${esc(t.id)}')">重新排队</button>` : '') +
        ((isDraft || t.status === 'WAITING') ? `<button class="btn-inbox-op danger" onclick="cancelInboxTask('${esc(t.id)}')">取消</button>` : '') +
        `<button class="btn-inbox-op" onclick="showInboxTaskDetail('${esc(t.id)}')">查看详情</button>` +
        `</div></div></div>`;
    });
    if(inboxState.tasks.length < inboxState.total){
      html += `<div style="text-align:center;padding:12px 0"><button class="btn" onclick="loadMoreInboxTasks()" style="font-size:12px;padding:5px 16px">加载更多任务 (已展示 ${inboxState.tasks.length} / 共 ${inboxState.total} 条)</button></div>`;
    }
  }
  html += `</div></div>`;
  container.innerHTML = html;
  intake.bindEvents(container, loadInboxData, renderInboxView);
}

function setInboxStatusFilter(s){ inboxState.filterStatus = s; return loadInboxData(false, true); }
function setInboxProjectFilter(p){ inboxState.filterProject = p; return loadInboxData(false, true); }
function setInboxPriorityFilter(pr){ inboxState.filterPriority = pr; return loadInboxData(false, true); }

async function triggerInboxSweep(){
  try{
    toast('正在触发工作区调度扫描…');
    const res = await api('/api/inbox/sweep', {method:'POST'});
    const sw = res.sweep || {};
    toast(`调度扫描完成：启动 ${(sw.launched||[]).length} 项，收敛 ${(sw.reconciled||[]).length} 项`);
    await loadInboxData();
  }catch(e){ toast('调度扫描异常: ' + e.message, true); }
}

async function showNewInboxTaskModal(){
  const projects = (state.overview && state.overview.projects) || [];
  const projOptions = projects.map(p => `<option value="${esc(p.project_id)}" data-ws="${esc(p.workspace_id||'')}">${esc(p.project_name||p.project_id)} (${esc(p.workspace_id||'无空间')})</option>`).join('');
  openModal('提交新任务到统一收件箱', `
    <div class="form" style="line-height:1.5">
      <div id="inboxDetectBanner" class="inbox-detect-banner" style="display:none"></div>
      <label for="inboxTitle">任务标题<span class="danger-text">*</span></label>
      <input id="inboxTitle" onblur="onInboxInstructionBlur()" placeholder="例如：适配深色模式切换 / 修复结算浮点精度" style="margin-bottom:10px">
      <label for="inboxInstruction">任务需求指令<span class="danger-text">*</span></label>
      <textarea id="inboxInstruction" rows="4" placeholder="请详细描述任务需求。支持直接输入代码路径，系统将自动识别推荐所属项目…" onblur="onInboxInstructionBlur()" style="margin-bottom:10px"></textarea>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:10px">
        <div><label for="inboxProject">归属项目 <span class="danger-text" aria-hidden="true">*</span></label><select id="inboxProject" required onchange="onInboxProjectSelectChange(true)"><option value="">-- 选择项目 --</option>${projOptions}</select></div>
        <div><label for="inboxWorkspace">归属工作区 <span class="danger-text">*</span></label><input id="inboxWorkspace" placeholder="选择项目后自动确定" readonly></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
        <div><label for="inboxPriority">优先级</label><select id="inboxPriority"><option value="NORMAL" selected>常规 (NORMAL)</option><option value="URGENT">紧急 (URGENT)</option><option value="HIGH">高优 (HIGH)</option><option value="LOW">低优 (LOW)</option></select></div>
        <div style="display:flex;align-items:flex-end"><label class="inbox-scope-checkbox-label"><input type="checkbox" id="inboxAutoConfirm"><span>提交后立即排队等待执行</span></label></div>
      </div>
      <div class="muted" style="font-size:12px;margin-bottom:14px">请确认项目与工作区。未勾选立即排队时，任务会保存为草稿。</div>
      <div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button id="inboxSubmit" class="btn primary" onclick="submitNewInboxTask()">确认归属并提交</button></div>
    </div>`);
  inboxState.userSelectedProject = false;
  recommendedScope = null;
  if(inboxSubmitting) document.getElementById('inboxSubmit').disabled = true;
}

function onInboxProjectSelectChange(isManual = false){
  const sel = document.getElementById('inboxProject');
  const wsInput = document.getElementById('inboxWorkspace');
  if(!sel || !wsInput) return;
  const opt = sel.selectedOptions[0];
  wsInput.value = opt ? opt.dataset.ws || '' : '';
  if(isManual){
    inboxState.userSelectedProject = Boolean(sel.value);
  }
}

async function onInboxInstructionBlur(){
  const instruction = document.getElementById('inboxInstruction');
  const ins = (instruction?.value || '').trim();
  const ti = document.getElementById('inboxTitle');
  if(!ins) return;
  if(ti && !ti.value.trim()){
    for(const line of ins.split(/[\r\n]+/)){
      const clean = line.replace(/^(#{1,6}\s+|[-*+]+\s+|\d+[.、)]\s*|\[[ xX]\]\s*)+/, '').trim();
      if(clean && !clean.startsWith('## 需求') && clean !== '需求'){ ti.value = clean.slice(0, 50); break; }
    }
  }
  const title = (ti?.value || '').trim();
  try{
    const res = await api('/api/inbox/detect', {method:'POST', body:JSON.stringify({instruction:ins, title})});
    if(inboxSubmitting || document.getElementById('inboxSubmit')?.disabled
      || instruction !== document.getElementById('inboxInstruction')
      || instruction.value.trim() !== ins || (ti?.value || '').trim() !== title) return;
    const banner = document.getElementById('inboxDetectBanner');
    if(res && banner){
      banner.style.display = 'flex';
      const isConfident = Boolean(res.is_confident);
      banner.className = 'inbox-detect-banner ' + (isConfident ? '' : 'low-conf');
      if(isConfident && res.recommended_project_id){
        banner.innerHTML = `🎯 <b>智能推荐：</b> 项目【${esc(res.project_name||res.recommended_project_id)}】 · 空间【${esc(res.recommended_workspace_id)}】（置信度 ${Math.round(res.confidence*100)}% - ${esc(res.reason)}）`;
        const sel = document.getElementById('inboxProject');
        if(sel && !inboxState.userSelectedProject){
          sel.value = res.recommended_project_id;
          recommendedScope = {title, instruction:ins};
          onInboxProjectSelectChange(false);
        }
      } else {
        banner.innerHTML = `⚠️ <b>推荐置信度不足（${Math.round((res.confidence||0)*100)}%）：</b> ${esc(res.reason||'未达到自动确认门限')}。请手动选择项目并确认工作区。`;
        const sel = document.getElementById('inboxProject');
        const wsInput = document.getElementById('inboxWorkspace');
        if(!inboxState.userSelectedProject){
          if(sel) sel.value = '';
          if(wsInput) wsInput.value = '';
          recommendedScope = null;
        }
      }
    }
  }catch(e){
    console.error('Scope detect error:', e);
  }
}

async function submitNewInboxTask(){
  const button = document.getElementById('inboxSubmit');
  if(!button || button.disabled || inboxSubmitting) return;
  const title = (document.getElementById('inboxTitle')?.value || '').trim();
  const ins = (document.getElementById('inboxInstruction')?.value || '').trim();
  const project = document.getElementById('inboxProject');
  const pid = (project?.value || '').trim();
  const wid = (document.getElementById('inboxWorkspace')?.value || '').trim();
  const prio = (document.getElementById('inboxPriority')?.value || 'NORMAL').trim();
  const shouldConfirm = Boolean(document.getElementById('inboxAutoConfirm')?.checked);

  if(!title) return toast('请输入任务标题', true);
  if(!ins) return toast('请输入任务指令/需求', true);
  const missingScope = !pid || !wid;
  const staleScope = !inboxState.userSelectedProject && recommendedScope
    && (recommendedScope.title !== title || recommendedScope.instruction !== ins);
  if(missingScope || staleScope){
    project?.focus();
    return toast(missingScope ? '请选择有归属工作区的项目' : '任务内容已修改，请重新确认归属', true);
  }

  const payload = {
    title, instruction:ins, project_id:pid, workspace_id:wid,
    priority:prio, auto_confirm:shouldConfirm,
    confirmed_by:shouldConfirm ? 'user' : null
  };
  const submission = JSON.stringify(payload);
  inboxSubmitting = true;
  button.disabled = true;
  let completed = false;
  try{
    if(!pendingInboxSubmissions.has(submission)){
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      pendingInboxSubmissions.set(submission, Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join(''));
    }
    await api('/api/inbox/tasks', {
      method: 'POST',
      body: JSON.stringify({...payload, idempotency_key:pendingInboxSubmissions.get(submission)})
    });
    completed = true;
    pendingInboxSubmissions.delete(submission);
    if(button === document.getElementById('inboxSubmit')) closeModal();
    toast(shouldConfirm ? '任务已提交并进入执行队列！' : '任务已保存为草稿');
    await loadInboxData(false, true);
  }catch(e){
    toast('提交失败: ' + e.message, true);
  }finally{
    inboxSubmitting = false;
    const activeButton = document.getElementById('inboxSubmit');
    if(activeButton && (!completed || activeButton !== button)) activeButton.disabled = false;
  }
}

async function showInboxTaskDetail(taskId){
  try{
    const res = await api('/api/inbox/task?id=' + encodeURIComponent(taskId));
    if(!res || !res.ok) return toast('获取任务详情失败', true);
    const t = res.task;
    openModal('收件箱任务详情', `
      <div style="line-height:1.6;font-size:13px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;border-bottom:1px solid var(--border-default,#e5e7eb);padding-bottom:8px">
          <strong style="font-size:16px">${esc(t.title)}</strong><div>${statusBadgeHtml(t)}</div>
        </div>
        <div style="margin-bottom:12px"><label style="font-weight:600;color:var(--text-muted,#6b7280)">任务指令</label><div style="background:var(--bg-subtle,#f8fafc);border:1px solid var(--border-default,#e5e7eb);border-radius:6px;padding:10px;white-space:pre-wrap;margin-top:4px">${esc(t.instruction)}</div></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
          <div><b>任务 ID:</b> <code>${esc(t.id)}</code></div><div><b>优先级:</b> <span class="prio-pill prio-${(t.priority||'normal').toLowerCase()}">${priorityLabel(t.priority)}</span></div>
          <div><b>归属项目:</b> ${esc(t.project_id||'—')}</div><div><b>归属空间:</b> <code>${esc(t.workspace_id||'—')}</code></div>
          <div><b>创建时间:</b> ${formatTime(t.created_at)}</div><div><b>更新时间:</b> ${formatTime(t.updated_at)}</div>
          <div><b>启动时间:</b> ${formatTime(t.started_at)}</div><div><b>完成时间:</b> ${formatTime(t.completed_at)}</div>
          <div><b>尝试次数:</b> ${t.attempt_count||0} / ${t.max_attempts||3}</div><div><b>承接 Worker:</b> ${esc(t.worker_id||'—')}</div>
        </div>
        ${t.workflow_id ? `<div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:10px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center"><div><div style="font-weight:600;color:#1e40af">关联 Workflow</div><div style="font-size:12px;color:#3b82f6"><code>${esc(t.workflow_id)}</code></div></div><button class="btn primary" onclick="closeModal();inspectWorkflow('${esc(t.workflow_id)}')">直达画布</button></div>` : ''}
        ${t.error ? `<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:10px;margin-bottom:12px;color:#991b1b"><div style="font-weight:600">异常/失败原因</div><div style="font-size:12px;white-space:pre-wrap;margin-top:4px">${esc(t.error)}</div></div>` : ''}
        <div style="display:flex;justify-content:flex-end"><button class="btn" onclick="closeModal()">关闭</button></div>
      </div>`);
  }catch(e){ toast('读取任务失败: ' + e.message, true); }
}

function showConfirmScopeModal(taskId){
  const projects = (state.overview && state.overview.projects) || [];
  const projOptions = projects.map(p => `<option value="${esc(p.project_id)}" data-ws="${esc(p.workspace_id||'')}">${esc(p.project_name||p.project_id)} (${esc(p.workspace_id||'无空间')})</option>`).join('');
  openModal('确认任务归属', `
    <div class="form">
      <div style="font-size:13px;color:var(--text-secondary,#4b5563);margin-bottom:12px">请确认该任务归属的项目和工作区。确认后任务将进入就绪排队队列。</div>
      <label for="confirmScopeProject">选择归属项目<span class="danger-text">*</span></label>
      <select id="confirmScopeProject" onchange="onConfirmScopeProjectChange()" style="margin-bottom:10px"><option value="">-- 选择项目 --</option>${projOptions}</select>
      <label for="confirmScopeWorkspace">对应工作区 (自动联动)<span class="danger-text">*</span></label>
      <input id="confirmScopeWorkspace" disabled style="margin-bottom:14px">
      <div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="submitConfirmScope('${esc(taskId)}')">确认归属并排队</button></div>
    </div>`);
}

function onConfirmScopeProjectChange(){
  const sel = document.getElementById('confirmScopeProject');
  const wsInput = document.getElementById('confirmScopeWorkspace');
  if(!sel || !wsInput) return;
  const opt = sel.selectedOptions[0];
  wsInput.value = opt ? opt.dataset.ws || '' : '';
}

async function submitConfirmScope(taskId){
  const pid = (document.getElementById('confirmScopeProject')?.value || '').trim();
  const wid = (document.getElementById('confirmScopeWorkspace')?.value || '').trim();
  if(!pid || !wid) return toast('请选择有效的项目和工作区', true);
  try{
    toast('正在确认任务归属…');
    await api('/api/inbox/task/confirm', {method:'POST', body:JSON.stringify({task_id:taskId, project_id:pid, workspace_id:wid})});
    closeModal();
    toast('任务归属已确认，已进入排队队列');
    await loadInboxData();
  }catch(e){ toast('确认失败: ' + e.message, true); }
}

function showChangePriorityModal(taskId, currentPrio){
  openModal('调整任务优先级', `
    <div class="form">
      <label for="changePrioSelect">设置新优先级</label>
      <select id="changePrioSelect" style="margin-bottom:14px">
        <option value="URGENT"${currentPrio==='URGENT'?' selected':''}>紧急 (URGENT - 下次空闲首选)</option>
        <option value="HIGH"${currentPrio==='HIGH'?' selected':''}>高优 (HIGH)</option>
        <option value="NORMAL"${currentPrio==='NORMAL'?' selected':''}>常规 (NORMAL)</option>
        <option value="LOW"${currentPrio==='LOW'?' selected':''}>低优 (LOW)</option>
      </select>
      <div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button class="btn primary" onclick="submitChangePriority('${esc(taskId)}')">保存优先级</button></div>
    </div>`);
}

async function submitChangePriority(taskId){
  const prio = (document.getElementById('changePrioSelect')?.value || 'NORMAL').trim();
  try{
    toast('正在更新优先级…');
    await api('/api/inbox/task/priority', {method:'POST', body:JSON.stringify({task_id:taskId, priority:prio})});
    closeModal();
    toast('优先级已更新为 ' + priorityLabel(prio));
    await loadInboxData();
  }catch(e){ toast('更新失败: ' + e.message, true); }
}

async function moveTaskOrder(taskId, direction){
  try{
    await api('/api/inbox/task/reorder', {method:'POST', body:JSON.stringify({task_id:taskId, direction})});
    await loadInboxData();
  }catch(e){ toast('调整顺序失败: ' + e.message, true); }
}

function cancelInboxTask(taskId){
  showConfirmModal({
    title: '取消任务',
    message: '确定要取消该任务吗？取消后任务将移出执行队列。',
    confirmText: '确认取消',
    danger: true,
    onConfirm: async () => {
      try{
        toast('正在取消任务…');
        await api('/api/inbox/task/cancel', {method:'POST', body:JSON.stringify({task_id:taskId, reason:'用户在收件箱取消'})});
        toast('任务已取消');
        await loadInboxData();
      }catch(e){ toast('取消失败: ' + e.message, true); }
    }
  });
}

async function retryInboxTask(taskId){
  try{
    toast('正在重新提交排队…');
    await api('/api/inbox/task/retry', {method:'POST', body:JSON.stringify({task_id:taskId})});
    toast('任务已重新排队');
    await loadInboxData();
  }catch(e){ toast('重试失败: ' + e.message, true); }
}

function inspectWorkflow(wid){
  if(!wid) return;
  stopInboxTimer();
  if(typeof(openWorkflowTab)==='function'){
    openWorkflowTab(wid);
  } else {
    state.workflowId = wid;
    if(typeof(showShellView)==='function') showShellView('workbench');
    if(typeof(loadWorkflow)==='function') loadWorkflow(wid);
  }
}

function stopInboxTimer(){
  if(inboxState.autoRefreshTimer){
    clearInterval(inboxState.autoRefreshTimer);
    inboxState.autoRefreshTimer = null;
  }
}

function showInbox(){
  state.shellView = 'inbox';
  state.opsMode = false;
  state.dashMode = false;
  if(typeof stopDashTimer === 'function') stopDashTimer();
  setWorkspaceMode('inbox');
  paintCrumb();
  saveViewState();
  syncOpsUi();

  if(!state.openWorkflowTabIds) state.openWorkflowTabIds = [];
  if(!state.openWorkflowTabIds.includes('__inbox__')) state.openWorkflowTabIds.push('__inbox__');
  state.workflowId = '__inbox__';
  if(typeof renderWorkflowTabs === 'function') renderWorkflowTabs();
  loadInboxData(false, true);
  stopInboxTimer();
  inboxState.autoRefreshTimer = setInterval(() => {
    if(state.shellView === 'inbox' && !document.hidden) loadInboxData();
  }, 5000);
}

return {
  showInbox, stopInboxTimer, inspectWorkflow, loadInboxData, loadMoreInboxTasks, showNewInboxTaskModal, showInboxTaskDetail,
  showConfirmScopeModal, changeTaskPriority: showChangePriorityModal, moveTaskOrder, cancelInboxTask,
  retryInboxTask, setInboxStatusFilter, setInboxProjectFilter, setInboxPriorityFilter,
  triggerInboxSweep, showChangePriorityModal, submitChangePriority,
  submitNewInboxTask, submitConfirmScope, onInboxProjectSelectChange,
  onInboxInstructionBlur, onConfirmScopeProjectChange
};

})({
  state, api, esc, badge, formatElapsed,
  paintCrumb, setWorkspaceMode,
  openModal, closeModal, toast, showConfirmModal
});
