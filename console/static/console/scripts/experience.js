/* Experience Hub filtering, rendering and review/revoke actions. */
var {
  saveHubToken, applyHubFilter, renderShellExperience, reviewExperienceCandidate,
  revokeExperienceRule
} = (function({
  state, showPromptModal, api,
  pageRead, toast, esc,
  jsArg
}){

async function saveHubToken(){
  try{
    const inp=document.getElementById('hubTokenInput');
    const val=(inp&&inp.value||'').trim();
    if(val){
      await api('/api/experience/token',{method:'POST',body:JSON.stringify({token:val})});
      sessionStorage.setItem('herdr_hub_token',val);
      toast('Hub 身份凭据已持久化至受保护存储');
    }else{
      await api('/api/experience/token',{method:'POST',body:JSON.stringify({token:''})});
      sessionStorage.removeItem('herdr_hub_token');
      toast('Hub 身份凭据已清除');
    }
    renderShellExperience();
  }catch(e){
    toast('保存失败: '+(e.message||e),true);
  }
}

function applyHubFilter(){
  try{
    const inp=document.getElementById('hubCustomerInput');
    window._hubCustomerFilter=((inp&&inp.value)||'').trim();
  }catch(e){}
  renderShellExperience();
}

function formatExperienceContent(rawText){
  if(!rawText)return '';
  const lines=String(rawText).split('\n');
  let out=[];
  let tableLines=[];
  let listItems=[];

  function formatInline(str){
    let s=esc(str);
    s=s.replace(/`([^`]+)`/g,'<code class="exp-code">$1</code>');
    s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    return s;
  }

  function flushTable(){
    if(!tableLines.length)return;
    const rows=tableLines.map(l=>{
      let trimmed=l.trim();
      if(trimmed.startsWith('|'))trimmed=trimmed.slice(1);
      if(trimmed.endsWith('|'))trimmed=trimmed.slice(0,-1);
      return trimmed.split('|').map(c=>c.trim());
    });
    let headerRow=null;
    let dataRows=[];
    if(rows.length>=2&&rows[1].every(c=>/^:?-+:?$/.test(c))){
      headerRow=rows[0];
      dataRows=rows.slice(2);
    }else{
      dataRows=rows;
    }
    let thHtml='';
    if(headerRow){
      thHtml='<thead><tr>'+headerRow.map(c=>'<th>'+formatInline(c)+'</th>').join('')+'</tr></thead>';
    }
    let trHtml=dataRows.map(r=>{
      return '<tr>'+r.map((c,i)=>{
        let cls='';
        if(i===0&&headerRow&&/问题|形态|缺陷|陷阱/.test(headerRow[0]))cls=' class="exp-col-issue"';
        else if(i===1&&headerRow&&/教训|原因|分析|复盘/.test(headerRow[1]))cls=' class="exp-col-lesson"';
        else if(i===2&&headerRow&&/规范|对策|规程|准则|方案/.test(headerRow[2]))cls=' class="exp-col-solution"';
        return '<td'+cls+'>'+formatInline(c)+'</td>';
      }).join('')+'</tr>';
    }).join('');
    out.push('<div class="exp-table-wrapper"><table class="exp-table">'+thHtml+'<tbody>'+trHtml+'</tbody></table></div>');
    tableLines=[];
  }

  function flushList(){
    if(!listItems.length)return;
    let listHtml='<div class="exp-list">'+listItems.map((item,idx)=>{
      return '<div class="exp-list-item"><span class="exp-list-badge">'+(item.num||(idx+1))+'</span><div class="exp-list-body">'+formatInline(item.text)+'</div></div>';
    }).join('')+'</div>';
    out.push(listHtml);
    listItems=[];
  }

  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    const trimmed=line.trim();

    if(trimmed.startsWith('|')&&trimmed.endsWith('|')){
      flushList();
      tableLines.push(trimmed);
      continue;
    }else{
      flushTable();
    }

    const secMatch=trimmed.match(/^【(.*?)】$/);
    if(secMatch){
      flushList();
      const title=secMatch[1];
      let icon='📌';
      let cls='';
      if(/经验|教训|复盘|背景/.test(title)){
        icon='💡'; cls=' exp-section-lesson';
      }else if(/规范|操作|规程|准则/.test(title)){
        icon='📋'; cls=' exp-section-rule';
      }
      out.push('<div class="exp-section-header'+cls+'"><span class="exp-section-icon">'+icon+'</span><span class="exp-section-title">'+esc(title)+'</span></div>');
      continue;
    }

    const numMatch=trimmed.match(/^(\d+)[\.、]\s*(.*)$/);
    if(numMatch){
      listItems.push({num:numMatch[1],text:numMatch[2]});
      continue;
    }

    const bulletMatch=trimmed.match(/^[-*]\s+(.*)$/);
    if(bulletMatch){
      listItems.push({num:'•',text:bulletMatch[1]});
      continue;
    }

    flushList();
    if(!trimmed)continue;
    out.push('<p style="margin:8px 0 12px;line-height:1.6;">'+formatInline(trimmed)+'</p>');
  }

  flushTable();
  flushList();
  return out.join('');
}

async function renderShellExperience(){
  const sequence=state.experienceSequence=(state.experienceSequence||0)+1;
  const el=document.getElementById('tasks');if(!el)return;

  let saved='';
  try{saved=sessionStorage.getItem('herdr_hub_token')||'';}catch(e){}
  let hasServerToken=false;
  try{
    const tokenRes=await api('/api/experience/token');
    hasServerToken=Boolean(tokenRes&&(tokenRes.has_token||tokenRes.data?.has_token));
  }catch(e){}
  const custFilter=(window._hubCustomerFilter||'').trim();

  let authStatusHtml='';
  if(saved||hasServerToken){
    authStatusHtml='<span style="display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;color:#059669;background:#ecfdf5;padding:4px 8px;border-radius:4px;border:1px solid #a7f3d0;">✓ 已载入凭据</span>';
  }else{
    authStatusHtml='<span style="display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;color:#d97706;background:#fef3c7;padding:4px 8px;border-radius:4px;border:1px solid #fde68a;">! 需凭据</span>';
  }

  el.innerHTML=`<div class="exp-shell-wrap">
    <div class="exp-page-header">
      <div class="exp-header-top">
        <div class="exp-title-area">
          <h2>企业经验中心 <span class="exp-title-badge">EXPERIENCE HUB</span></h2>
          <p class="exp-subtitle">沉淀自真实生产任务闭环与实战复盘，经由审核固化为组织级业务规则与操作准则资产</p>
        </div>
        <div class="exp-toolbar-panel">
          <div class="exp-auth-group">
            <span style="font-size:12px;font-weight:600;color:#475569;">🔑 凭据:</span>
            ${authStatusHtml}
            <input id="hubTokenInput" class="exp-input" type="password" placeholder="${hasServerToken?'已绑定默认凭据 (可在此覆盖)':'粘贴 Hub 身份 Token'}" style="width:200px;" />
            <button class="exp-btn-base exp-btn-primary" onclick="saveHubToken()">保存凭据</button>
          </div>
          <div class="exp-group-divider"></div>
          <div class="exp-filter-group">
            <span style="font-size:12px;font-weight:600;color:#475569;">🏢 租户:</span>
            <input id="hubCustomerInput" class="exp-input" type="text" placeholder="全部 / 指定租户" oninput="window._hubCustomerFilter=this.value" style="width:130px;" />
            <button class="exp-btn-base exp-btn-secondary" onclick="applyHubFilter()">筛选</button>
          </div>
        </div>
      </div>
    </div>
    <div id="experienceHubContainer"><div class="loading" style="padding:48px 0;text-align:center;color:#64748b;">正在加载企业经验数据...</div></div>
  </div>`;

  try{const inp=document.getElementById('hubTokenInput');if(inp)inp.value=saved;}catch(e){}
  try{const cinp=document.getElementById('hubCustomerInput');if(cinp&&document.activeElement!==cinp)cinp.value=custFilter;}catch(e){}
  const container=document.getElementById('experienceHubContainer');

  try{
    const suffix=custFilter?'&customer='+encodeURIComponent(custFilter):'';
    const [candRes,ruleRes]=await Promise.all([
      pageRead('experience-candidates','/api/experience/candidates?status=pending_review'+suffix),
      pageRead('experience-rules','/api/experience/rules?scope=all'+suffix),
    ]);
    if(!candRes||!ruleRes||sequence!==state.experienceSequence||state.shellView!=='experience'||!container.isConnected)return;

    if(candRes.ok===false||ruleRes.ok===false){
      const err=candRes.error||ruleRes.error||'未授权或服务异常';
      container.innerHTML=`<div class="card" style="padding:24px;background:#ffffff;border:1px solid #fed7aa;border-left:4px solid #f59e0b;border-radius:8px;">
        <div style="color:#9a3412;font-size:15px;font-weight:700;margin-bottom:8px;">🔑 企业经验中心访问受限 / 需身份验证</div>
        <div style="color:#475569;font-size:13px;line-height:1.6;">${esc(err)}</div>
        <div style="margin-top:16px;font-size:12px;color:#64748b;line-height:1.6;">
          提示：企业经验中心严禁匿名访问。请在上方输入框粘贴个人或 Reviewer 的 Hub Token 后点击「保存凭据」。<br>
          （本地开发环境可在终端执行 <code>cat ~/.herdr-controller/credentials/experience-console-reviewer.token</code> 获取凭据）
        </div>
      </div>`;
      return;
    }

    const cands=(candRes&&(candRes.candidates||candRes.data?.candidates))||[];
    const rules=(ruleRes&&(ruleRes.rules||ruleRes.data?.rules))||[];
    let html='';

    // 1. Pending Candidates Section
    html+='<div class="exp-section">';
    html+=`<div class="exp-section-title-bar">
      <h3>⏳ 待审核业务规则候选 <span class="exp-count-badge exp-count-amber">${cands.length}</span></h3>
      <span style="font-size:12px;color:#64748b;">沉淀自真实任务闭环与实战教训，待审核通过后生效</span>
    </div>`;

    if(!cands.length){
      html+='<div class="empty" style="color:#94a3b8;font-size:13px;padding:24px;text-align:center;background:#f8fafc;border-radius:8px;border:1px dashed #e2e8f0;">当前暂无待审核规则候选</div>';
    }else{
      for(const c of cands){
        const cust=c.applies_to?.customer||'通用';
        const actor=c.contributor||'unknown';
        const tid=c.source?.task_id||'—';
        const refs=(c.source?.evidence_refs||[]).join(', ');
        html+=`<div class="exp-card exp-card-candidate">
          <div class="exp-card-header">
            <div>
              <div class="exp-card-title-row">
                <span class="exp-title-badge">待审核候选</span>
                <h4 class="exp-card-title">${esc(c.title)}</h4>
              </div>
              <div class="exp-meta-row">
                <span class="exp-chip">🏢 适用客户: <strong>${esc(cust)}</strong></span>
                <span class="exp-chip">👤 提炼者: <strong>${esc(actor)}</strong></span>
                <span class="exp-chip">🔗 来源任务: <code>${esc(tid)}</code></span>
                ${refs?`<span class="exp-chip" title="${esc(refs)}">📄 引用证据: <code>${esc(refs)}</code></span>`:''}
              </div>
            </div>
            <div class="exp-card-actions">
              <button class="exp-btn-base exp-btn-approve" onclick="reviewExperienceCandidate(${jsArg(c.experience_id)},'approve')">✓ 通过并发布</button>
              <button class="exp-btn-base exp-btn-reject" onclick="reviewExperienceCandidate(${jsArg(c.experience_id)},'reject')">✕ 驳回</button>
            </div>
          </div>
          <div class="exp-card-body">
            ${formatExperienceContent(c.content)}
          </div>
        </div>`;
      }
    }
    html+='</div>';

    // 2. Published Rules Section
    html+='<div class="exp-section">';
    html+=`<div class="exp-section-title-bar">
      <h3>🛡️ 已发布正式业务规则 <span class="exp-count-badge exp-count-emerald">${rules.length}</span></h3>
      <span style="font-size:12px;color:#64748b;">已全量生效并注入智能体调度与执行的权威业务规则资产</span>
    </div>`;

    if(!rules.length){
      html+='<div class="empty" style="color:#94a3b8;font-size:13px;padding:24px;text-align:center;background:#f8fafc;border-radius:8px;border:1px dashed #e2e8f0;">当前暂无已发布规则</div>';
    }else{
      for(const r of rules){
        const cust=r.applies_to?.customer||'通用';
        const actor=r.contributor||'unknown';
        const ver=r.version||1;
        const rev=r.review;
        let auditText='已发布生效';
        if(rev&&rev.reviewer){
          auditText=`审核人: ${esc(rev.reviewer)} · 审核结论: "${esc(rev.comment||'同意采纳')}"`;
        }
        html+=`<div class="exp-card exp-card-published">
          <div class="exp-card-header">
            <div>
              <div class="exp-card-title-row">
                <span class="exp-version-pill">v${ver} 权威规则</span>
                <h4 class="exp-card-title">${esc(r.title)}</h4>
              </div>
              <div class="exp-meta-row">
                <span class="exp-chip">🏢 适用客户: <strong>${esc(cust)}</strong></span>
                <span class="exp-chip">👤 提炼者: <strong>${esc(actor)}</strong></span>
                <span class="exp-chip">🆔 规则编号: <code>${esc(r.experience_id)}</code></span>
              </div>
            </div>
            <div class="exp-card-actions">
              <button class="exp-btn-base exp-btn-revoke" onclick="revokeExperienceRule(${jsArg(r.experience_id)})">撤回规则</button>
            </div>
          </div>
          <div class="exp-card-body">
            ${formatExperienceContent(r.content)}
          </div>
          <div class="exp-card-footer">
            <span>✓ ${auditText}</span>
            <span>更新于 ${r.updated_at?new Date(r.updated_at*1000).toLocaleString('zh-CN',{hour12:false}):'—'}</span>
          </div>
        </div>`;
      }
    }
    html+='</div>';

    container.innerHTML=html;
  }catch(e){
    if(sequence!==state.experienceSequence||!container.isConnected)return;
    const errStr=String(e.message||e);
    if(errStr.includes('401')||errStr.includes('unauthorized')){
      container.innerHTML=`<div class="card" style="padding:24px;background:#ffffff;border:1px solid #fed7aa;border-left:4px solid #f59e0b;border-radius:8px;">
        <div style="color:#9a3412;font-size:15px;font-weight:700;margin-bottom:8px;">🔑 企业经验中心需身份验证</div>
        <div style="color:#475569;font-size:13px;line-height:1.6;">${esc(errStr)}</div>
        <div style="margin-top:16px;font-size:12px;color:#64748b;line-height:1.6;">
          提示：请在页面顶部的「🔑 凭据」输入框中粘贴 Hub 身份 Token 后点击「保存凭据」。<br>
          （本地开发环境可在终端执行 <code>cat ~/.herdr-controller/credentials/experience-console-reviewer.token</code> 获取凭据）
        </div>
      </div>`;
    }else{
      container.innerHTML=`<div class="error" style="color:var(--danger);padding:16px;">企业经验中心读取异常: ${esc(errStr)}</div>`;
    }
  }
}
async function reviewExperienceCandidate(expId,action){
  showPromptModal({
    title: action==='approve'?'通过并发布业务规则':'驳回规则候选',
    label: action==='approve'?'审核通过批注（可选）':'驳回理由',
    defaultValue: '',
    confirmText: action==='approve'?'确认发布':'确认驳回',
    onConfirm: async(comment)=>{
      try{
        await api('/api/experience/review',{method:'POST',body:JSON.stringify({experience_id:expId,action:action,comment:comment||''})});
        toast(action==='approve'?'已通过并发布业务规则':'已驳回规则候选');
        renderShellExperience();
      }catch(e){
        toast('操作失败: '+(e.message||e),true);
      }
    }
  });
}
async function revokeExperienceRule(expId){
  showPromptModal({
    title: '撤回业务规则',
    label: '撤回原因',
    defaultValue: '',
    confirmText: '确认撤回',
    onConfirm: async(reason)=>{
      if(!reason)return toast('撤回原因不能为空',true);
      try{
        await api('/api/experience/revoke',{method:'POST',body:JSON.stringify({experience_id:expId,reason:reason})});
        toast('已撤回该业务规则');
        renderShellExperience();
      }catch(e){
        toast('撤回失败: '+(e.message||e),true);
      }
    }
  });
}
return {
  saveHubToken, applyHubFilter, renderShellExperience, reviewExperienceCandidate,
  revokeExperienceRule
};
})({
  state, showPromptModal, api,
  pageRead, toast, esc,
  jsArg
});
