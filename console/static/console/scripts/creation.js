/* Project registration/adoption and asynchronous workflow creation. */
var {
  submitNewWorkflowAsync, renderAgents, renderSlots, autoFillWorkflowTitle,
  showNewProjectModal, autoFillProjectName, submitNewProject, submitAdoptSpace,
  showUnregisterProjectModal, submitUnregisterProject, showNewWorkflow, bindSlotPrompt
} = (function({
  state, showPromptModal, saveViewState,
  api, toast, esc,
  cleanStageLabel, agentStatusLabel, authHintLabel,
  populateTemplateSelect, refreshAll, loadProject,
  openModal, closeModal
}){

async function waitForWorkflowJob(jobId){
  try{
    for(let i=1;i<=620;i++){
      const job=await api('/api/run/status?id='+encodeURIComponent(jobId));
      if(job.status==='succeeded')return job;
      if(job.status==='failed')throw new Error(job.error||'工作流启动失败');
      const wait=document.getElementById('runWaitStatus');
      if(wait)wait.textContent='深度体检与启动中… '+i+'s（后端最长约 10 分钟，请勿重复创建）';
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    throw new Error('工作流启动超时，请到运维驾驶舱查看状态');
  }finally{
    const wait=document.getElementById('runWaitStatus');
    if(wait)wait.textContent='';
  }
}
async function submitNewWorkflowAsync(){
  const button=[...document.querySelectorAll('#modal button')].find(x=>x.textContent.includes('启动工作流'));
  const title=(document.getElementById('newTitle')?.value||'').trim();
  const q=document.getElementById('newRequirement')?.value.trim();
  const a=document.getElementById('newAgent')?.value;
  const t=document.getElementById('newTemplate')?.value||'software-development-v1';
  if(!q)return toast('请输入需求',true);
  if(button){button.disabled=true;button.classList.add('is-loading');button.textContent='启动中…'}
  try{
    toast('正在创建工作流…');
    const job=await api('/api/run',{method:'POST',body:JSON.stringify({project_root:state.project.project.project_root,title:title,requirement:q,agent:a,template:t})});
    const result=await waitForWorkflowJob(job.job_id);
    const m=/(?:^|[\r\n])WORKFLOW_ID=([^\s]+)/.exec(result.output||'');
    if(m){state.workflowId=m[1];saveViewState()}
    closeModal();await refreshAll();
    toast('工作流已启动：'+(m?m[1]:(result.output||'已提交')));
  }catch(e){
    if(button){button.disabled=false;button.classList.remove('is-loading');button.textContent='重试启动工作流'}
    const body=document.getElementById('modalBody');
    if(body){let error=body.querySelector('.run-error');if(!error){error=document.createElement('div');error.className='run-error danger-text';body.prepend(error)}error.textContent='启动失败：'+e.message}
    toast('工作流启动失败：'+e.message,true);
  }
}


function renderAgents(){const rs=state.project.agents||[];document.getElementById('agents').innerHTML=rs.length?rs.map(a=>`<div class="agent-row"><span><i class="dot ${esc(a.status)}"></i>${esc(a.agent)}</span><span class="muted">${esc(agentStatusLabel(a.status))} · 负载 ${a.load} · 认证 ${esc(authHintLabel(a.auth_hint))}</span></div>`).join(''):'<div class="empty">暂无执行者信息</div>'}function renderSlots(){const target=document.getElementById('slots');if(state.project.runtime_pending){target.innerHTML='<div class="empty" role="status">正在读取工位运行现场…</div>';return}if(state.project.runtime_error){target.innerHTML='<div class="empty">工位运行现场读取失败：'+esc(state.project.runtime_error)+'</div>';return}const rs=state.project.slots||[];document.getElementById('slots').innerHTML=rs.length?rs.map(s=>`<div class="slot-row"><div><div>${esc(s.pane_id)} · ${esc(cleanStageLabel(s.stage_label))}</div><div class="task-meta">绑定 ${esc(s.bound_agent)} · 运行时 ${esc(s.live_agent||'空闲')} · ${esc(s.claimed_by?'被任务占用':'未占用')}</div></div><button class="mini" onclick="bindSlotPrompt('${esc(s.pane_id)}')">绑定</button></div>`).join(''):'<div class="empty">暂无用户预建智能体工位</div>'}
function autoFillWorkflowTitle(){
  const ti=document.getElementById('newTitle');
  if(!ti||ti.value.trim())return;
  const req=(document.getElementById('newRequirement')?.value||'').trim();
  if(!req)return;
  for(const line of req.split(/[\r\n]+/)){
    const clean=line.replace(/^(#{1,6}\s+|[-*+]+\s+|\d+[.、)]\s*|\[[ xX]\]\s*)+/,'').trim();
    if(clean&&!clean.startsWith('## 需求')&&clean!=='需求说明'&&clean!=='需求'){
      ti.value=clean.slice(0,50);
      break;
    }
  }
}
function showNewProjectModal(){
  openModal('新建工厂空间',`<div class="form"><label for="newProjPath">本地 Git 仓库路径</label><input id="newProjPath" placeholder="例如：/Users/user/my-app" oninput="autoFillProjectName()"><label for="newProjName">项目名称（可选）</label><input id="newProjName" placeholder="默认与文件夹同名"><label for="newProjTemplate">选用流水线模板</label><select id="newProjTemplate"></select><button class="btn primary" style="margin-top:8px" onclick="submitNewProject()">创建并装配工厂空间</button></div>`);
  populateTemplateSelect('newProjTemplate');
}
function autoFillProjectName(){
  const p=(document.getElementById('newProjPath')?.value||'').trim().replace(/[/\\]+$/,'');
  const nameInput=document.getElementById('newProjName');
  if(!nameInput)return;
  if(p){
    const segs=p.split(/[/\\]/);
    nameInput.value=segs[segs.length-1]||'';
  }
}
async function submitNewProject(){
  const path=(document.getElementById('newProjPath')?.value||'').trim();
  const name=(document.getElementById('newProjName')?.value||'').trim();
  const tmpl=document.getElementById('newProjTemplate')?.value||'software-development-v1';
  if(!path)return toast('请输入项目 Git 路径',true);
  try{
    toast('正在开辟并装配工厂空间…');
    const res=await api('/api/project/create',{method:'POST',body:JSON.stringify({project_root:path,project_name:name,template:tmpl})});
    closeModal();
    toast(res.already_registered?'项目已存在，已为您定位到该工厂空间':'工厂空间装配完成！');
    if(res.workspace_id){
      state.spaceId=res.workspace_id;
      saveViewState();
    }
    await refreshAll();
  }catch(e){
    toast('创建失败：'+e.message,true);
  }
}
async function submitAdoptSpace(wid){
  const path=(document.getElementById('adoptPath')?.value||'').trim();
  const tmpl=document.getElementById('adoptTemplate')?.value||'software-development-v1';
  try{
    toast('正在装配此空间为工厂车间…');
    const res=await api('/api/project/adopt',{method:'POST',body:JSON.stringify({workspace_id:wid,project_root:path,template:tmpl})});
    toast('空间装配完成，已接入工厂！');
    if(res.workspace_id){
      state.spaceId=res.workspace_id;
      saveViewState();
    }
    await refreshAll();
  }catch(e){
    toast('装配失败：'+e.message,true);
  }
}
function showUnregisterProjectModal(){
  if(!state.project||!state.projectId)return toast('请先选择当前工厂项目',true);
  const p=state.project.project;
  openModal('注销工厂项目',`<div style="line-height:1.6"><div style="font-size:15px;font-weight:650;margin-bottom:8px">确定要注销项目【${esc(p.project_name||p.project_id)}】吗？</div><div class="muted" style="font-size:13px;margin-bottom:16px">项目目录：<code>${esc(p.project_root)}</code><br>当前空间：<b>${esc(p.workspace_id||'')}</b></div><div style="background:var(--bg-subtle);border:1px solid var(--border-default);border-radius:10px;padding:12px;font-size:12px;margin-bottom:16px;color:var(--text-primary)"><div style="font-weight:600;margin-bottom:4px">注销影响说明：</div><div>1. <b>本地代码仓库绝对不碰</b>，保留所有代码与 Git 提交。</div><div>2. 工厂调度器将停止对该项目的自动任务派发与状态巡检。</div><div>3. 终端空间将从【当前工厂 <span class="dot ready" style="vertical-align:middle"></span>】退回为【独立终端 <span class="dot working" style="vertical-align:middle"></span>】。</div></div><label for="unregCloseSpace" style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;margin-bottom:16px"><input type="checkbox" id="unregCloseSpace"> 同时关闭终端工作区（仅关闭窗口标签页，不删除代码）</label><div style="display:flex;justify-content:flex-end;gap:8px"><button class="btn" onclick="closeModal()">取消</button><button class="btn danger-btn" onclick="submitUnregisterProject()">确认注销</button></div></div>`);
}
async function submitUnregisterProject(){
  if(!state.project||!state.projectId)return;
  const closeSpace=Boolean(document.getElementById('unregCloseSpace')?.checked);
  try{
    toast('正在注销工厂项目…');
    await api('/api/project/unregister',{method:'POST',body:JSON.stringify({project_id:state.projectId,close_workspace:closeSpace})});
    closeModal();
    toast('项目已注销成功');
    state.projectId=null;
    state.project=null;
    state.spaceId=null;
    saveViewState();
    await refreshAll();
  }catch(e){
    toast('注销失败：'+e.message,true);
  }
}
function showNewWorkflow(preferredTemplateId){
  if(!state.project||!state.space||state.space.relation!=='current_factory')return toast('请先选择当前工厂空间',true);
  openModal('新建需求',`<div class="form"><label for="newProjName">项目</label><input id="newProjName" value="${esc(state.project.project.project_name)}" disabled><label for="newTitle">本次任务名称</label><input id="newTitle" placeholder="例如：适配深色模式切换 / 修复结算页面浮点精度 Bug"><label for="newTemplate">工作流模板</label><select id="newTemplate"><option value="software-development-v1">software-development-v1（默认软件开发）</option></select><label for="newAgent">执行者策略</label><select id="newAgent"><option value="auto">auto（Router 自动）</option>${['opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>`<option>${a}</option>`).join('')}</select><label for="newRequirement">自然语言需求</label><textarea id="newRequirement" onblur="autoFillWorkflowTitle()"></textarea><button class="btn primary" onclick="submitNewWorkflowAsync()">启动工作流</button><div id="runWaitStatus" class="muted" style="min-height:16px;font-size:12px"></div></div>`);
  populateTemplateSelect('newTemplate',preferredTemplateId)
}
async function bindSlotPrompt(p){
  showPromptModal({
    title:'绑定智能体工位',
    label:'执行者名称（如 auto、claude、codex、opencode 等）',
    defaultValue:'auto',
    confirmText:'确认绑定',
    onConfirm:async(a)=>{
      if(!a)return;
      try{
        await api('/api/slot/bind',{method:'POST',body:JSON.stringify({pane_id:p,agent:a})});
        await loadProject(state.projectId,false);
        toast('工位已绑定 '+a);
      }catch(e){toast(e.message,true)}
    }
  });
}
return {
  submitNewWorkflowAsync, renderAgents, renderSlots, autoFillWorkflowTitle,
  showNewProjectModal, autoFillProjectName, submitNewProject, submitAdoptSpace,
  showUnregisterProjectModal, submitUnregisterProject, showNewWorkflow, bindSlotPrompt
};
})({
  state, showPromptModal, saveViewState,
  api, toast, esc,
  cleanStageLabel, agentStatusLabel, authHintLabel,
  populateTemplateSelect:(...args)=>populateTemplateSelect(...args), refreshAll, loadProject,
  openModal, closeModal
});
