
setInterval(()=>{if(!document.hidden)refreshAll()},600000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshAll()});
(function(){const v=loadViewState();if(!v)return;state.opsMode=!!v.opsMode;state.dashMode=!!v.dashMode;state.dashWorkflowId=v.dashWorkflowId||null;state.spaceId=v.spaceId||null;state.workflowId=v.workflowId||null;state.shellView=v.shellView||'workbench';})();
function simpleWorkbenchUrl(){
  const q=new URLSearchParams();
  if(state.projectId)q.set('project_id',state.projectId);
  if(state.workflowId&&!state.workflowId.startsWith('__'))q.set('workflow_id',state.workflowId);
  return '/simple?'+q.toString();
}
async function initFromUrlOrState(){
  const p=new URLSearchParams(window.location.search);
  let qWf=p.get('workflow_id');
  const qProject=p.get('project_id');
  if(qProject&&!qWf){
    state.workflowId=null;state.shellView='workbench';
    try{await loadProject(qProject);}catch(e){toast(e.message,true);}
  }
  const qTask=p.get('task_id'),qPane=p.get('pane_id'),qOps=p.get('ops'),qView=p.get('view');
  if(!qWf&&qTask){
    try{
      const td=await api('/api/task?id='+encodeURIComponent(qTask));
      if(td&&td.task&&td.task.workflow_id)qWf=td.task.workflow_id;
    }catch(e){}
  }
  if(!qWf&&!qTask&&!qPane&&!qOps&&!qView&&!state.dashMode&&!state.opsMode&&state.shellView==='workbench'&&state.workflowId&&!state.workflowId.startsWith('__')){
    qWf=state.workflowId;
  }
  if(!qWf&&!qTask&&!qPane&&!qOps&&!qView){
    if(state.dashMode)showDashboard();
    else if(state.workflowId==='__templates__'||state.shellView==='templates'){showTemplateLibrary();refreshAll();}
    else if(state.shellView&&state.shellView!=='workbench')showShellView(state.shellView);
    else state.opsMode?showOpsCenter():refreshAll();
    return;
  }
  if(qOps==='1'||qOps==='true'||qView==='ops'){
    state.opsMode=true;
    state.dashMode=false;
    state.shellView='ops';
  }else if(qView==='dashboard'){
    state.dashMode=true;
    state.opsMode=false;
    state.shellView='dashboard';
    if(qWf)state.dashWorkflowId=qWf;
  }else if(qView==='templates'){
    state.opsMode=false;
    state.dashMode=false;
    state.shellView='templates';
    state.workflowId='__templates__';
  }else if(qView&&['alerts','agents','slots','workflows','workbench','experience'].includes(qView)){
    state.opsMode=false;
    state.dashMode=false;
    state.shellView=qView;
    if(state.workflowId==='__templates__')state.workflowId=null;
  }
  if(qWf&&!state.dashMode&&state.workflowId!=='__templates__'){
    state.opsMode=false;
    state.dashWorkflowId=null;
    state.workflowId=qWf;
    try{
      const d=await api('/api/workflow?id='+encodeURIComponent(qWf)+'&lightweight=1');
      if(d&&d.project){
        if(d.project.project_id)state.projectId=d.project.project_id;
        if(d.project.workspace_id)state.spaceId=d.project.workspace_id;
      }
      if(state.shellView==='workbench'){await loadWorkflow(qWf,d);state.initialWorkflowPainted=true;}
    }catch(e){toast(e.message,true)}
  }
  if(state.dashMode){
    await showDashboard();
  }else if(state.opsMode){
    await showOpsCenter();
  }else if(state.shellView==='templates'){
    // 冷启动直开:先渲染,再水合项目/空间并按有效模板重绘(否则 effectiveTemplateId 回落默认模板)
    await showTemplateLibrary();
    refreshAll();
  }else if(state.shellView&&state.shellView!=='workbench'){
    showShellView(state.shellView);
    refreshAll();
  }else{
    await refreshAll();
    if(qWf&&state.workflowId!==qWf){
      try{await loadWorkflow(qWf);}catch(e){}
    }
    if(qTask){
      const el=document.querySelector(`[data-task-id="${CSS.escape?CSS.escape(qTask):qTask}"]`);
      if(el){el.scrollIntoView({behavior:'smooth',block:'center'});el.classList.add('task-highlight');}
      await openTaskDrawer(qTask);
    }else if(qPane){
      await showPane(qPane);
    }
  }
}
window.addEventListener('popstate',()=>{initFromUrlOrState();});
initFromUrlOrState();
