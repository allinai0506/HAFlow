/* 任务抽屉里最近一条已投递指令。完整正文留在本机 reveal。 */
function renderPromptLedger(proj){
  const card=(proj||{}).prompt_ledger;
  if(!card)return '';
  let ledger='<div class="td-sec"><div class="td-lbl">发出的指令</div>';
  if(card.available===false){
    ledger+=`<div class="empty" style="padding:8px 0">${esc(card.error||'指令记录暂时无法读取')}</div></div>`;
    return ledger;
  }
  const latest=card.latest;
  if(!latest){
    ledger+='<div class="empty" style="padding:8px 0">这次任务还没有投递记录</div></div>';
    return ledger;
  }
  const sha=String(latest.prompt_sha256||'').slice(0,12);
  const status=latest.status_label||latest.dispatch_status||'';
  ledger+=`<div class="td-txt">${esc(status)} · ${esc(latest.agent_name||'UNKNOWN')} · ${esc(sha)}</div>`;
  if(latest.content_purged)ledger+='<div class="td-ev-detail">正文已按保留期清除，哈希仍在。</div>';
  else if(latest.prompt_excerpt)ledger+=`<div class="td-ev-detail">${esc(latest.prompt_excerpt)}</div>`;
  ledger+='<div class="td-ev-detail">不表示模型已经收到或理解。</div>';
  const compare=card.compare;
  if(compare&&compare.left_id&&compare.right_id){
    const relation=compare.equal===true?'与上一轮相同':(compare.equal===false?'与上一轮不同':'与上一轮无法比较');
    const cmd='bin/herdr-task prompt-ledger diff --left '+compare.left_id+' --right '+compare.right_id+' --reveal';
    ledger+=`<div class="td-ev-detail">${esc(relation)}</div><div class="td-ev-detail" style="font-family:ui-monospace,Menlo,monospace">${esc(cmd)}</div>`;
  }
  ledger+='</div>';
  return ledger;
}
