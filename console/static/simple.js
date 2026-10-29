'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const query = new URLSearchParams(location.search);
const model = {projects:[], projectId:query.get('project_id') || '', workflowId:query.get('workflow_id') || '', templates:[], sequence:0, templateSequence:0, job:null, templateSelections:new Map()};
const labels = {pending:'待开始',running:'执行中',working:'执行中',dispatched:'已派发',blocked:'受阻，等待处理',failed:'执行失败',paused:'已暂停',completed:'执行完成，待收尾',delivered:'已交付',cleaned:'已完成',integrated:'已集成',committed:'已提交',superseded:'已取代',rework:'返工中',unknown:'状态未知',ready:'已就绪',waiting:'等待中'};
const label = status => labels[status] || status || '状态未知';
/* workflow-status:start */
const workflowEsc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function workflowKey(w){
  w=w||{};
  if(w.status==='running'&&w.startup_ready===false)return 'startup_pending';
  if(w.status==='completed'&&w.outcome==='startup_failed')return 'startup_failed';
  if(w.status==='completed'&&w.outcome==='abandoned')return 'abandoned';
  return String(w.status||'unknown');
}
function workflowLabel(w){const key=workflowKey(w);return ({startup_pending:'启动中（尚未就绪）',startup_failed:'启动失败',abandoned:'已放弃'})[key]||label(key);}
function workflowHint(w){
  const key=workflowKey(w); if(key!=='startup_pending'&&key!=='startup_failed')return '';
  const detail=[w.failed_stage&&('阶段：'+w.failed_stage),w.startup_failure_reason&&('原因：'+w.startup_failure_reason)].filter(Boolean).join('；');
  return workflowEsc([detail,'人工清理：herdr-task close-workflow '+(w.workflow_id||'')+' --abandon'].filter(Boolean).join('。'));
}
/* workflow-status:end */
async function api(url, body) {
  const response = await fetch(url, {headers:{'Content-Type':'application/json','X-HAFlow-Console-Request':'1'}, ...(body ? {method:'POST',body:JSON.stringify(body)} : {}), signal:AbortSignal.timeout(url==='/api/project/create'?150000:20000)});
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || `接口失败 (${response.status})`);
  return payload.data;
}
function notice(message='') { $('notice').textContent=message; $('notice').hidden=!message; }
function formError(form, error, focus=true) {
  const target=form.querySelector('.formError');
  target.textContent=error?.message || error || '';
  if(target.textContent && focus) target.focus();
}
function contextUrl(path) {
  const q=new URLSearchParams();
  if(model.projectId) q.set('project_id',model.projectId);
  if(model.workflowId) q.set('workflow_id',model.workflowId);
  return path + ([...q].length ? '?'+q : '');
}
function saveSelection() {
  history.replaceState(null,'',contextUrl('/simple'));
  $('professional').href=contextUrl('/');
}
function options(select, entries, empty) {
  select.replaceChildren();
  if(!entries.length) select.add(new Option(empty,''));
  for(const [value,text] of entries) select.add(new Option(text,value));
}
function currentProject() { return model.projects.find(p=>p.project_id===model.projectId); }
function businessTemplates(templates) {
  return templates.filter(t=>!['eng','sys'].includes(t.category));
}
function updateSteps(step=model.projectId?2:1) {
  for(let n=1;n<=3;n++) $('step'+n).dataset.active=String(n===step);
  $('newRequirement').disabled=!currentProject();
  $('showResults').disabled=!model.workflowId;
}
function renderProjects() {
  $('projectList').replaceChildren();
  for(const project of model.projects) {
    const button=document.createElement('button');
    button.className='projectItem'+(project.project_id===model.projectId?' selected':'');
    button.textContent=project.project_name || project.project_id;
    button.ariaCurrent=project.project_id===model.projectId?'page':null;
    button.onclick=()=>{ $('project').value=project.project_id; notice(); return selectProject(); };
    $('projectList').append(button);
  }
  if(!model.projects.length) $('projectList').textContent='从第 1 步创建你的第一个项目。';
  $('workspaceName').textContent=currentProject()?.project_name || currentProject()?.project_id || '从一个项目开始';
  $('workspaceSubtitle').textContent=model.projectId?'从新建需求开始，把资料变成你需要的成果。':'给事情起个名字，说清需求，拿到成果。';
  $('projectHint').textContent=model.projectId?'已选择项目':'';
  updateSteps();
}
function renderWorkflows(workflows) {
  $('requestList').replaceChildren();
  $('workflowCount').textContent=workflows.length?'共 '+workflows.length+' 项':'';
  for(const workflow of workflows) {
    const row=document.createElement('article'); row.className='requestRow';
    const name=document.createElement('button'); name.className='requestName';
    name.textContent=workflow.title || workflow.requirement_subject || workflow.workflow_id;
    name.onclick=()=>openWorkflow(workflow.workflow_id);
    const status=document.createElement('span'); status.className='requestStatus';status.textContent=workflowLabel(workflow);status.dataset.status=workflow.status || 'unknown';
    const button=document.createElement('button');button.className='requestAction';
    const finished=['completed','delivered','cleaned'].includes(workflow.status);
    button.textContent=finished?'查看成果':'查看进度';
    button.onclick=()=>openWorkflow(workflow.workflow_id,finished);
    row.append(name,status,button);$('requestList').append(row);
  }
  updateSteps();
}
function showResults() {
  $('myRequests').hidden=true;$('results').hidden=false;
  updateSteps(3);$('results').focus();
}
async function openWorkflow(wid,results=false) {
  $('workflow').value=wid;
  await loadWorkflow();
  if(model.workflowId!==wid || $('execution').hidden) return;
  if(results) showResults();
  else $('progressDialog').showModal();
}
async function refreshProjects() {
  try {
    notice();
    model.projects=await api('/api/projects');
    options($('project'),model.projects.map(p=>[p.project_id,p.project_name || p.project_id]),'暂无项目');
    if(model.projectId && !currentProject()) { notice('原项目已不可用，请重新选择。'); model.projectId=''; model.workflowId=''; }
    // A workflow-only deep link resolves ownership through the existing workflow API.
    if(!model.projectId && model.workflowId) {
      const d=await api('/api/workflow?id='+encodeURIComponent(model.workflowId)+'&lightweight=1');
      model.projectId=d.project?.project_id || d.workflow.project_id;
      if(!currentProject()) throw new Error('该需求所属项目不可用');
    }
    model.projectId=model.projectId || model.projects[0]?.project_id || '';
    $('project').value=model.projectId;
    $('projectEmpty').hidden=!!model.projects.length;
    await selectProject(false);
  } catch(e) { notice('项目加载失败：'+e.message+'。可点击刷新状态重试。'); }
}
async function selectProject(clear=true) {
  const sequence=++model.sequence;
  model.projectId=$('project').value;
  ++model.templateSequence;
  $('start').disabled=true;
  for(const id of ['category','template','templateSearch']) $(id).value='';
  $('templateInfo').textContent=''; $('contextInputs').replaceChildren();
  $('attentionList').textContent='请先选择一个项目。'; $('attentionCount').textContent=''; $('decisions').textContent='选择需求后，在这里处理待决事项。';
  if(clear) model.workflowId='';
  $('progressDialog').close();$('results').hidden=true;$('myRequests').hidden=false;
  $('requestList').replaceChildren();$('workflowCount').textContent='';$('resultHint').textContent='';
  $('attentionSection').hidden=true;renderProjects();
  $('execution').hidden=true;
  $('resultList').innerHTML='<p class="empty">正在读取成果…</p>';
  $('resultCount').textContent='';
  saveSelection();
  if(!model.projectId) { options($('workflow'),[],'先选择项目'); $('requirementEmpty').hidden=true; $('resultList').innerHTML='<p class="empty">选择一个项目和需求后，在这里查看成果。</p>'; return; }
  try {
    const workflows=await api('/api/workflows?project_id='+encodeURIComponent(model.projectId));
    if(sequence!==model.sequence) return;
    if(model.workflowId&&!workflows.some(w=>w.workflow_id===model.workflowId)) { notice('原需求不属于当前项目或已不可用。'); model.workflowId=''; }
    model.workflowId=model.workflowId || workflows[0]?.workflow_id || '';
    options($('workflow'),workflows.map(w=>[w.workflow_id,w.title || w.requirement_subject || w.workflow_id]),'暂无需求');
    $('workflow').value=model.workflowId;
    $('requirementEmpty').hidden=!!workflows.length;
    renderWorkflows(workflows);
    await Promise.all([loadWorkflow(),loadAttention(workflows,model.projectId)]);
  } catch(e) { if(sequence===model.sequence) { options($('workflow'),[],'需求加载失败'); $('resultList').textContent='成果暂不可读取'; $('requestList').textContent='需求暂不可读取，请刷新重试。'; notice('需求加载失败：'+e.message); } }
}
async function loadWorkflow() {
  const wid=model.workflowId=$('workflow').value;
  const sequence=++model.sequence;
  $('decisions').textContent=wid?'正在读取当前需求的待决事项…':'选择需求后，在这里处理待决事项。';
  saveSelection();
  $('execution').hidden=true;
  $('resultCount').textContent='';$('resultHint').textContent='';updateSteps();
  if(!wid) { $('resultList').innerHTML='<p class="empty">暂无成果。新建需求后可在这里跟踪交付。</p>'; return; }
  $('resultList').innerHTML='<p class="empty">正在读取成果…</p>';
  try {
    const d=await api('/api/workflow?id='+encodeURIComponent(wid)+'&lightweight=1');
    if(sequence!==model.sequence) return;
    if(d.workflow.project_id!==model.projectId) throw new Error('工作流项目归属不匹配');
    if(wid!==$('execution').dataset.workflowId) for(const detail of $('execution').querySelectorAll('details')) detail.open=false;
    $('execution').dataset.workflowId=wid;
    $('execution').hidden=false;
    $('requirementTitle').textContent=d.workflow.title || d.workflow.requirement_subject || wid;
    $('requirementText').textContent=d.workflow.requirement || '';
    $('resultSubject').textContent=$('requirementTitle').textContent;
    $('workflowTemplate').textContent='协作模板：'+(d.workflow.template || d.workflow.template_id || d.workflow.workflow_template || d.workflow.config?.workflow_template || '未提供');
    $('status').textContent=workflowLabel(d.workflow);
    $('status').dataset.status=d.workflow.status || 'unknown';
    $('taskCount').textContent=(d.tasks || []).length+' 项记录';
    $('stages').innerHTML=(d.stages || []).map(s=>`<li>${esc(s.label || s.key)}<small>${esc(label(s.status))}</small></li>`).join('');
    $('tasks').innerHTML=(d.tasks || []).map(t=>`<div class="task"><div>${esc(t.node_label || t.goal || t.task_id)}<small>${esc(t.failure_reason || t.blocked_reason || '')}</small></div><span>${esc(label(t.status))}</span></div>`).join('') || '<p class="empty">需求已登记，等待调度派发。</p>';
    const workflowNotice=workflowHint(d.workflow);
    if(workflowNotice) notice(workflowNotice);
    else if(['failed','blocked'].includes(d.workflow.status)) notice('当前需求'+label(d.workflow.status)+'。可切换到流程工作台查看原因并处理。');
    await Promise.all([loadDecisions(wid,sequence),loadResults(wid,sequence)]);
  } catch(e) { if(sequence===model.sequence) { notice('执行状态加载失败：'+e.message); $('decisions').textContent='当前需求的待决事项暂不可读取，请刷新重试。'; $('resultList').textContent='成果暂不可读取'; } }
}
async function loadDecisions(wid, sequence) {
  $('decisions').replaceChildren();
  try {
    const d=await api('/api/workflow/decisions?workflow_id='+encodeURIComponent(wid));
    if(sequence!==model.sequence) return;
    if(!(d.decisions || []).length) $('decisions').textContent='当前需求暂无需要你处理的事项。';
    for(const decision of d.decisions || []) {
      const form=document.createElement('form'); form.className='decision';
      form.innerHTML=`<h3>需要你的决定</h3><p>${esc(decision.question)}</p><label>你的决定<textarea required rows="2" aria-label="你的决定"></textarea></label><p role="alert" class="formError" tabindex="-1"></p><button class="primary">提交决定</button>`;
      form.onsubmit=async event=>{
        event.preventDefault(); const button=form.querySelector('button'); button.disabled=true;
        try {
          await api('/api/workflow/decision',{workflow_id:wid,decision_id:decision.decision_id,decision:form.querySelector('textarea').value.trim()});
          if(wid===model.workflowId) { notice('决定已记录，工作流将按既有机制继续处理。'); await selectProject(false); }
        } catch(e) { formError(form,e); } finally { button.disabled=false; }
      };
      $('decisions').append(form);
    }
  } catch(e) { if(sequence===model.sequence) $('decisions').textContent='人工决策读取失败：'+e.message; }
}
function deliverableUrl(wid,item,download=false) {
  return '/api/workflow/deliverable?'+new URLSearchParams({workflow_id:wid,task_id:item.task_id,id:item.id,...(download?{download:'1'}:{})});
}
async function loadResults(wid,sequence) {
  try {
    const d=await api('/api/workflow/deliverables?workflow_id='+encodeURIComponent(wid));
    if(sequence!==model.sequence) return;
    $('resultList').replaceChildren();
    const items=d.items || [];
    const usable=items.filter(item=>(item.readable && (item.actions || []).some(action=>action==='view' || action==='download')) || ((item.actions || []).includes('open') && /^https?:\/\//i.test(item.url || '')));
    const featured=new Set(usable.slice(0,3));
    $('resultHint').textContent=usable.length?usable.length+' 项成果可查看':'';
    $('resultCount').textContent=usable.length+' 项可用成果 · '+items.length+' 项记录';
    const history=document.createElement('details'); history.className='resultHistory';
    const summary=document.createElement('summary'); summary.textContent='查看全部记录（其余 '+(items.length-featured.size)+' 项）'; history.append(summary);
    if(d.error) { const p=document.createElement('p'); p.textContent='成果读取失败：'+d.error; $('resultList').append(p); }
    if(d.truncated) { const p=document.createElement('p'); p.textContent=d.notice || '成果列表可能未完整'; $('resultList').append(p); }
    if(!(d.items || []).length && !d.error) $('resultList').innerHTML='<p class="empty">暂无成果。执行中的需求将在产生文件后展示。</p>';
    if(items.length && !usable.length) { const p=document.createElement('p'); p.className='empty'; p.textContent='暂无可直接查看的成果，执行与历史记录可在下方展开。'; $('resultList').append(p); }
    for(const item of items) {
      const card=document.createElement('article'); card.className='result';
      card.innerHTML=`<strong>${esc(item.name || item.title || item.kind)}</strong><p class="subtle">${esc(item.status_label || item.status)} · ${esc(item.reason || item.rel_path || '')}</p><div class="tools"></div>`;
      const tools=card.querySelector('.tools');
      if(item.readable && (item.actions || []).includes('view')) {
        const view=document.createElement('button'); view.textContent='查看'; view.onclick=async()=>{
          $('previewTitle').textContent=item.name || '成果预览'; $('preview').textContent='正在读取…'; $('previewDialog').showModal();
          try { const preview=await api(deliverableUrl(wid,item)); $('preview').textContent=(preview.text || preview.reason || '无预览内容')+(preview.truncated?'\n（预览已截断，请下载完整文件）':''); }
          catch(e) { $('preview').textContent='预览失败：'+e.message; }
        }; tools.append(view);
      }
      if(item.readable && (item.actions || []).includes('download')) {
        const download=document.createElement('a'); download.textContent='下载'; download.href=deliverableUrl(wid,item,true); download.download=item.name;
        download.onclick=async event=>{
          event.preventDefault();
          try {
            const response=await fetch(deliverableUrl(wid,item,true),{signal:AbortSignal.timeout(20000)});
            if(!response.ok) { const error=await response.json(); throw new Error(error.error || '下载失败'); }
            const url=URL.createObjectURL(await response.blob());
            const link=document.createElement('a'); link.href=url; link.download=item.name; link.click();
            setTimeout(()=>URL.revokeObjectURL(url),1000);
          } catch(e) { notice('下载失败：'+e.message); }
        }; tools.append(download);
      }
      if((item.actions || []).includes('open') && /^https?:\/\//i.test(item.url || '')) {
        const link=document.createElement('a'); link.textContent='打开成果链接'; link.href=item.url; link.target='_blank'; link.rel='noopener noreferrer'; tools.append(link);
      }
      (featured.has(item)?$('resultList'):history).append(card);
    }
    if(items.length>featured.size) $('resultList').append(history);
  } catch(e) { if(sequence===model.sequence) $('resultList').textContent='成果加载失败：'+e.message+'。请刷新重试。'; }
}
async function loadAttention(workflows,projectId) {
  const sequence=(model.attentionSequence || 0)+1; model.attentionSequence=sequence;
  $('attentionSection').hidden=true;
  $('attentionList').textContent='正在读取各需求的待处理事项…'; $('attentionCount').textContent='读取中';
  const items=[]; let failed=false;
  // Sequential requests keep this aggregation bounded without another state source.
  for(const workflow of workflows) {
    if(projectId!==model.projectId || sequence!==model.attentionSequence) return;
    try {
      const data=await api('/api/workflow/decisions?workflow_id='+encodeURIComponent(workflow.workflow_id));
      if(projectId!==model.projectId || sequence!==model.attentionSequence) return;
      for(const decision of data.decisions || []) items.push({workflow,decision});
    } catch { failed=true; }
  }
  if(projectId!==model.projectId || sequence!==model.attentionSequence) return;
  $('attentionList').replaceChildren();
  $('attentionSection').hidden=!items.length&&!failed;
  $('attentionCount').textContent=items.length+' 项'+(failed?' · 部分读取失败':'');
  for(const {workflow,decision} of items) {
    const button=document.createElement('button'); button.className='attentionItem';
    button.textContent=(workflow.title || workflow.requirement_subject || workflow.workflow_id)+'：'+decision.question;
    button.onclick=()=>openWorkflow(workflow.workflow_id);
    $('attentionList').append(button);
  }
  if(!items.length) $('attentionList').textContent=failed?'部分需求待处理事项读取失败，请刷新重试':'当前项目暂无需要你处理的事项。';
  else if(failed) { const p=document.createElement('p'); p.textContent='部分需求读取失败，列表可能不完整，请刷新重试。'; $('attentionList').append(p); }
}
function rememberTemplateSelection() {
  model.templateSelections.set(model.projectId,{category:$('category').value,template:$('template').value,search:$('templateSearch').value});
}
async function openRequirement() {
  if(!currentProject()) { notice('请先创建或选择一个项目。'); $('newProject').focus(); return; }
  const projectId=model.projectId, sequence=++model.templateSequence;
  $('requirementDialog').showModal(); $('template').focus();
  $('requirementProject').textContent='所属项目：'+(currentProject().project_name || projectId);
  formError($('requirementForm'),'');
  if(model.job) { pollJob(); return; }
  $('start').disabled=true;
  try {
    const data=await api('/api/templates');
    if(projectId!==model.projectId || sequence!==model.templateSequence) return;
    model.templates=businessTemplates(data.templates || []);
    const categories=new Map(model.templates.map(t=>[t.category,t.category_name || t.category]));
    const saved=model.templateSelections.get(projectId);
    options($('category'),[['','全部分类'],...categories], '暂无分类');
    $('category').value=saved?.category || '';
    $('templateSearch').value=saved?.search || '';
    populateTemplates(saved?.template || '');
    if(!saved) {
      const mode=currentProject()?.execution?.mode || 'context';
      const preferred=model.templates.find(t=>t.execution_mode===mode);
      $('template').value=preferred?.id || '';
    }
    await loadTemplate();
  } catch(e) { if(projectId===model.projectId && sequence===model.templateSequence) formError($('requirementForm'),'模板加载失败：'+e.message); }
}
function populateTemplates(selected=$('template').value) {
  const search=$('templateSearch').value.trim().toLocaleLowerCase();
  const templates=model.templates.filter(t=>(!$('category').value || t.category===$('category').value) && (!search || [t.label,t.name,t.id,t.description].join(' ').toLocaleLowerCase().includes(search)));
  options($('template'),templates.map(t=>[t.id,t.label || t.id]),'暂无匹配模板');
  if(templates.some(t=>t.id===selected)) $('template').value=selected;
}
async function loadTemplate() {
  const sequence=++model.templateSequence, projectId=model.projectId;
  rememberTemplateSelection();
  formError($('requirementForm'),'');
  $('start').disabled=true; $('templateHint').textContent='正在读取模板…'; $('templateInfo').textContent=''; $('contextInputs').replaceChildren();
  const tid=$('template').value;
  if(!tid) { $('templateHint').textContent='此分类没有可用模板。'; $('templateInfo').textContent='此分类没有可用模板。'; formError($('requirementForm'),'没有匹配模板，请调整分类或搜索条件。',false); return; }
  try {
    const d=await api('/api/template?id='+encodeURIComponent(tid));
    if(sequence!==model.templateSequence || projectId!==model.projectId) return;
    $('templateHint').textContent=d.template.description || '选择模板后，说明你想完成的事情。';
    $('templateInfo').textContent=`${d.nodes.map(n=>n.label).join(' → ')}\n执行方式：${d.execution_mode==='context'?'业务上下文，可使用普通目录':'Git 仓库，目录需已初始化 Git'}`;
    for(const kind of ['required','optional']) for(const ctx of d.context?.[kind] || []) {
      const wrapper=document.createElement('label'); wrapper.textContent=(ctx.label || ctx.id)+(kind==='required'?'（必填）':'（选填）');
      const input=document.createElement('input'); input.dataset.context=ctx.id; input.required=kind==='required'; input.placeholder='已有资料文件或目录的绝对路径'; wrapper.append(input); $('contextInputs').append(wrapper);
    }
    $('start').disabled=!!model.job;
  } catch(e) { if(sequence===model.templateSequence) { $('templateInfo').textContent='模板详情读取失败：'+e.message; formError($('requirementForm'),'模板详情读取失败，请重新选择模板或稍后重试。',false); } }
}
function draftSave() {
  try { localStorage.setItem('haflow.simple.draft',JSON.stringify({title:$('title').value,requirement:$('requirement').value})); } catch { /* Storage may be disabled. */ }
}
try { const draft=JSON.parse(localStorage.getItem('haflow.simple.draft') || '{}'); $('title').value=draft.title || ''; $('requirement').value=draft.requirement || ''; model.job=JSON.parse(sessionStorage.getItem('haflow.simple.job') || 'null'); } catch { /* URL and server remain authoritative. */ }
$('title').oninput=draftSave; $('requirement').oninput=draftSave;
$('projectForm').onsubmit=async event=>{
  event.preventDefault(); const button=event.submitter || event.target.querySelector('button[type=submit]'); if(button) button.disabled=true; formError(event.target,'');
  try {
    const project=await api('/api/project/create',{project_name:$('projectName').value.trim(),project_root:$('projectRoot').value.trim(),template:$('projectType').value});
    model.projectId=project.project_id; model.workflowId=''; $('projectDialog').close(); await refreshProjects(); notice('项目已关联。');
  } catch(e) { formError(event.target,e); } finally { if(button) button.disabled=false; }
};
$('requirementForm').onsubmit=async event=>{
  event.preventDefault(); if(model.job) return;
  const project=currentProject(); if(!project) return formError(event.target,'项目已不可用，请重新选择');
  $('start').disabled=true; formError(event.target,'');
  try {
    const context={}; for(const input of $('contextInputs').querySelectorAll('input')) if(input.value.trim()) context[input.dataset.context]=input.value.trim();
    const job=await api('/api/run',{project_root:project.project_root,requirement:$('requirement').value.trim(),title:$('title').value.trim() || $('requirement').value.trim().slice(0,80),template:$('template').value,context,agent:'auto'});
    model.job={id:job.job_id,projectId:project.project_id};
    try { sessionStorage.setItem('haflow.simple.job',JSON.stringify(model.job)); } catch { }
    pollJob();
  } catch(e) { formError(event.target,e); $('start').disabled=false; }
};
let polling=false;
async function pollJob() {
  if(polling || !model.job) return;
  polling=true; $('start').disabled=true; $('jobStatus').textContent='正在启动协作，预检与派发可能需要数分钟…';
  try {
    const job=await api('/api/run/status?id='+encodeURIComponent(model.job.id));
    if(job.status==='running') { setTimeout(pollJob,1500); return; }
    model.job=null; try { sessionStorage.removeItem('haflow.simple.job'); } catch { }
    if(job.status==='failed') throw new Error(job.error || '启动失败');
    const match=String(job.output || '').match(/^WORKFLOW_ID=(\S+)$/m);
    if(!match) throw new Error('启动响应未返回工作流 ID，请在流程工作台核对后再试');
    const started=await api('/api/workflow?id='+encodeURIComponent(match[1])+'&lightweight=1');
    if(!started.workflow?.project_id) throw new Error('工作流缺少所属项目，请在流程工作台核对');
    model.projectId=started.workflow.project_id; model.workflowId=match[1];
    $('requirementDialog').close(); $('jobStatus').textContent='';
    await refreshProjects();
    if(!$('notice').hidden || model.workflowId!==match[1]) throw new Error('新需求已登记，但读取执行状态失败，请刷新核对');
    notice('需求已启动，正在显示实际执行状态。');
  } catch(e) {
    if(/不存在或已过期/.test(e.message)) { model.job=null; try { sessionStorage.removeItem('haflow.simple.job'); } catch { } }
    notice('启动或状态读取失败：'+e.message);
    formError($('requirementForm'),e); $('jobStatus').textContent=model.job?'状态读取失败，稍后会继续查询。':'启动失败，请核对后重试。'; if(model.job) setTimeout(pollJob,5000); }
  finally { polling=false; $('start').disabled=!!model.job; }
}
$('newProject').onclick=async()=>{
  formError($('projectForm'),''); $('projectDialog').showModal(); $('projectName').focus();
  const button=$('projectForm').querySelector('button[type=submit]'); button.disabled=true;
  options($('projectType'),[],'正在读取模板…');
  try {
    const data=await api('/api/templates');
    options($('projectType'),businessTemplates(data.templates || []).map(t=>[t.id,(t.execution_mode==='context'?'业务目录':'Git 仓库')+' · '+(t.label || t.id)]),'没有可用模板');
    const preferred=businessTemplates(data.templates || []).find(t=>t.execution_mode==='context');
    if(preferred) $('projectType').value=preferred.id;
    button.disabled=!$('projectType').value;
  } catch(e) { formError($('projectForm'),'项目模板读取失败：'+e.message); }
};
$('newRequirement').onclick=openRequirement;
$('showResults').onclick=showResults;
$('backToRequests').onclick=()=>{$('results').hidden=true;$('myRequests').hidden=false;updateSteps();};
$('project').onchange=()=>{notice(); selectProject();};
$('category').onchange=()=>{populateTemplates();loadTemplate();};
$('template').onchange=loadTemplate;
$('templateSearch').oninput=()=>{populateTemplates();loadTemplate();};
// Search inputs otherwise consume the first Escape to clear their value.
$('templateSearch').onkeydown=event=>{if(event.key==='Escape'){event.preventDefault();$('requirementDialog').close();}};
$('refresh').onclick=refreshProjects;
for(const button of document.querySelectorAll('[data-close]')) button.onclick=()=>button.closest('dialog').close();
function refreshView() {
  if(document.hidden || !model.workflowId || document.querySelector('#projectDialog[open], #requirementDialog[open], #previewDialog[open]') || document.activeElement.closest('.decision')) return;
  if([...document.querySelectorAll('.decision textarea')].some(input=>input.value.trim())) return;
  if($('progressDialog').open) {
    if(!document.querySelector('#progressDialog details[open]')) return loadWorkflow();
  } else if(!$('results').hidden) {
    if(!document.querySelector('#results details[open]')) return loadResults(model.workflowId,model.sequence);
  } else if(!document.querySelector('#myRequests details[open]')) return selectProject(false);
}
setInterval(refreshView,15000);
refreshProjects();
if(model.job) pollJob();
