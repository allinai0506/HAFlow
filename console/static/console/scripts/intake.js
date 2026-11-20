/* Active Intake 控制台外部任务控制器与视图 */
var {
  toggleIntakeAuth,
  showIntakeDetail,
  fetchIntakeActiveItems,
  renderIntakeCards,
  renderIntakePagination,
  renderIntakeEmpty,
  getIntakeAuthButtonHtml,
  bindIntakeEvents,
  hasIntakeToken,
  getIntakeItemsCount,
  syncIntakeDetail
} = (function({
  state, api, esc, openModal, closeModal, toast
}){

const INBOX_TOKEN_STORAGE_KEY = 'haflow_intake_token';

function getStoredToken(){
  try{
    return sessionStorage.getItem(INBOX_TOKEN_STORAGE_KEY) || '';
  }catch(_){ return ''; }
}

function storeToken(token){
  intakeToken = token || '';
  try{
    if(intakeToken){
      sessionStorage.setItem(INBOX_TOKEN_STORAGE_KEY, intakeToken);
    }else{
      sessionStorage.removeItem(INBOX_TOKEN_STORAGE_KEY);
    }
  }catch(_){}
}

let intakeToken = getStoredToken();
let intakeItems = [];
let intakeLoad = 0;
let intakeOffset = 0;
const intakePageSize = 50;
const intakeLabels = {
  DRAFT:'外部草稿', PENDING_ASSIGNMENT:'待分配', CONFIRMED:'已确认',
  PENDING:'排队待执行', RUNNING:'正在执行', COMPLETED:'已完成', FAILED:'执行失败',
  IGNORED:'已忽略', SOURCE_CLOSED:'来源已关闭', SOURCE_CONFLICT:'来源信息冲突',
};

function formatTime(ts){
  if(!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', {hour12:false, month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
}

function priorityLabel(p){
  const map = {URGENT:'紧急', HIGH:'高优', NORMAL:'常规', LOW:'低优'};
  return map[(p||'NORMAL').toUpperCase()] || p;
}

function intakeRequest(suffix = '', options = {}, credential = intakeToken){
  return api('/api/inbox/intake' + suffix, {
    ...options, headers:{'X-HAFlow-Intake-Inbox-Token':credential},
  });
}

function hasIntakeToken(){ return Boolean(intakeToken); }
function getIntakeItemsCount(){ return intakeItems.length; }

async function fetchIntakeActiveItems(qParts, view, isReset){
  if(isReset) intakeOffset = 0;
  const intakeRead = ++intakeLoad;
  const intakeQuery = new URLSearchParams(qParts.join('&'));
  intakeQuery.delete('status');
  intakeQuery.set('view', view);
  intakeQuery.set('offset', intakeOffset);
  intakeQuery.set('limit', intakePageSize);
  const showIntake = ['ALL','MINE','DRAFT','PENDING_ASSIGNMENT'].includes(view);
  let activeItems = null;
  if(intakeToken && showIntake){
    activeItems = await intakeRequest('?' + intakeQuery).catch(error => ({error:error.message}));
  }
  if(intakeRead !== intakeLoad) return;
  if(activeItems?.error === 'intake_inbox_authentication_required') storeToken('');
  intakeItems = activeItems?.items || [];
  syncIntakeDetail();
}

function syncIntakeDetail(){
  const detail = document.getElementById('intakeDetail');
  if(detail && detail.dataset && !intakeItems.some(item => item.link_id === detail.dataset.linkId)){
    closeModal();
    if(typeof detail.remove === 'function') detail.remove();
  }
}

function renderIntakeCard(item){
  const label = intakeLabels[item.status] || item.status;
  return `<article class="inbox-card">
    <div class="inbox-card-head"><div class="inbox-card-title-row">
      <span class="prio-pill prio-${esc(item.priority.toLowerCase())}">${esc(priorityLabel(item.priority))}</span>
      <span class="inbox-card-title">${esc(item.title)}</span>
    </div><span class="status-pill pill-draft">${esc(label)}</span></div>
    <div class="inbox-card-instruction">${esc(item.description)}</div>
    <div class="inbox-card-meta">
      <span>来源 ${esc(item.provider)} · ${esc(item.object_type)} / ${esc(item.external_id)}</span>
      <span>项目 ${esc(item.project_id || '未指定')}</span>
      <span>工作区 ${esc(item.workspace_id || '未指定')}</span>
      <span>${item.assignment_source === 'MANUAL' ? '手动分配' : '自动判断'}</span>
      <span>创建 ${formatTime(item.created_at / 1000)}</span>
      <span>更新 ${formatTime(item.updated_at / 1000)}</span>
    </div>
    <div class="inbox-card-foot"><span class="inbox-foot-left">${esc(item.display_name)}</span>
      <button class="btn-inbox-op" type="button" data-intake-open="${esc(item.link_id)}">查看详情</button>
    </div>
  </article>`;
}

function renderIntakeCards(existingTasks = []){
  const existingIds = new Set((existingTasks || []).map(t => t.id));
  return intakeItems
    .filter(item => !item.task_id || !existingIds.has(item.task_id))
    .map(renderIntakeCard)
    .join('');
}

function renderIntakePagination(){
  if(intakeToken && (intakeOffset || intakeItems.length === intakePageSize)){
    return `<div class="inbox-foot-actions" aria-label="外部任务分页">
      <button class="btn" type="button" data-intake-page="-1" ${intakeOffset === 0 ? 'disabled' : ''}>外部任务上一页</button>
      <button class="btn" type="button" data-intake-page="1" ${intakeItems.length < intakePageSize ? 'disabled' : ''}>外部任务下一页</button>
    </div>`;
  }
  return '';
}

function renderIntakeEmpty(view){
  if(view === 'PENDING_ASSIGNMENT'){
    return `<div class="empty">${intakeToken ? '暂无待分配任务' : '解锁外部任务后查看待分配事项'}</div>`;
  }
  return '';
}

function getIntakeAuthButtonHtml(){
  return `<button class="btn" type="button" data-intake-auth>${intakeToken ? '锁定外部任务' : '解锁外部任务'}</button>`;
}

function toggleIntakeAuth(onReload, onRender){
  if(intakeToken){
    storeToken('');
    intakeItems = [];
    closeModal();
    const detail = document.getElementById('intakeDetail');
    if(detail && typeof detail.remove === 'function') detail.remove();
    if(typeof onRender === 'function') onRender();
    if(typeof onReload === 'function') return onReload();
    return;
  }
  openModal('解锁外部任务', `
    <form class="form" id="intakeUnlock" action="/api/inbox/intake" method="post" aria-labelledby="modalTitle">
      <label for="intakeCredential">Inbox 访问凭据</label>
      <input id="intakeCredential" type="password" autocomplete="current-password" required minlength="32">
      <p role="status"></p>
      <button type="submit" class="btn primary">解锁</button>
    </form>`);
  const form = document.getElementById('intakeUnlock');
  const field = document.getElementById('intakeCredential');
  field?.focus?.();
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const submit = form.querySelector('button');
    submit.disabled = true;
    try{
      const credential = field.value;
      await intakeRequest('?limit=1', {}, credential);
      storeToken(credential);
      field.value = '';
      closeModal();
      if(typeof onReload === 'function') await onReload();
    }catch(error){
      form.querySelector('[role=status]').textContent =
        error.message === 'intake_inbox_disabled' ? '当前实例未启用外部任务。' : '无法解锁，请核对 Inbox 访问凭据。';
      field?.focus?.();
    }finally{ submit.disabled = false; }
  });
}

function showIntakeDetail(linkId, onReload){
  const item = intakeItems.find(row => row.link_id === linkId);
  if(!item || !intakeToken) return;
  const isLinked = item.disposition === 'LINKED';
  const isUnassigned = item.disposition === 'UNASSIGNED';
  const isIgnored = item.disposition === 'IGNORED';
  const isClosedOrConflict = ['SOURCE_CLOSED','SOURCE_CONFLICT'].includes(item.status);

  const editable = isLinked && !isClosedOrConflict;

  let availableActions = [];
  if(isIgnored){
    availableActions = [['reactivate','重新激活']];
  }else if(isClosedOrConflict){
    availableActions = isLinked
      ? [['unassign','撤销归属'],['ignore','忽略']]
      : [['ignore','忽略']];
  }else if(isUnassigned){
    availableActions = [['assign','分配给我'],['ignore','忽略']];
  }else if(isLinked){
    if(item.status === 'DRAFT'){
      availableActions = [['confirm','确认并排队'],['unassign','撤销归属'],['ignore','忽略']];
    }else if(item.status === 'CONFIRMED'){
      availableActions = [['dispatch','排队执行'],['unassign','撤销归属'],['ignore','忽略']];
    }else{
      availableActions = [['unassign','撤销归属'],['ignore','忽略']];
    }
  }
  const projects = (state.overview && state.overview.projects) || [];
  const options = projects.filter(p => p.workspace_id).map(p =>
    `<option value="${esc(p.project_id)}" data-workspace="${esc(p.workspace_id)}"${p.project_id===item.project_id?' selected':''}>${esc(p.project_name || p.project_id)}</option>`
  ).join('');
  const priorities = ['NORMAL','LOW','HIGH','URGENT'].map(p =>
    `<option value="${p}"${p===item.priority?' selected':''}>${priorityLabel(p)}</option>`
  ).join('');
  openModal('外部任务详情', `
    <form class="form" id="intakeDetail" data-link-id="${esc(linkId)}" action="/api/inbox/intake" method="post" aria-labelledby="modalTitle">
      <p>${esc(item.provider)} · ${esc(item.external_id)} · ${esc(intakeLabels[item.status] || item.status)}</p>
      <p>连接 ${esc(item.connection_id)}<br>事件 ${esc(item.event_id)}<br>原始时间 ${esc(item.occurred_time || '未提供')}<br>投递回执 ${esc(item.receipt_id || '未保留')}</p>
      <details><summary>来源快照</summary><p>${esc(item.source_title)}</p><p>${esc(item.source_description)}</p></details>
      ${editable ? `
        <label for="intakeTitle">标题</label><input id="intakeTitle" name="title" required maxlength="240" value="${esc(item.title)}">
        <label for="intakeDescription">描述</label><textarea id="intakeDescription" name="description" required maxlength="8000" rows="4">${esc(item.description)}</textarea>
        <label for="intakeProject">项目</label><select id="intakeProject" name="project_id"><option value="">未指定</option>${options}</select>
        <label for="intakeWorkspace">工作区</label><input id="intakeWorkspace" readonly value="${esc(item.workspace_id || '')}">
        <label for="intakePriority">优先级</label><select id="intakePriority" name="priority">${priorities}</select>
      ` : `<p>${esc(item.title)}</p><p>${esc(item.description)}</p>`}
      <p role="status"></p>
      <div class="inbox-foot-actions">${editable ? '<button type="submit" class="btn primary">保存修改</button>' : ''}
        ${availableActions.map(([action,label]) => `<button class="btn" type="button" data-action="${action}">${label}</button>`).join('')}
      </div>
    </form>`);
  const form = document.getElementById('intakeDetail');
  const project = document.getElementById('intakeProject');
  const workspace = document.getElementById('intakeWorkspace');
  const initial = editable ? JSON.stringify([...new FormData(form)]) : '';
  const apply = async (action, changes) => {
    if(action === 'confirm' && initial !== JSON.stringify([...new FormData(form)])){
      form.querySelector('[role=status]').textContent = '请先保存修改，再确认当前任务。';
      form.querySelector('button[type=submit]').focus();
      return;
    }
    const buttons = [...form.querySelectorAll('button')];
    buttons.forEach(button => {button.disabled = true;});
    try{
      await intakeRequest('', {method:'POST',body:JSON.stringify({
        action, changes, link_id:item.link_id, revision:item.revision,
      })});
      closeModal();
      if(typeof onReload === 'function') await onReload();
      if(action === 'confirm' || action === 'dispatch') toast('已确认外部任务并加入执行队列');
    }catch(error){
      const status = form.querySelector('[role=status]');
      status.textContent = '操作未保存。' + error.message;
      status.tabIndex = -1;
      status.focus();
    }finally{ buttons.forEach(button => {button.disabled = false;}); }
  };
  form.addEventListener('submit', event => {
    event.preventDefault();
    if(!editable) return;
    const data = new FormData(form);
    apply('edit', {
      title:data.get('title'), description:data.get('description'), priority:data.get('priority'),
      project_id:project.value || null, workspace_id:workspace.value || null,
    });
  });
  if(project) project.addEventListener('change', () => {
    workspace.value = project.selectedOptions[0]?.dataset.workspace || '';
  });
  form.querySelectorAll('[data-action]').forEach(button => {
    button.addEventListener('click', () => apply(button.dataset.action));
  });
  form.querySelector('input, button')?.focus?.();
}

function bindIntakeEvents(container, onReload, onRender){
  if(!container || typeof container.querySelector !== 'function') return;
  container.querySelector('[data-intake-auth]')?.addEventListener('click', () => toggleIntakeAuth(onReload, onRender));
  container.querySelectorAll('[data-intake-page]').forEach(button => {
    button.addEventListener('click', () => {
      intakeOffset += Number(button.dataset.intakePage) * intakePageSize;
      if(typeof onReload === 'function') onReload();
    });
  });
  container.querySelectorAll('[data-intake-open]').forEach(button => {
    button.addEventListener('click', () => showIntakeDetail(button.dataset.intakeOpen, onReload));
  });
}

return {
  toggleIntakeAuth,
  showIntakeDetail,
  fetchIntakeActiveItems,
  renderIntakeCards,
  renderIntakePagination,
  renderIntakeEmpty,
  getIntakeAuthButtonHtml,
  bindIntakeEvents,
  hasIntakeToken,
  getIntakeItemsCount,
  syncIntakeDetail
};

})({
  state, api, esc, openModal, closeModal, toast
});
