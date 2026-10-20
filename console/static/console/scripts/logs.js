/* Log reads, export and polling; callers stop polling through stopLogAutoRefresh. */
var {
  showLogs, switchLogKind, switchLogLineCount, refreshLogsTab,
  copyCurrentLog, exportLogsFile, toggleLogAutoRefresh, stopLogAutoRefresh
} = (function({
  state, pageRead, toast,
  renderWorkflowTabs, paintCrumb, setWorkspaceMode
}){

let logState = {
  kind: 'controller',
  lines: 200,
  autoRefreshTimer: null
};

function renderLogsTabSkeleton(){
  return `<div class="ctl-panel-header">`+
    `<div style="display:flex;justify-content:space-between;align-items:center;width:100%;flex-wrap:wrap;gap:8px">`+
      `<div><strong>调度审计日志</strong><span class="muted" style="margin-left:8px;font-weight:400">· 协调器事件溯源、主循环轮询流与各工位物理日志</span></div>`+
      `<div style="display:flex;gap:8px;align-items:center">`+
        `<button class="btn" onclick="refreshLogsTab()" title="刷新日志">🔄 刷新</button>`+
        `<button class="btn" onclick="copyCurrentLog()" title="复制日志到剪贴板">📋 复制</button>`+
        `<button class="btn" onclick="exportLogsFile()" title="导出日志文件">⬇️ 导出</button>`+
      `</div>`+
    `</div>`+
  `</div>`+
  `<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px">`+
    `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">`+
      `<div class="log-kind-selector">`+
        `<button class="btn mini log-kind-btn ${logState.kind==='controller'?'active':''}" id="logKindController" onclick="switchLogKind('controller')">协调器 (Controller)</button>`+
        `<button class="btn mini log-kind-btn ${logState.kind==='controller_err'?'active':''}" id="logKindControllerErr" onclick="switchLogKind('controller_err')">错误流 (Error)</button>`+
        `<button class="btn mini log-kind-btn ${logState.kind==='sentinel'?'active':''}" id="logKindSentinel" onclick="switchLogKind('sentinel')">巡检守卫 (Sentinel)</button>`+
        `<button class="btn mini log-kind-btn ${logState.kind==='notifier'?'active':''}" id="logKindNotifier" onclick="switchLogKind('notifier')">通知服务 (Notifier)</button>`+
      `</div>`+
      `<select id="logLineCount" onchange="switchLogLineCount(this.value)" style="padding:4px 8px;font-size:12px;border:1px solid #e6e8ee;border-radius:6px;background:#ffffff">`+
        `<option value="100"${logState.lines===100?' selected':''}>最近 100 行</option>`+
        `<option value="200"${logState.lines===200?' selected':''}>最近 200 行</option>`+
        `<option value="500"${logState.lines===500?' selected':''}>最近 500 行</option>`+
        `<option value="1000"${logState.lines===1000?' selected':''}>最近 1000 行</option>`+
      `</select>`+
    `</div>`+
    `<div style="display:flex;gap:16px;align-items:center">`+
      `<label style="display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#5f6368;cursor:pointer;user-select:none">`+
        `<input type="checkbox" id="logAutoScroll" checked style="margin:0">`+
        `<span>自动滚屏</span>`+
      `</label>`+
      `<label style="display:inline-flex;align-items:center;gap:4px;font-size:12px;color:#5f6368;cursor:pointer;user-select:none">`+
        `<input type="checkbox" id="logAutoRefresh" onchange="toggleLogAutoRefresh(this.checked)" style="margin:0">`+
        `<span>自动刷新 (3s)</span>`+
      `</label>`+
    `</div>`+
  `</div>`+
  `<div class="cockpit-stat-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:8px">`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">当前日志流</div>`+
      `<div id="logStatKind" style="font-size:14px;font-weight:600;color:#121316;margin-top:4px">Controller 主循环</div>`+
    `</div>`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">日志行数</div>`+
      `<div id="logStatLines" style="font-size:14px;font-weight:600;color:#121316;margin-top:4px;font-variant-numeric:tabular-nums">0 行</div>`+
    `</div>`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">持久化模式</div>`+
      `<div style="font-size:14px;font-weight:600;color:#5e6ad2;margin-top:4px">WAL 严格追加</div>`+
    `</div>`+
    `<div style="background:#ffffff;border:1px solid #e6e8ee;border-radius:8px;padding:12px 16px;box-shadow:0 1px 2px rgba(18,19,22,0.04)">`+
      `<div style="font-size:11px;color:#5f6368;font-weight:500">更新时间</div>`+
      `<div id="logStatUpdated" style="font-size:14px;font-weight:600;color:#047857;margin-top:4px;font-variant-numeric:tabular-nums">—</div>`+
    `</div>`+
  `</div>`+
  `<div class="log-stream-wrap">`+
    `<pre id="logContentPre" class="log-content-pre">正在拉取调度审计日志…</pre>`+
  `</div>`;
}

async function showLogs(kind){
  if(kind)logState.kind=kind;
  const logEl=document.getElementById('logsTabView');
  setWorkspaceMode('logs');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__logs__'))state.openWorkflowTabIds.push('__logs__');
  state.workflowId='__logs__';
  renderWorkflowTabs();
  paintCrumb();
  if(logEl){
    if(!document.getElementById('logContentPre')){
      logEl.innerHTML=renderLogsTabSkeleton();
    }
    logEl.hidden=false;
  }
  await loadLogContent();
}

async function loadLogContent(isAuto){
  const sequence=logState.sequence=(logState.sequence||0)+1;
  const kind=logState.kind,lines=logState.lines;
  const pre=document.getElementById('logContentPre');
  const statLines=document.getElementById('logStatLines');
  const statUpdated=document.getElementById('logStatUpdated');
  const statKind=document.getElementById('logStatKind');
  if(!pre)return;
  if(!isAuto&&(!pre.textContent||!pre.textContent.trim())){
    pre.textContent='正在拉取调度审计日志…';
  }
  try{
    const d=await pageRead('logs','/api/logs?kind='+encodeURIComponent(kind)+'&n='+encodeURIComponent(lines));
    if(!d||sequence!==logState.sequence||!pre.isConnected)return;
    const output=(d&&d.output)||'（暂无日志输出）';
    pre.textContent=output;
    const lineCount=output.trim()?output.split('\n').length:0;
    if(statLines)statLines.textContent=lineCount+' 行';
    if(statUpdated){
      const now=new Date();
      statUpdated.textContent=now.toTimeString().split(' ')[0];
    }
    const kindNames={
      controller:'Controller 主循环',
      controller_err:'Error 异常流',
      sentinel:'Sentinel 巡检',
      notifier:'Notifier 通知'
    };
    if(statKind)statKind.textContent=kindNames[logState.kind]||logState.kind;
    const autoScrollEl=document.getElementById('logAutoScroll');
    if(autoScrollEl&&autoScrollEl.checked){
      pre.scrollTop=pre.scrollHeight;
    }
  }catch(e){
    if(sequence!==logState.sequence||!pre.isConnected)return;
    if(!isAuto)toast(e.message,true);
    if(pre)pre.textContent='拉取日志失败: '+e.message;
  }
}

function switchLogKind(kind){
  logState.kind=kind;
  ['controller','controller_err','sentinel','notifier'].forEach(k=>{
    const btn=document.getElementById('logKind'+(k==='controller'?'Controller':k==='controller_err'?'ControllerErr':k==='sentinel'?'Sentinel':'Notifier'));
    if(btn)btn.classList.toggle('active',k===kind);
  });
  loadLogContent();
}

function switchLogLineCount(val){
  logState.lines=parseInt(val,10)||200;
  loadLogContent();
}

function refreshLogsTab(){
  loadLogContent();
  toast('日志已刷新');
}

function copyCurrentLog(){
  const pre=document.getElementById('logContentPre');
  if(!pre||!pre.textContent){toast('暂无日志内容可复制',true);return;}
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(pre.textContent).then(()=>{toast('已复制日志到剪贴板！');}).catch(()=>{toast('复制失败',true);});
  }else{
    const ta=document.createElement('textarea');
    ta.value=pre.textContent;
    document.body.appendChild(ta);
    ta.select();
    try{document.execCommand('copy');toast('已复制日志到剪贴板！');}catch(e){toast('复制失败',true);}
    document.body.removeChild(ta);
  }
}

function exportLogsFile(){
  const pre=document.getElementById('logContentPre');
  if(!pre||!pre.textContent){toast('暂无日志内容可导出',true);return;}
  const blob=new Blob([pre.textContent],{type:'text/plain;charset=utf-8'});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;
  a.download=`haflow-${logState.kind}-${Date.now()}.log`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  toast('已导出日志文件');
}

function toggleLogAutoRefresh(enabled){
  if(logState.autoRefreshTimer){
    clearInterval(logState.autoRefreshTimer);
    logState.autoRefreshTimer=null;
  }
  if(enabled){
    logState.autoRefreshTimer=setInterval(()=>{
      if(state.workflowId==='__logs__'&&!document.hidden){
        loadLogContent(true);
      }
    },3000);
    toast('已开启 3s 自动刷新');
  }else{
    toast('已关闭自动刷新');
  }
}
function stopLogAutoRefresh(){
  if(logState.autoRefreshTimer){clearInterval(logState.autoRefreshTimer);logState.autoRefreshTimer=null;}
}

return {
  showLogs, switchLogKind, switchLogLineCount, refreshLogsTab,
  copyCurrentLog, exportLogsFile, toggleLogAutoRefresh, stopLogAutoRefresh
};
})({
  state, pageRead, toast,
  renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args), paintCrumb, setWorkspaceMode
});
