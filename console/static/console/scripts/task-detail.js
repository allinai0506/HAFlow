/* 任务详情、轨迹分页与轮询；保持任务身份隔离。 */
var {
  canSteerTask, canForceReviewTask, canForcePassTask, toggleTaskDrawerMenu,
  closeTaskDrawerMenu, traceDotClass, toggleTraceEvent, retryTaskTrace,
  loadMoreTaskTrace, openTaskDrawer, closeTaskDrawer, switchTaskDrawerTab,
  showTask, showPane, askCoordinator
} = (function({
  state, taskDrawerNumTs, api,
  toast, esc, humanStatus,
  humanReason, badge, deliveryReason,
  taskStatusLabel, deliveryPipelineLine, taskDisplayName,
  formatElapsed, deliverableNoteHtml, deliverableRowHtml,
  openModal, openSignoffChamber
}){
function fmtClock(ts){const n=taskDrawerNumTs(ts);if(n===null)return '';const ms=n>1e12?n:n*1000;const d=new Date(ms);if(isNaN(d.getTime()))return '';return d.toLocaleTimeString('zh-CN',{hour12:false})}
function taskExecStart(t){t=t||{};const rt=t.runtime||{};return taskDrawerNumTs(t.started_at??rt.started_at)}
function taskDisplayStart(t){const e=taskExecStart(t);if(e!==null)return {label:'开始时间',ts:e};const c=taskDrawerNumTs((t||{}).created_at);if(c!==null)return {label:'创建时间',ts:c};return null}
function taskUpdatedAt(t){t=t||{};return taskDrawerNumTs(t.updated_at??t.last_activity_at)}
function taskDurationSecs(t){const s=taskExecStart(t);const u=taskUpdatedAt(t);if(s===null||u===null)return null;return Math.max(0,Math.round(u-s))}
function canSteerTask(t){return ['working','dispatched','rework','blocked','paused'].includes((t||{}).status)}
function canForceReviewTask(t){return (t||{}).status==='rework'}
function canForcePassTask(t){return (t||{}).stage_verdict==='blocked'}
function buildTaskEvents(task,proj){
  task=task||{};proj=proj||{};
  const evs=[];
  const push=(type,timestamp,title,detail,tone)=>{evs.push({type,timestamp:timestamp??null,title:title||type,detail:detail||'',tone:tone||'default'})};
  const created=taskDrawerNumTs(task.created_at);
  if(created!==null)push('task_created',created,'任务已创建','', 'default');
  const MAP={pending:['task_created','任务已创建'],dispatched:['task_dispatched','已派发'],working:['agent_started','执行者开始执行'],paused:['task_dispatched','已暂停'],blocked:['blocked','任务已阻塞'],failed:['failed','任务失败'],rework:['rework','返工中'],agent_done:['agent_done','执行者已完成'],completed:['completed','任务完成'],committed:['commit_created','已提交'],integrated:['completed','已集成'],cleanup_ready:['completed','待归档'],cleaned:['completed','已完成']};
  const hist=Array.isArray(task.status_history)?task.status_history:[];
  for(const h of hist){
    if(!h||typeof h!=='object')continue;
    const st=h.to||h.status;
    const at=taskDrawerNumTs(h.at);
    if(!st||at===null)continue;
    const m=MAP[st]||['status_changed',humanStatus(st)];
    if(m[0]==='task_created'&&evs.some(e=>e.type==='task_created'&&e.timestamp!==null&&Math.abs(e.timestamp-at)<2))continue;
    const tone=st==='failed'?'bad':(st==='blocked'?'warn':(['completed','committed','integrated','cleaned'].includes(st)?'done':(['working','dispatched','rework'].includes(st)?'info':'default')));
    const from=h.from?('由 '+h.from+' → '+st):'';
    push(m[0],at,m[1],from,tone);
  }
  const rt=task.runtime||{};
  const rtStart=taskDrawerNumTs(rt.started_at);
  if(rtStart!==null&&!evs.some(e=>e.type==='runtime_ready'))push('runtime_ready',rtStart,'运行现场就绪',(task.pane_id?('工位 '+task.pane_id):''),'info');
  const upd=taskUpdatedAt(task);
  const blockers=Array.isArray(proj.blockers)?proj.blockers:(proj.blocker?[proj.blocker]:((task.blocker||task.blocked_reason||task.sentinel_reason)?[task.blocker||task.blocked_reason||task.sentinel_reason]:[]));
  const realBlockers=blockers.map(humanReason).filter(b=>b&&String(b).trim());
  if(realBlockers.length&&upd!==null&&!evs.some(e=>(e.type==='blocked'||e.type==='failed')&&e.timestamp!==null&&Math.abs(e.timestamp-upd)<2))push('blocked',upd,'任务已阻塞',String(realBlockers[0]),'warn');
  let acts=proj.recent_activity;
  if(typeof acts==='string'&&acts)acts=acts.split('\n');
  if(Array.isArray(acts)){for(const a of acts.slice(0,6)){if(a&&String(a).trim())push('observation',null,String(a).trim(),'', 'default')}}
  const timed=evs.filter(e=>e.timestamp!==null).sort((a,b)=>a.timestamp-b.timestamp);
  const untimed=evs.filter(e=>e.timestamp===null);
  return timed.concat(untimed);
}
function renderTaskOverview(task,proj){
  task=task||{};proj=proj||{};
  const goal=task.goal||proj.goal||proj.intent||'';
  const result=task.last_result||task.result||'';
  const blocker=humanReason(proj.blocker||task.blocker||task.blocked_reason||task.sentinel_reason||'');
  const verdict=task.stage_verdict||'';
  const note=task.stage_verdict_note||'';
  let h='';
  if(goal)h+=`<div class="td-sec"><div class="td-lbl">目标</div><div class="td-txt">${esc(goal)}</div></div>`;
  if(result){h+=`<div class="td-sec"><div class="td-lbl">结果</div><div class="td-txt">${esc(typeof result==='object'?JSON.stringify(result,null,2):result)}</div></div>`}
  else{h+=`<div class="td-sec"><div class="td-lbl">结果</div><div class="empty" style="padding:8px 0">当前任务尚未产生结果</div></div>`}
  h+=`<div class="td-sec"><div class="td-lbl">状态</div><div>${badge(task.status||'unknown')}</div></div>`;
  if(blocker)h+=`<div class="td-sec"><div class="td-lbl">阻塞原因</div><div class="td-txt">${esc(blocker)}</div></div>`;
  if(verdict)h+=`<div class="td-sec"><div class="td-lbl">阶段结论</div><div class="td-txt">${esc(verdict)}${note?' · '+esc(note):''}</div></div>`;
  return h;
}
function renderTaskActivity(task,proj){
  const evs=buildTaskEvents(task,proj);
  if(!evs.length)return '<div class="empty">暂无可用活动记录</div>';
  return `<div class="td-timeline">${evs.map((e,i)=>{
    const t=e.timestamp!==null?fmtClock(e.timestamp):'·';
    const dot=e.tone==='done'?'done':e.tone==='warn'?'warn':e.tone==='bad'?'bad':e.tone==='info'?'info':'';
    const line=i<evs.length-1?'<div class="td-ev-line"></div>':'';
    return `<div class="td-ev"><div class="td-ev-time">${esc(t)}</div><div class="td-ev-rail"><span class="td-dot ${dot}"></span>${line}</div><div class="td-ev-body"><div class="td-ev-title">${esc(e.title)}</div>${e.detail?`<div class="td-ev-detail">${esc(e.detail)}</div>`:''}</div></div>`;
  }).join('')}</div>`;
}
function renderTaskArtifacts(task,proj){
  task=task||{};proj=proj||{};
  const packed=proj.deliverables;
  if(packed&&Array.isArray(packed.items)){
    const items=packed.items.filter(item=>String(item.task_id||'')===String(task.task_id||''));
    const note=deliverableNoteHtml(packed);
    if(!items.length&&!(Array.isArray(proj.artifacts)?proj.artifacts:[]).some(a=>a&&(a.kind==='evaluation'||String(a.path||'').indexOf('.herdr-loop')>=0))){
      return note||'<div class="empty">当前任务尚未产生可展示产物</div>';
    }
    const arts=(Array.isArray(proj.artifacts)?proj.artifacts:[]).filter(a=>a&&(a.kind==='evaluation'||String(a.path||'').indexOf('.herdr-loop')>=0));
    let h=note+items.map(deliverableRowHtml).join('');
    for(const a of arts){
      const files=Array.isArray(a.files)&&a.files.length?`<div class="td-ev-detail">${esc(a.files.slice(0,10).join(', '))}${a.files_changed?` · 共 ${a.files_changed} 个文件`:''}</div>`:'';
      h+=`<div class="td-art"><div class="td-art-hd"><span>${esc(a.name||a.kind||'产物')}</span><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind||'')}</span></div>${a.summary?`<div class="td-ev-detail">${esc(a.summary)}</div>`:''}${files}</div>`;
    }
    return h;
  }
  const arts=Array.isArray(proj.artifacts)?proj.artifacts:[];
  const cr=task.commit_result;
  const commitSha=task.commit||task.integrated_commit||((cr&&typeof cr==='object')?(cr.commit||cr.sha||''):(typeof cr==='string'?cr:''))||'';
  const commitMsg=((cr&&typeof cr==='object')?(cr.message||cr.summary||''):'')||task.commit_basis||'';
  const branch=task.branch||task.integration_branch||'';
  if(!arts.length&&!commitSha&&!branch)return '<div class="empty">当前任务尚未产生可展示产物</div>';
  let h='';
  if(commitSha)h+=`<div class="td-sec"><div class="td-lbl">提交</div><div class="td-txt" style="font-family:ui-monospace,Menlo,monospace">${esc(String(commitSha).slice(0,12))}</div>${commitMsg?`<div class="td-ev-detail">${esc(String(commitMsg).slice(0,200))}</div>`:''}</div>`;
  if(branch)h+=`<div class="td-sec"><div class="td-lbl">分支</div><div class="td-txt" style="font-family:ui-monospace,Menlo,monospace">${esc(branch)}</div></div>`;
  for(const a of arts){
    const files=Array.isArray(a.files)&&a.files.length?`<div class="td-ev-detail">${esc(a.files.slice(0,10).join(', '))}${a.files_changed?` · 共 ${a.files_changed} 个文件`:''}</div>`:'';
    h+=`<div class="td-art"><div class="td-art-hd"><span>${esc(a.name||a.kind||'产物')}</span><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind||'')}</span></div>${a.summary?`<div class="td-ev-detail">${esc(a.summary)}</div>`:''}${a.path?`<div class="td-ev-detail" style="font-family:ui-monospace,Menlo,monospace">${esc(a.path)}</div>`:''}${files}</div>`;
  }
  return h;
}
function renderTaskRuntime(task,proj){
  task=task||{};const rt=task.runtime||{};const live=(state.selectedTaskDetail&&state.selectedTaskDetail.live)||{};
  const rows=[];
  const add=(k,v)=>{if(v!==null&&v!==undefined&&String(v).trim()!=='')rows.push([k,String(v)])};
  add('执行者',task.agent||live.agent||'');
  add('执行者会话',rt.agent_session_id||rt.agent_name||live.agent_session_id||'');
  add('项目空间',task.workspace_id||rt.workspace_id||'');
  add('工作流节点',task.tab_id||rt.tab_id||'');
  add('智能体工位',task.pane_id||rt.pane_id||'');
  add('运行 ID',task.run_id||'');
  add('分支',task.branch||'');
  add('运行状态',rt.status||live.agent_status||'');
  const ds=taskDisplayStart(task);
  if(ds)add(ds.label,fmtClock(ds.ts));
  if(task.clone_path)add('代码目录',task.clone_path);
  if(!rows.length)return '<div class="empty">暂无运行时信息</div>';
  return `<div class="td-attrs">${rows.map(r=>`<div class="td-attr-k">${esc(r[0])}</div><div class="td-attr-v">${esc(r[1])}</div>`).join('')}</div>`;
}
function taskDrawerMenuHtml(t){
  t=t||{};
  const id=esc(t.task_id||'');
  const rt=(t.runtime||{});
  const paneId=esc(t.pane_id||rt.pane_id||'');
  const target={workflowId:t.workflow_id||state.workflowId,nodeId:t.node||t.stage||'',taskId:t.task_id,runId:t.run_id||'',paneId:t.pane_id||rt.pane_id||''};
  let h='';
  if(paneId)h+=`<button class="task-dropdown-item" onclick="closeTaskDrawerMenu();showPane('${paneId}','${id}')">查看工位</button>`;
  h+=`<button class="task-dropdown-item" onclick="closeTaskDrawerMenu();openControllerCockpit(${esc(JSON.stringify(target))})">在 Controller 中处理</button>`;
  return h;
}
function toggleTaskDrawerMenu(e){if(e)e.stopPropagation();const m=document.getElementById('taskDrawerMenu');if(m)m.classList.toggle('open')}
function closeTaskDrawerMenu(){const m=document.getElementById('taskDrawerMenu');if(m)m.classList.remove('open')}
function traceClock(ts){
  const n=taskDrawerNumTs(ts);
  if(n===null)return '';
  const ms=n>1e12?n:n*1000;
  const d=new Date(ms);
  if(isNaN(d.getTime()))return '';
  const p=v=>String(v).padStart(2,'0');
  return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());
}
function traceDotClass(ev){
  const status=ev&&ev.status;
  if(status==='success')return 'ok';
  if(status==='error')return 'err';
  if(status==='info'||(ev&&ev.kind==='file_edit'))return 'edit';
  return 'neutral';
}
function traceDetailRows(ev){
  const d=(ev&&ev.details)||{};
  const rows=[];
  const add=(k,v)=>{if(v!==null&&v!==undefined&&String(v).trim()!=='')rows.push([k,String(v)])};
  add('执行者',d.agent||ev.agent);
  add('工具',d.tool);
  add('文件',d.path);
  add('命令',d.command);
  add('结果',d.result);
  if(typeof d.exit_code==='number')add('退出码',d.exit_code);
  if(ev.timestamp!==null&&ev.timestamp!==undefined)add('原始时间',ev.timestamp);
  add('来源',d.source||ev.source);
  add('证据',d.source_ref||ev.source_ref);
  add('产物回执',d.observation_id);
  add('SHA-256',d.sha256);
  return rows;
}
function renderExecutionTrace(model){
  if(!model||model.status==='loading')return '<div class="empty">正在加载执行轨迹…</div>';
  if(model.status==='error')return '<div class="empty">执行轨迹加载失败：'+esc(model.message||'未知错误')+'</div><button class="btn mini" type="button" onclick="retryTaskTrace()">重试</button>';
  const events=Array.isArray(model.events)?model.events:[];
  const notes=Array.isArray(model.degradation)?model.degradation:[];
  const codes=notes.map(n=>n&&n.code);
  const refused=notes.some(n=>n&&n.code==='native_unverified'&&/独占|没有可核对/.test(String(n.message||'')));
  const sourceDown=codes.includes('native_unavailable')||codes.includes('native_corrupt')||codes.includes('trajectory_unavailable');
  let banner='';
  if(!events.length&&sourceDown)banner='<div class="empty">记录来源暂不可用。</div>';
  else if(!events.length&&refused)banner='<div class="empty">原生会话无法确认属于当前任务，已仅展示已确认的任务记录。</div>';
  else if(!events.length||model.availability==='none')banner='<div class="empty">当前任务暂无可用执行轨迹。</div>';
  else if(model.availability==='task_events')banner='<div class="trace-note">当前仅有任务状态记录，尚无可验证的执行者操作明细。</div>';
  if(events.length&&(codes.includes('native_unavailable')||codes.includes('native_corrupt')))banner+='<div class="trace-note">原生会话记录暂不可用，以下为已保存的任务状态。</div>';
  if(events.length&&refused&&model.availability!=='agent_actions')banner+='<div class="trace-note">原生会话无法确认属于当前任务，已仅展示已确认的任务记录。</div>';
  if(codes.includes('native_unsupported'))banner+='<div class="trace-note">该执行者暂无原生工具轨迹适配；以下展示已保存的派发、报告与产物回执。</div>';
  if(codes.includes('native_truncated'))banner+='<div class="trace-note">执行轨迹已截断，仅显示已读取的部分。</div>';
  if(!events.length)return banner;
  const statusKinds=['task_created','agent_started','task_completed','task_dispatched'];
  const lifecycle=model.availability!=='task_events'?events.filter(e=>statusKinds.includes(e.kind)&&!e.detail):[];
  const actions=events.filter(e=>!lifecycle.includes(e));
  const timed=actions.filter(e=>e.timestamp!==null&&e.timestamp!==undefined);
  const untimed=actions.filter(e=>e.timestamp===null||e.timestamp===undefined);
  const renderList=(list)=>{
    const icons={file_read:'▤',file_edit:'✎',test_passed:'✓',test_failed:'✗',retry:'↻',commit:'⊂⊃'};
    return list.map((e,i)=>{
      const hasTime=e.timestamp!==null&&e.timestamp!==undefined;
      const clock=hasTime?traceClock(e.timestamp):'时间未知';
      const icon=(e.kind&&icons[e.kind])?icons[e.kind]:'';
      const line=i<list.length-1?'<div class="td-ev-line"></div>':'';
      const titleClass=e.status==='error'?' err':'';
      const rows=traceDetailRows(e);
      const extra=rows.length?'<span class="trace-extra" hidden>'+rows.map(r=>'<span>'+esc(r[0])+'</span>'+esc(r[1])).join('<br>')+'</span>':'';
      return '<button type="button" class="td-ev trace-ev" onclick="toggleTraceEvent(this)"><span class="td-ev-time'+(hasTime?'':' muted')+'">'+esc(clock)+'</span><span class="td-ev-rail"><span class="td-dot '+traceDotClass(e)+'"></span>'+line+'</span><span class="td-ev-body"><span class="td-ev-title'+titleClass+'">'
        +(icon?'<span class="td-ev-icon">'+esc(icon)+'</span>':'')+esc(e.title||'')+'</span>'+(e.detail?'<span class="td-ev-detail">'+esc(e.detail)+'</span>':'')+extra+'</span></button>';
    }).join('');
  };
  let html=banner+'<div class="td-timeline trace-timeline">'+renderList(timed);
  if(untimed.length)html+='<div class="trace-note">以下记录没有原始时间，不与已知时间的事件比较先后。</div>'+renderList(untimed);
  html+='</div>';
  if(lifecycle.length)html+='<details class="td-sec"><summary>任务状态记录（'+lifecycle.length+'）</summary>'+renderList(lifecycle)+'</details>';
  if(model.appendError)html+='<div class="trace-note">后续轨迹加载失败：'+esc(model.appendError)+'</div><button class="btn mini" type="button" onclick="loadMoreTaskTrace()">重试</button>';
  else if(model.has_more)html+='<button class="btn mini" type="button" onclick="loadMoreTaskTrace()">加载更多</button>';
  return html;
}
function toggleTraceEvent(el){
  if(!el)return;
  const extra=el.querySelector('.trace-extra');
  if(!extra)return;
  extra.hidden=!extra.hidden;
}
function mergeTraceEvents(prev,incoming){
  const merged=Array.isArray(prev)?prev.slice():[];
  if(!Array.isArray(incoming)||!incoming.length)return merged;
  const index=new Map();
  merged.forEach((item,pos)=>{if(item&&item.id)index.set(item.id,pos)});
  for(const item of incoming){
    if(!item||!item.id){merged.push(item);continue}
    const pos=index.get(item.id);
    if(pos===undefined){index.set(item.id,merged.length);merged.push(item)}
    else merged[pos]=item;
  }
  return merged;
}
function applyTraceResponse(state,tid,seq,payload){
  if(!state||state.selectedTaskId!==tid||state.taskTraceSeq!==seq)return false;
  const prev=state.taskTrace&&Array.isArray(state.taskTrace.events)?state.taskTrace.events:[];
  const incoming=payload&&Array.isArray(payload.events)?payload.events:[];
  state.taskTrace={
    status:'ready',
    task_id:payload.task_id,
    run_id:payload.run_id,
    availability:payload.availability,
    events:payload&&payload.append?mergeTraceEvents(prev,incoming):incoming,
    has_more:!!payload.has_more,
    next_cursor:payload.next_cursor||null,
    degradation:Array.isArray(payload.degradation)?payload.degradation:[],
    appending:false
  };
  return true;
}
function retryTaskTrace(){loadTaskTrace(state.selectedTaskId,true)}
function traceTaskWorking(){
  const t=state.selectedTaskDetail&&state.selectedTaskDetail.task;
  return !!t&&['working','dispatched','rework'].includes(t.status);
}
async function refreshSelectedTaskStatus(){
  const tid=state.selectedTaskId;
  if(!tid||!state.selectedTaskDetail)return;
  try{
    const td=await api('/api/task?id='+encodeURIComponent(tid));
    if(state.selectedTaskId!==tid||!state.selectedTaskDetail)return;
    const c=state.selectedTaskDetail;
    c.task=Object.assign({},c.task,(td&&td.task)||{});
    if(td&&td.runtime)c.live=td.runtime;
  }catch(e){/* 取态失败保留上次快照，下一 tick 重试 */}
}
function stopTaskTracePoll(){
  if(state.taskTracePoll){clearInterval(state.taskTracePoll);state.taskTracePoll=null}
}
function pollTaskTrace(){
  if(!(state.taskDrawerTab==='trace'&&state.selectedTaskId)){stopTaskTracePoll();return}
  if(document.hidden)return;
  return refreshSelectedTaskStatus().then(()=>{
    if(!(state.taskDrawerTab==='trace'&&state.selectedTaskId)){stopTaskTracePoll();return}
    if(traceTaskWorking())return loadTaskTrace(state.selectedTaskId,true);
    const t=state.selectedTaskDetail&&state.selectedTaskDetail.task;
    const now=t&&t.status?t.status:null;
    if(t&&['completed','failed','superseded','cleaned','committed','integrated','cleanup_ready'].includes(t.status)){stopTaskTracePoll();return loadTaskTrace(state.selectedTaskId,true)}
    if(now!==state.traceLastPolledStatus){state.traceLastPolledStatus=now;return loadTaskTrace(state.selectedTaskId,true)}
    state.traceLastPolledStatus=now;
  });
}
function startTaskTracePoll(){
  stopTaskTracePoll();
  if(!(state.taskDrawerTab==='trace'&&state.selectedTaskId))return;
  const t=state.selectedTaskDetail&&state.selectedTaskDetail.task;
  state.traceLastPolledStatus=t&&t.status?t.status:null;
  state.taskTracePoll=setInterval(pollTaskTrace,8000);
}
async function loadTaskTrace(tid,force){
  if(!tid)return;
  const seq=state.taskTraceSeq||0;
  if(!force&&state.taskTrace&&state.taskTrace.task_id===tid&&state.taskTrace.status==='ready')return;
  const loaded=state.taskTrace&&state.taskTrace.task_id===tid&&Array.isArray(state.taskTrace.events)?state.taskTrace.events:[];
  // 后端单页上限 200：深度未达上限用覆盖式窗口；达到上限后只用 cursor 取增量并按 id 合并。
  // cursor 必须取最后一条有时间戳的事件：无时间戳的任务历史/提交事件排在全部有时间戳事件之后，
  // 以它们作 cursor 会永久漏掉随后到达的新原生动作。
  let cursorId=null;
  if(loaded.length>=200){
    for(let i=loaded.length-1;i>=0;i--){if(loaded[i]&&loaded[i].timestamp&&loaded[i].id){cursorId=loaded[i].id;break}}
  }
  if(!cursorId){
    state.taskTrace={status:'loading',task_id:tid,events:[]};
    if(state.taskDrawerTab==='trace'||state.taskDrawerTab==='activity')renderTaskDrawer();
  }
  try{
    let path='/api/task/trajectory?id='+encodeURIComponent(tid)+'&limit='+(cursorId?100:Math.min(200,Math.max(100,loaded.length)));
    if(cursorId)path+='&cursor='+encodeURIComponent(cursorId);
    const data=await api(path);
    if(cursorId)data.append=true;
    if(!applyTraceResponse(state,tid,seq,data))return;
  }catch(e){
    // cursor 指向的事件可能已被后续证据抑制（tests_completed 让位给 verification_completed），
    // 增量请求会以 400 "cursor 无效" 失败。终态那次刷新之后轮询已停止，此处若清空轨迹就永久坏掉，
    // 因此退回无 cursor 的覆盖窗口并按 id 合并。
    let recovered=false;
    if(cursorId){
      try{
        const retry=await api('/api/task/trajectory?id='+encodeURIComponent(tid)+'&limit='+Math.min(200,Math.max(100,loaded.length)));
        retry.append=true;
        recovered=applyTraceResponse(state,tid,seq,retry);
      }catch(retryError){recovered=false}
    }
    if(!recovered){
      if(state.selectedTaskId!==tid||state.taskTraceSeq!==seq)return;
      state.taskTrace={status:'error',task_id:tid,message:e&&e.message?e.message:'未知错误',events:[]};
    }
  }
  if(state.selectedTaskId===tid&&(state.taskDrawerTab==='trace'||state.taskDrawerTab==='activity'))renderTaskDrawer();
}
async function loadMoreTaskTrace(){
  const tid=state.selectedTaskId;
  const model=state.taskTrace;
  if(!tid||!model||!model.has_more||!model.next_cursor||model.appending)return;
  const seq=state.taskTraceSeq||0;
  model.appending=true;
  try{
    const data=await api('/api/task/trajectory?id='+encodeURIComponent(tid)+'&limit=100&cursor='+encodeURIComponent(model.next_cursor));
    if(state.selectedTaskId!==tid||state.taskTraceSeq!==seq)return;
    data.append=true;
    applyTraceResponse(state,tid,seq,data);
  }catch(e){
    if(state.selectedTaskId!==tid||state.taskTraceSeq!==seq)return;
    const current=state.taskTrace&&Array.isArray(state.taskTrace.events)?state.taskTrace:model;
    state.taskTrace={
      status:'ready',
      task_id:current.task_id||tid,
      run_id:current.run_id,
      availability:current.availability,
      events:current.events||[],
      has_more:!!current.has_more,
      next_cursor:current.next_cursor||null,
      degradation:Array.isArray(current.degradation)?current.degradation:[],
      appending:false,
      appendError:e&&e.message?e.message:'未知错误'
    };
  }
  if(state.taskDrawerTab==='trace'||state.taskDrawerTab==='activity')renderTaskDrawer();
}
function renderTaskDrawer(){
  const c=state.selectedTaskDetail;
  const drawer=document.getElementById('taskDrawer');
  if(!c||!drawer)return;
  const task=c.task||{};const proj=c.proj||{};
  const title=taskDisplayName(task);
  const dur=taskDurationSecs(task);
  document.getElementById('taskDrawerIcon').textContent=['completed','committed','integrated','cleaned'].includes(task.status)?'✓':(['blocked','failed'].includes(task.status)?'!':(['working','dispatched','rework'].includes(task.status)?'●':'○'));
  document.getElementById('taskDrawerTitle').textContent=title;
  document.getElementById('taskDrawerId').textContent=task.task_id||'';
  const parts=[];
  if(task.status)parts.push(taskStatusLabel(task));
  if(task.agent)parts.push(task.agent);
  if(dur!==null)parts.push(formatElapsed(dur));
  document.getElementById('taskDrawerMeta').textContent=parts.join(' · ');
  const primary=document.getElementById('taskDrawerPrimary');
  if(primary)primary.onclick=()=>openSignoffChamber(task.task_id);
  document.getElementById('taskDrawerMenu').innerHTML=taskDrawerMenuHtml(task);
  const stage=task.stage_label||task.stage||task.node_label||task.node||'';
  const rt=task.runtime||{};
  const rtShort=[task.workspace_id||rt.workspace_id||'',task.pane_id||rt.pane_id||''].filter(Boolean).join(' / ');
  const ds=taskDisplayStart(task);
  const attrRows=[];
  if(task.agent)attrRows.push(['执行者',task.agent]);
  if(stage)attrRows.push(['阶段',stage]);
  if(task.status)attrRows.push(['状态',taskStatusLabel(task)]);
  if(rtShort)attrRows.push(['运行现场',rtShort]);
  if(ds)attrRows.push([ds.label,fmtClock(ds.ts)]);
  if(dur!==null)attrRows.push(['耗时',formatElapsed(dur)]);
  const pipelineLine=deliveryPipelineLine(task);
  if(pipelineLine)attrRows.push(['交付管道',pipelineLine]);
  const waitReason=deliveryReason(task);
  if(waitReason)attrRows.push(['当前等待',waitReason]);
  const tab=state.taskDrawerTab==='activity'?'trace':state.taskDrawerTab;
  const tabs=['overview','trace','artifacts','runtime'];
  const labels={overview:'tdTabOverview',trace:'tdTabActivity',artifacts:'tdTabArtifacts',runtime:'tdTabRuntime'};
  for(const k of tabs){const el=document.getElementById(labels[k]);if(el){const on=tab===k;el.classList.toggle('active',on);el.setAttribute('aria-selected',on?'true':'false')}}
  let body='';
  if(attrRows.length&&tab!=='trace')body+=`<div class="td-attrs">${attrRows.map(r=>`<div class="td-attr-k">${esc(r[0])}</div><div class="td-attr-v">${esc(r[1])}</div>`).join('')}</div>`;
  if(tab==='trace')body+=renderExecutionTrace(state.taskTrace)+'<div class="td-sec"><div class="td-lbl">本任务产出</div>'+renderTaskArtifacts(task,proj)+'</div>';
  else if(tab==='artifacts')body+=renderTaskArtifacts(task,proj);
  else if(tab==='runtime')body+=renderTaskRuntime(task,proj);
  else body+=renderTaskOverview(task,proj);
  document.getElementById('taskDrawerBody').innerHTML=body;
}
function resolveTaskDetail(tid,td,proj,tdError,projError,rowTask){
  if(!td&&!proj){
    const msg=(tdError&&tdError.message)||(projError&&projError.message)||'任务详情加载失败';
    return {status:'error',message:msg};
  }
  const fetched=(td&&td.task)||{};
  const task=Object.assign({},rowTask||{},fetched);
  if(!Object.keys(task).length&&proj)Object.assign(task,{task_id:proj.task_id,workflow_id:proj.workflow_id,node:proj.node,agent:proj.agent,status:proj.status,goal:proj.goal});
  // The drawer's own projection is authoritative when the row snapshot is stale.
  if(proj&&proj.delivery)task.delivery=proj.delivery;
  return {status:'ok',task,proj:proj||{},live:((td&&td.runtime)||{})};
}
async function openTaskDrawer(tid){
  if(!tid)return;
  state.selectedTaskId=tid;state.taskDrawerTab='overview';state.taskTrace=null;state.taskTraceSeq=(state.taskTraceSeq||0)+1;
  const drawer=document.getElementById('taskDrawer');
  if(drawer){drawer.hidden=false;drawer.classList.add('open')}
  document.getElementById('taskDrawerTitle').textContent='加载中…';
  document.getElementById('taskDrawerId').textContent=tid;
  document.getElementById('taskDrawerMeta').textContent='';
  document.getElementById('taskDrawerBody').innerHTML='<div class="empty">正在加载任务详情…</div>';
  document.querySelectorAll('.task.task-highlight').forEach(el=>el.classList.remove('task-highlight'));
  const row=document.querySelector(`[data-task-id="${CSS.escape?CSS.escape(tid):tid}"]`);
  if(row)row.classList.add('task-highlight');
  try{
    const [tdRes,projRes]=await Promise.all([
      api('/api/task?id='+encodeURIComponent(tid)).then(d=>({ok:true,data:d}),e=>({ok:false,error:e})),
      api('/api/task/projection?id='+encodeURIComponent(tid)).then(d=>({ok:true,data:d}),e=>({ok:false,error:e}))
    ]);
    if(state.selectedTaskId!==tid)return;
    const td=tdRes.ok?tdRes.data:null;
    const proj=projRes.ok?projRes.data:null;
    let rowTask=null;
    try{rowTask=((state.workflow&&state.workflow.tasks)||[]).find(t=>t.task_id===tid)||null}catch(e){rowTask=null}
    const r=resolveTaskDetail(tid,td,proj,tdRes.ok?null:tdRes.error,projRes.ok?null:projRes.error,rowTask);
    if(r.status==='error'){
      document.getElementById('taskDrawerTitle').textContent='任务详情加载失败';
      document.getElementById('taskDrawerMeta').textContent='';
      document.getElementById('taskDrawerBody').innerHTML=`<div class="empty">加载失败：${esc(r.message)}</div>`;
      state.selectedTaskDetail=null;
      return;
    }
    state.selectedTaskDetail={task:r.task,proj:r.proj,live:r.live};
    renderTaskDrawer();
  }catch(e){document.getElementById('taskDrawerBody').innerHTML=`<div class="empty">加载失败：${esc(e.message)}</div>`}
}
function closeTaskDrawer(){
  const drawer=document.getElementById('taskDrawer');
  if(!drawer||drawer.hidden)return;
  stopTaskTracePoll();
  drawer.classList.remove('open');drawer.hidden=true;
  state.selectedTaskId=null;state.selectedTaskDetail=null;closeTaskDrawerMenu();
  document.querySelectorAll('.task.task-highlight').forEach(el=>el.classList.remove('task-highlight'));
}
function switchTaskDrawerTab(tab){
  if(tab==='activity')tab='trace';
  state.taskDrawerTab=tab;
  renderTaskDrawer();
  const drawer=document.getElementById('taskDrawer');
  if(drawer&&drawer.hidden){drawer.hidden=false;drawer.classList.add('open')}
  if(tab==='trace'){loadTaskTrace(state.selectedTaskId);startTaskTracePoll()}
  else stopTaskTracePoll();
}
async function showTask(id){try{const [td,proj]=await Promise.all([api('/api/task?id='+encodeURIComponent(id)).catch(()=>null),api('/api/task/projection?id='+encodeURIComponent(id)).catch(()=>null)]);const d=proj||(td&&td.task)||{};const raw=td||proj||{};const st=d.status||(td&&td.task&&td.task.status)||'unknown';const intent=d.intent||(td&&td.task&&td.task.goal)||'无明确意图描述';const blockers=Array.isArray(d.blockers)?d.blockers:(d.blocker?[d.blocker]:[]);const ms=Array.isArray(d.milestones)?d.milestones:[];const arts=Array.isArray(d.artifacts)?d.artifacts:[];const acts=Array.isArray(d.recent_activity)?d.recent_activity:(typeof d.recent_activity==='string'&&d.recent_activity?d.recent_activity.split('\n'):[]);const blkHtml=blockers.length?`<div class="proj-blk"><strong>⚠️ 卡点告警:</strong><span>${esc(blockers.map(humanReason).join('; '))}</span></div>`:'';const msHtml=ms.length?`<div class="proj-sec"><div class="proj-lbl">动态路标</div>${ms.map(m=>`<div class="proj-ms"><span class="proj-ms-dot ${m.status}">${m.status==='completed'?'✓':(m.status==='in_progress'?'›':'·')}</span><span style="${m.status==='completed'?'color:var(--text)':(m.status==='in_progress'?'color:var(--warn);font-weight:600':'color:var(--muted)')}">${esc(m.label)}</span></div>`).join('')}</div>`:'';const artHtml=arts.length?`<div class="proj-sec"><div class="proj-lbl">核心产物</div>${arts.map(a=>`<div class="proj-art"><div class="proj-art-hd"><span>${esc(a.name||a.kind)}</span><span class="badge ${a.passed?'cleaned':(a.kind==='evaluation'?'failed':'waiting')}">${esc(a.kind)}</span></div><div class="muted">${esc(a.summary||'')}</div></div>`).join('')}</div>`:'';const actHtml=acts.length?`<div class="proj-sec"><div class="proj-lbl">近期动态提炼</div><ul class="proj-acts">${acts.map(a=>`<li>${esc(a)}</li>`).join('')}</ul></div>`:'';const body=`<div class="proj-box"><div style="display:flex;justify-content:space-between;align-items:center;padding-bottom:8px;border-bottom:1px solid var(--line)"><div><span class="badge ${st}">${esc(st)}</span><span style="margin-left:8px;font-size:12px;color:var(--muted)">执行者: <strong>${esc(d.agent||'-')}</strong></span><span style="margin-left:8px;font-size:12px;color:var(--muted)">工位: <strong>${esc(d.node||'-')}</strong></span></div><button class="mini" onclick="const el=document.getElementById('taskRawPre');if(el)el.style.display=el.style.display==='none'?'block':'none'">原始数据</button></div>${blkHtml}<div class="proj-sec"><div class="proj-lbl">当前语义意图</div><div class="proj-txt">${esc(intent)}</div></div>${msHtml}${artHtml}${actHtml}<div id="taskRawPre" style="display:none;margin-top:8px"><div class="proj-lbl">原始调试数据</div><pre>${esc(JSON.stringify(raw,null,2))}</pre></div></div>`;openModal('任务白盒简报 · '+id,body)}catch(e){toast(e.message,true)}}async function showPane(id,taskId){
  if(!id)return toast('没有工位',true);
  const tasks=[...((state.workflow&&state.workflow.tasks)||[]),state.selectedTaskDetail?.task].filter(Boolean);
  const task=taskId?tasks.find(t=>t.task_id===taskId&&(t.pane_id||t.runtime?.pane_id)===id):null;
  const target=task?{workflowId:task.workflow_id||state.workflow?.workflow?.workflow_id,nodeId:task.node||task.stage||'',taskId:task.task_id,runId:task.run_id||'',paneId:id}:null;
  try{
    const d=await api('/api/pane/read?id='+encodeURIComponent(id));
    const shortcut=target?'<button class="mini" onclick="closeModal();openControllerCockpit('+esc(JSON.stringify(target))+')">在 Controller 中处理</button>':'';
    openModal('工位 '+id,shortcut+'<pre>'+esc(d.output)+'</pre>');
  }catch(e){toast(e.message,true)}
}async function askCoordinator(id){
  const task=((state.workflow&&state.workflow.tasks)||[]).find(t=>t.task_id===id)||(state.selectedTaskDetail?.task?.task_id===id?state.selectedTaskDetail.task:null);
  return openControllerCockpit({workflowId:task?.workflow_id||state.workflow?.workflow?.workflow_id,nodeId:task?.node||task?.stage||'',taskId:id,runId:task?.run_id||'',paneId:task?.pane_id||task?.runtime?.pane_id||''});
}
return {
  canSteerTask, canForceReviewTask, canForcePassTask, toggleTaskDrawerMenu,
  closeTaskDrawerMenu, traceDotClass, toggleTraceEvent, retryTaskTrace,
  loadMoreTaskTrace, openTaskDrawer, closeTaskDrawer, switchTaskDrawerTab,
  showTask, showPane, askCoordinator
};
})({
  state, taskDrawerNumTs, api,
  toast, esc, humanStatus,
  humanReason, badge, deliveryReason,
  taskStatusLabel, deliveryPipelineLine, taskDisplayName,
  formatElapsed, deliverableNoteHtml, deliverableRowHtml,
  openModal, openSignoffChamber
});
