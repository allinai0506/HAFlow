/* 执行者自检批次、结果、进度和重试。 */
var {
  retrySinglePreflight, runPreflight
} = (function({
  state, api, toast,
  esc, authHintLabel, openModal
}){

let preflightRunId=0;
const preflightState={total:0,completed:0,ready:0,warn:0,error:0,disabled:0,results:{}};
const preflightStatusLabel=s=>({
  READY:'就绪',
  DISABLED:'已禁用',
  UNKNOWN:'未知',
  MISSING:'未安装',
  WARN:'警告',
  TOKEN_EXHAUSTED:'额度耗尽',
  AUTH_REQUIRED:'需要认证',
  PROVIDER_ERROR:'服务端繁忙',
  LOCAL_ERROR:'本地异常',
  TRUST_REQUIRED:'需要信任',
  UPDATE_BLOCKED:'更新受阻',
  TIMEOUT:'超时',
  ERROR:'错误'
})[s]||s;
const preflightHardFailures=new Set([
  'TOKEN_EXHAUSTED','AUTH_REQUIRED','PROVIDER_ERROR','LOCAL_ERROR','TRUST_REQUIRED',
  'UPDATE_BLOCKED','TIMEOUT','ERROR','MISSING'
]);
function buildPreflightRowHtml(a){
  const agent=a.agent||a;
  const auth=authHintLabel(a.auth_hint||'unknown');
  return `<div class="preflight-row" id="pf-row-${esc(agent)}">`
    +`<div style="flex:1;min-width:0">`
    +`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">`
    +`<i class="dot" id="pf-dot-${esc(agent)}"></i>`
    +`<strong>${esc(agent)}</strong> `
    +`<span id="pf-badge-${esc(agent)}" class="badge waiting">等待检测…</span>`
    +`<span id="pf-spin-${esc(agent)}" class="spinner" style="width:11px;height:11px;border-width:1.5px;display:none"></span>`
    +`</div>`
    +`<div class="task-meta" id="pf-note-${esc(agent)}" style="margin-top:4px">准备执行最小探针…</div>`
    +`<div id="pf-output-${esc(agent)}"></div>`
    +`</div>`
    +`<div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex-shrink:0">`
    +`<span class="task-meta" id="pf-auth-${esc(agent)}">${esc(auth)}</span>`
    +`<div id="pf-action-${esc(agent)}"></div>`
    +`</div>`
    +`</div>`;
}
function updateAgentPreflightResult(agent,row){
  const deep=row.deep||{};
  const final=row.final_status||row.shallow_status||'UNKNOWN';
  let dotCls='';
  let badgeCls='waiting';
  let rowCls='';
  if(final==='READY'){
    dotCls='ready';badgeCls='cleaned';rowCls='is-ready';
  }else if(preflightHardFailures.has(final)){
    dotCls='failed';badgeCls='failed';rowCls='is-failed';
  }else if(final==='WARN'){
    dotCls='working';badgeCls='working';rowCls='is-warn';
  }else if(final==='DISABLED'){
    dotCls='disabled';badgeCls='superseded';
  }
  const note=deep.note||row.version||row.binary||(final==='MISSING'?'未安装':'');
  const adapter=deep.adapter?(' · '+esc(deep.adapter)):'';
  const out=(deep.output||'').slice(-800);
  const outHtml=out?('<pre class="ctl-pre-light">'+esc(out)+'</pre>'):'';
  const rowEl=document.getElementById('pf-row-'+agent);
  if(rowEl)rowEl.className='preflight-row '+rowCls;
  const dotEl=document.getElementById('pf-dot-'+agent);
  if(dotEl)dotEl.className='dot '+dotCls;
  const badgeEl=document.getElementById('pf-badge-'+agent);
  if(badgeEl){badgeEl.className='badge '+badgeCls;badgeEl.textContent=preflightStatusLabel(final)}
  const spinEl=document.getElementById('pf-spin-'+agent);
  if(spinEl)spinEl.style.display='none';
  const noteEl=document.getElementById('pf-note-'+agent);
  if(noteEl)noteEl.innerHTML=esc(note)+adapter;
  const outEl=document.getElementById('pf-output-'+agent);
  if(outEl)outEl.innerHTML=outHtml;
  const authEl=document.getElementById('pf-auth-'+agent);
  if(authEl)authEl.textContent=authHintLabel(row.auth_hint||'unknown');
  const actEl=document.getElementById('pf-action-'+agent);
  if(actEl)actEl.innerHTML=`<button class="mini" onclick="retrySinglePreflight('${esc(agent)}')">重试</button>`;
  preflightState.results[agent]=row;
  updatePreflightSummary();
}
function updatePreflightSummary(){
  const rs=Object.values(preflightState.results);
  const completed=rs.length;
  const total=preflightState.total;
  let ready=0,warn=0,err=0,dis=0;
  for(const r of rs){
    const f=r.final_status||r.shallow_status||'UNKNOWN';
    if(f==='READY')ready++;
    else if(preflightHardFailures.has(f))err++;
    else if(f==='WARN')warn++;
    else if(f==='DISABLED')dis++;
  }
  const pct=total?Math.min(100,Math.round((completed/total)*100)):0;
  const bar=document.getElementById('preflightProgressBar');
  if(bar)bar.style.width=pct+'%';
  const cnt=document.getElementById('preflightCounter');
  if(cnt)cnt.textContent=`${completed} / ${total}`;
  const sReady=document.getElementById('preflightStatReady');
  if(sReady){sReady.style.display=ready?'inline':'none';sReady.textContent=`${ready} 就绪`}
  const sWarn=document.getElementById('preflightStatWarn');
  if(sWarn){sWarn.style.display=warn?'inline':'none';sWarn.textContent=`${warn} 警告`}
  const sErr=document.getElementById('preflightStatError');
  if(sErr){sErr.style.display=err?'inline':'none';sErr.textContent=`${err} 异常`}
  const sDis=document.getElementById('preflightStatDisabled');
  if(sDis){sDis.style.display=dis?'inline':'none';sDis.textContent=`${dis} 已禁用`}
  if(completed>=total&&total>0){
    const topSpin=document.getElementById('preflightTopSpinner');
    if(topSpin)topSpin.style.display='none';
    const titleEl=document.getElementById('preflightSummaryTitle');
    if(titleEl)titleEl.innerHTML=`<span class="good-text">✓ 全部执行者自检已完成</span>`;
    const btn=document.getElementById('preflightRerunAllBtn');
    if(btn)btn.disabled=false;
    toast('深度自检完成');
  }
}
async function executeSinglePreflight(agent,runId){
  const dotEl=document.getElementById('pf-dot-'+agent);
  if(dotEl)dotEl.className='dot working';
  const badgeEl=document.getElementById('pf-badge-'+agent);
  if(badgeEl){badgeEl.className='badge in_progress';badgeEl.textContent='检测中…'}
  const spinEl=document.getElementById('pf-spin-'+agent);
  if(spinEl)spinEl.style.display='inline-block';
  const noteEl=document.getElementById('pf-note-'+agent);
  if(noteEl)noteEl.textContent='正在发起沙盒探针与真实最小调用…';
  const actEl=document.getElementById('pf-action-'+agent);
  if(actEl)actEl.innerHTML='';
  try{
    const res=await api('/api/deep-preflight?id='+encodeURIComponent(state.projectId)+'&agent='+encodeURIComponent(agent));
    if(runId&&runId!==preflightRunId)return;
    const row=(res.agents&&res.agents[0])||{agent:agent,final_status:'UNKNOWN'};
    updateAgentPreflightResult(agent,row);
  }catch(e){
    if(runId&&runId!==preflightRunId)return;
    updateAgentPreflightResult(agent,{agent:agent,final_status:'ERROR',deep:{attempted:true,status:'ERROR',note:'探针执行异常: '+e.message,output:e.stack||''}});
  }
}
async function retrySinglePreflight(agent){
  if(!state.projectId)return toast('当前空间不参与工厂调度',true);
  toast(`正在重新自检 ${agent}…`);
  delete preflightState.results[agent];
  updatePreflightSummary();
  await executeSinglePreflight(agent,0);
  toast(`${agent} 自检完成`);
}
async function runPreflight(){
  if(!state.projectId)return toast('当前空间不参与工厂调度',true);
  preflightRunId=Date.now();
  const currentRunId=preflightRunId;
  const agentItems=(state.project&&state.project.agents&&state.project.agents.length)
    ?state.project.agents
    :['opencode','codex','claude','qodercli','agy','pi','grok','kimi'].map(a=>({agent:a,auth_hint:'unknown'}));
  const agentNames=agentItems.map(a=>a.agent);
  preflightState.total=agentNames.length;
  preflightState.completed=0;
  preflightState.ready=0;
  preflightState.warn=0;
  preflightState.error=0;
  preflightState.disabled=0;
  preflightState.results={};
  const html='<div class="preflight-box">'
    +'<div class="muted" style="font-size:12px;line-height:1.5">'
    +'真实最小调用仅用于已确认安全非交互模式的执行者；UNKNOWN 表示尚未配置安全适配器，不代表不可用。<br>'
    +'TIMEOUT 可能是慢而非不可用（claude 冷启动常需 40–60s，已自动重试 1 次），请结合下方原始输出判断，必要时点一次“执行者自检”重试。'
    +'</div>'
    +'<div class="preflight-head">'
    +'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">'
    +'<div style="display:flex;align-items:center;gap:8px">'
    +'<span id="preflightTopSpinner" class="spinner"></span>'
    +'<strong id="preflightSummaryTitle">正在逐个自检执行者可用情况…</strong>'
    +'<span class="muted" id="preflightCounter" style="font-size:12px">0 / '+agentNames.length+'</span>'
    +'</div>'
    +'<button class="mini" id="preflightRerunAllBtn" onclick="runPreflight()" disabled>全部重新自检</button>'
    +'</div>'
    +'<div class="preflight-prog">'
    +'<div id="preflightProgressBar" class="preflight-prog-fill" style="width:0%"></div>'
    +'</div>'
    +'<div id="preflightStats" class="task-meta" style="margin-top:8px;display:flex;gap:12px;font-size:11.5px">'
    +'<span id="preflightStatReady" class="good-text" style="display:none">0 就绪</span>'
    +'<span id="preflightStatWarn" class="warn-text" style="display:none">0 警告</span>'
    +'<span id="preflightStatError" class="danger-text" style="display:none">0 异常</span>'
    +'<span id="preflightStatDisabled" class="muted" style="display:none">0 已禁用</span>'
    +'</div>'
    +'</div>'
    +'<div id="preflightRows" style="display:grid;gap:8px">'
    +agentItems.map(buildPreflightRowHtml).join('')
    +'</div>'
    +'</div>';
  openModal('执行者深度自检',html);
  agentNames.forEach((agentName,idx)=>{
    setTimeout(()=>{
      if(preflightRunId!==currentRunId)return;
      executeSinglePreflight(agentName,currentRunId);
    },idx*100);
  });
}
return {
  retrySinglePreflight, runPreflight
};
})({
  state, api, toast,
  esc, authHintLabel, openModal
});
