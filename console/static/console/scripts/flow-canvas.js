/* DAG layout, selection, inspector rendering and graph lifecycle. */
var {
  flowCardFoot, flowNodeStatus, flowOverviewHtml, setWorkspaceMode,
  switchWorkflowView, switchFlowInspectorTab, destroyFlowGraph, resizeFlowGraph,
  fitFlowGraph, flowZoomIn, flowZoomOut, renderFlowWorkbench,
  renderFlowGraph, flowNodeById, flowTasksForNode, renderNodeInspector
} = (function({
  state, esc, humanNodeStatus,
  cleanStageLabel, taskBadge, deliveryWaitHtml,
  deliveryPipelineLine, renderTasks, renderTaskRow,
  workflowDeliverables, deliverableNoteHtml, deliverableRowHtml,
  renderDeliverables, openTaskDrawer, currentRecovery,
  dispatchConfirmationHtml, recoveryUnavailable, recoveryCard
}){

function flowIconSvg(id){
  const paths={
    requirements:'<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/>',
    plan:'<path d="M4 6h16M4 12h10M4 18h16"/>',
    implementation:'<path d="M14 4l6 6-10 10H4v-6L14 4z"/>',
    review:'<path d="M5 13l4 4L19 7"/>',
    test:'<path d="M9 3h6M10 3v6l-5 9a4 4 0 0 0 3.5 6h7A4 4 0 0 0 19 18l-5-9V3"/>',
    wrapup:'<path d="M4 7h16v12H4zM8 7V5h8v2"/>'
  };
  const p=paths[id]||paths.requirements;
  return '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'+p+'</svg>';
}
function startupNodeLabel(state){
  return ({startup_pending:'启动中（尚未就绪）',startup_failed:'启动失败',abandoned:'已放弃'})[state]||String(state||'');
}
function flowCardFoot(n){
  if(n.workflow_startup_state)return startupNodeLabel(n.workflow_startup_state);
  const u=n.resource_usage;
  if(u)return '累计任务 '+u.task_count+' / '+(u.max_tasks_per_node??'未设')+' · 运行 '+u.active_task_count+' / '+(u.max_concurrency??'未设')+' · 工位引用 '+u.pane_count+(u.overflow?' · 超限':'');
  if(!n.task_count)return (n.status === 'completed' || n.status === 'cleaned') ? '已完成' : '尚未开始';
  const head=(Number(n.active_task_count)||0)+' 运行 · 完成 '+(Number(n.completed_task_count)||0);
  const agents=(n.agents||[]).filter(Boolean).slice(0,2);
  return agents.length?head+' · '+agents.join(' · '):head;
}
function flowNodeStatus(node,fallback){
  if(node.workflow_startup_state)return startupNodeLabel(node.workflow_startup_state);
  if(recoveryUnavailable())return '状态未确认';
  if(node.status==='completed'||node.status==='cleaned'||fallback==='已完成')return fallback;
  const op=currentRecovery(node.id)[0];
  return op?({waiting_human:'待人工确认',awaiting_result:'核验派发',waiting:'已暂缓',pending:'等待派发',running:'正在派发'}[op.status]||'需恢复'):fallback;
}
function flowCardInner(n){
  const status=n.status||'waiting';
  const kind=String(n.node_type||'agent').toUpperCase();
  const leftAnchor=n.has_upstream!==false?('<div class="node-anchor left'+(n.inbound_active?' inbound-active':'')+'"></div>'):'';
  const rightAnchor=n.has_downstream!==false?('<div class="node-anchor right'+(n.outbound_active?' outbound-active':'')+'"></div>'):'';
  return leftAnchor+rightAnchor
    +'<div class="fn-top"><span class="fn-ico">'+flowIconSvg(n.id)+'</span><span class="fn-kind">'+esc(kind)+'</span><span class="fn-pill">'+esc(flowNodeStatus(n,humanNodeStatus(status)))+'</span></div>'
    +'<div class="fn-title">'+esc(cleanStageLabel(n.label||n.id||''))+'</div>'
    +'<div class="fn-purpose">'+esc(flowPlain(n.purpose))+'</div>'
    +'<div class="fn-foot"'+(n.resource_usage?.overflow?' style="color:var(--danger);font-weight:600"':'')+'>'+esc(flowCardFoot(n))+'</div>';
}
function ensureFlowCardShape(){
  if(state.flowHtmlReady)return true;
  if(!window.X6||!X6.Shape||!X6.Shape.HTML||!X6.Shape.HTML.register)return false;
  X6.Shape.HTML.register({
    shape:'flow-card',width:232,height:134,effect:['data'],
    html(cell){
      const n=(cell&&cell.getData)?(cell.getData()||{}):{};
      const wrap=document.createElement('div');
      wrap.className='flow-node-card is-'+(n.status||'waiting')+(n.selected?' is-selected':'');
      wrap.innerHTML=flowCardInner(n);
      return wrap;
    }
  });
  state.flowHtmlReady=true;
  return true;
}
function flowPlain(s){return String(s||'').replace(/\s+/g,' ').trim();}
function flowReads(node){
  const purpose=flowPlain(node.purpose)||'该节点暂无目的说明';
  const agents=(node.agents||[]).filter(Boolean);
  const active=Number(node.active_task_count||0);
  let tail;
  if(!agents.length)tail='尚未分配执行者。';
  else if(active)tail='当前由 '+agents.join('、')+' 执行，'+active+' 个任务运行中。';
  else tail='当前由 '+agents.join('、')+' 执行。';
  const sep=/[。！？.!?]$/.test(purpose)?'':'。';
  return purpose+sep+tail;
}
function flowChecklist(node){
  const map={};(flowGraphData().nodes||[]).forEach(n=>{map[n.id]=n;});
  const labelOf=id=>cleanStageLabel((map[id]&&map[id].label)||id);
  const items=[];
  const deps=node.depends_on||[];
  if(!deps.length)items.push(['ok','起点节点，无上游依赖']);
  else{
    const waiting=deps.filter(id=>!map[id]||map[id].status!=='completed');
    if(!waiting.length)items.push(['ok','上游依赖已满足']);
    else items.push(['wait','等待上游 · '+waiting.map(labelOf).join('、')]);
  }
  const issueLabels={required_task_missing:'必需任务缺失',required_task_out_of_workflow:'必需任务属于其他工作流',required_task_out_of_node:'必需任务属于其他节点',required_task_lineage_cycle:'任务替代关系存在循环',required_task_ids_invalid:'必需任务配置无效',workflow_config_invalid:'工作流配置无效',required_task_incomplete:'必需任务尚未完成'};
  (node.completion_issues||[]).forEach(issue=>items.push(['bad',(issueLabels[issue.reason]||issue.reason)+(issue.task_id?' · '+issue.task_id:'')]));
  const agents=(node.agents||[]).filter(Boolean);
  if(node.workflow_startup_state){
    items.push([node.workflow_startup_state==='startup_pending'?'wait':'bad',startupNodeLabel(node.workflow_startup_state)+' · 工作流未就绪']);
  }
  else if(!agents.length){
    if(node.status==='completed')items.push(['ok','阶段已收尾完成']);
    else items.push(['wait','尚未分配执行者']);
  }
  else items.push(['ok','执行者已分配 · '+agents.join('、')]);
  const failed=Number(node.failed_task_count||0), blocked=Number(node.blocked_task_count||0);
  if(failed||blocked){
    if(node.status==='completed')items.push(['ok','历史失败/阻塞已处理放行（历史失败 '+failed+' · 阻塞 '+blocked+'）']);
    else items.push(['bad','失败 '+failed+' · 阻塞 '+blocked]);
  }
  else if(node.status==='blocked'||(node.completion_issues||[]).length)items.push(['bad','节点仍有未解除的阻塞']);
  else if(recoveryUnavailable())items.push(['bad','恢复状态读取失败，卡点未确认']);
  else if(currentRecovery(node.id).length)items.push(['bad','存在派发恢复待办，需要核查或人工确认']);
  else items.push(['ok','无失败、无阻塞']);
  const down=node.downstream||[];
  if(!down.length)items.push(['ok','终点节点']);
  else items.push(['ok','下游 · '+down.map(labelOf).join('、')]);
  return items;
}
function flowOverviewHtml(node, tasks){
  const checks=flowChecklist(node).map(item=>{
    const kind=item[0];
    const mark=kind==='bad'?'!':kind==='wait'?'○':'✓';
    return '<div class="flow-check"><span class="flow-mark '+kind+'">'+mark+'</span><span>'+esc(item[1])+'</span></div>';
  }).join('');
  const live=(tasks||[]).filter(t=>['cleaned','completed','committed','integrated','superseded'].indexOf(t.status)<0).slice(0,6);
  const rows=live.length?live.map(renderTaskRow).join(''):'<div class="empty">暂无进行中的任务</div>';
  const nodePacked=workflowDeliverables();
  const nodeItems=(nodePacked.items||[]).filter(item=>String(item.node_id)===String(node.id));
  const nodeNotice=deliverableNoteHtml(nodePacked);
  const nodeRows=nodePacked.pending?'<div class="empty">正在读取本节点成果…</div>':nodeItems.length?nodeNotice+nodeItems.map(deliverableRowHtml).join(''):nodeNotice||'<div class="empty">本节点还没有成果</div>';
  return dispatchConfirmationHtml(node.id)+currentRecovery(node.id).map(o=>'<div class="flow-node-card"><strong>'+esc(o.recovery?.summary||o.detail?.decision_needed||'恢复待办')+'</strong><button class="mini" onclick="openControllerCockpit('+esc(JSON.stringify({workflowId:state.workflowId,nodeId:node.id,operationId:o.id}))+')">在 Controller 中处理</button></div>').join('')+'<div class="flow-reads"><em>读作</em>'+esc(flowReads(node))+'</div><div class="flow-sec">本节点成果</div><div class="muted">执行任务：'+(tasks||[]).length+' 个</div>'+nodeRows+'<div class="flow-sec">本节点现状</div>'+checks+'<div class="flow-sec">当前任务</div>'+rows;
}

function flowGraphData(){return (state.workflow&&state.workflow.graph)||{nodes:[],edges:[]};}
function initFlowGraph(){renderFlowGraph();}
function pickDefaultFlowNodeId(nodes){if(!nodes||!nodes.length)return null;const order=['blocked','failed','working','rework'];for(const st of order){const hit=nodes.find(n=>n.status===st);if(hit)return hit.id;}const completed=new Set(nodes.filter(n=>n.status==='completed').map(n=>n.id));const waitingNext=nodes.find(n=>n.status==='waiting'&&(n.depends_on||[]).every(d=>completed.has(d)));if(waitingNext)return waitingNext.id;const waiting=nodes.find(n=>n.status==='waiting');if(waiting)return waiting.id;return nodes[0].id;}
function setWorkspaceMode(mode){const fw=document.getElementById('flowWrap');const tl=document.getElementById('tasks');const st=document.getElementById('stages');const fs=document.getElementById('flowSummary');const ab=document.getElementById('flowAttnBar');const ctlP=document.getElementById('controllerTabView');const tplP=document.getElementById('templatesTabView');const arcP=document.getElementById('archiveTabView');const logP=document.getElementById('logsTabView');const shell=document.querySelector('.shell');if(shell)shell.dataset.workspace=mode||'flow';const isSys=(mode==='aux'||mode==='ctl'||mode==='templates'||mode==='archive'||mode==='logs');const bar=document.getElementById('canvasToolbar');if(bar)bar.style.display=isSys?'none':'flex';const tg=document.querySelector('.flow-view-toggle');if(tg)tg.style.display=isSys?'none':'inline-flex';const tf=document.querySelector('.task-filters');if(tf)tf.style.display=(isSys||mode!=='list')?'none':'flex';if(fw)fw.style.display=mode==='flow'?'flex':'none';if(fs)fs.style.display=(mode==='flow')?'block':'none';if(ab&&isSys)ab.style.display='none';if(st)st.style.display='none';if(tl)tl.style.display=(mode==='flow'||mode==='ctl'||mode==='templates'||mode==='archive'||mode==='logs')?'none':'block';if(ctlP)ctlP.hidden=(mode!=='ctl');if(tplP)tplP.hidden=(mode!=='templates');if(arcP)arcP.hidden=(mode!=='archive');if(logP)logP.hidden=(mode!=='logs');if(typeof renderDeliverables==='function')renderDeliverables();}
function switchWorkflowView(v){state.workflowView=(v==='list'?'list':'flow');const bf=document.getElementById('viewFlowBtn');if(bf)bf.classList.toggle('active',state.workflowView==='flow');const bl=document.getElementById('viewListBtn');if(bl)bl.classList.toggle('active',state.workflowView==='list');setWorkspaceMode(state.workflowView);if(state.workflowView==='flow')requestAnimationFrame(()=>{resizeFlowGraph();fitFlowGraph();});else renderTasks();}
function switchFlowInspectorTab(t){state.flowInspectorTab=t;['Summary','Tasks','Context','Runtime'].forEach(k=>{const el=document.getElementById('flowTab'+k);if(el)el.classList.toggle('active',k.toLowerCase()===t);});renderNodeInspector();}
function selectFlowNode(id){
  state.flowSelectedNodeId=id;
  updateFlowSelection();
  renderNodeInspector();
  const tasks=flowTasksForNode(id).filter(t=>t.task_id);
  const current=tasks.filter(t=>t.status!=='superseded'&&!t.superseded_by);
  const task=current[current.length-1]||tasks[tasks.length-1];
  if(task)openTaskDrawer(task.task_id);
}
function updateFlowSelection(){try{if(!state.flowGraph||!state.flowGraph.getCellById)return;const g=flowGraphData();(g.nodes||[]).forEach(n=>{try{const cell=state.flowGraph.getCellById(n.id);if(!cell||!cell.getData||!cell.setData)return;const sel=String(state.flowSelectedNodeId||'')===String(n.id);const data=cell.getData()||{};if(!!data.selected===sel)return;cell.setData(Object.assign({},data,{selected:sel}));}catch(err){}});}catch(e){}}
function destroyFlowGraph(){try{if(state.flowGraph&&state.flowGraph.dispose)state.flowGraph.dispose();}catch(e){}state.flowGraph=null;state.flowGraphWfId=null;state.flowGraphProjectionKey=null;const c=document.getElementById('flowCanvas');if(c)c.innerHTML='';}
function resizeFlowGraph(){try{const cv=document.getElementById('flowCanvas');const host=document.getElementById('flowCanvasWrap')||cv;if(!cv||!host||!state.flowGraph)return;const w=host.clientWidth||cv.clientWidth||800;const h=Math.max(280,cv.clientHeight||host.clientHeight||480);if(state.flowGraph.resize)state.flowGraph.resize(w,h);}catch(e){}}
function fitFlowGraph(){try{if(!state.flowGraph)return;if(state.flowGraph.zoomToFit){state.flowGraph.zoomToFit({padding:24,maxScale:1});if(state.flowGraph.centerContent)state.flowGraph.centerContent();}else if(state.flowGraph.centerContent)state.flowGraph.centerContent();}catch(e){}}
function flowZoomIn(){try{if(state.flowGraph&&state.flowGraph.zoom)state.flowGraph.zoom(0.15);}catch(e){}}
function flowZoomOut(){try{if(state.flowGraph&&state.flowGraph.zoom)state.flowGraph.zoom(-0.15);}catch(e){}}
function renderFlowWorkbench(){
  if(state.shellView&&state.shellView!=='workbench')return;
  if(state.opsMode||state.dashMode||state.workflowId==='__templates__')return;
  const g=flowGraphData();const nodes=g.nodes||[];
  const wf=state.workflow&&state.workflow.workflow;
  const wfKey=wf?workflowStatusKey(wf):'';
  const startup=wfKey==='startup_pending'||wfKey==='startup_failed'||wfKey==='abandoned';
  setWorkspaceMode(state.workflowView);
  const fs=document.getElementById('flowSummary');
  if(fs){
    if(state.workflowView==='flow'&&(nodes.length||startup)){
      const working=nodes.filter(n=>n.status==='working').length;
      fs.style.display='inline-flex';
      const dotHtml=working>0?'<span class="flow-status-dot" style="background:#5e6ad2;box-shadow:0 0 0 2px rgba(94,106,210,.25)"></span>':'<span class="flow-status-dot"></span>';
      const label=wf?esc(workflowStatusLabel(wf)):'';
      fs.innerHTML=dotHtml+'<span>'+(label?label+' · ':'')+'<b>'+nodes.length+'</b> 个节点 · <b>'+working+'</b> 个运行中</span>';
    }else{
      fs.style.display='none';
    }
  }
  const bf=document.getElementById('viewFlowBtn');if(bf)bf.classList.toggle('active',state.workflowView!=='list');
  const bl=document.getElementById('viewListBtn');if(bl)bl.classList.toggle('active',state.workflowView==='list');
  if(!state.flowSelectedNodeId||!nodes.some(n=>n.id===state.flowSelectedNodeId)){state.flowSelectedNodeId=pickDefaultFlowNodeId(nodes);}
  const recoveries=currentRecovery();
  const ticker=document.getElementById('bottomTicker');
  if(ticker){
    const hint=wf?workflowStartupHint(wf):'';
    ticker.textContent=startup&&hint?hint:startup?workflowStatusLabel(wf):state.controllerActionsData?.pending?'正在读取恢复状态…':recoveryUnavailable()?'恢复状态读取失败，卡点未确认':recoveries.length?'有 '+recoveries.length+' 项恢复待办，请查看卡点':'工作流调度就绪';
  }
  const attnBar=document.getElementById('flowAttnBar');
  if(attnBar){
    if(state.workflowView==='flow'&&state.controllerActionsData?.pending){
      attnBar.style.display='flex';
      attnBar.innerHTML='<div class="flow-attn-text" role="status">正在读取恢复状态…</div>';
    }else if(state.workflowView==='flow'&&recoveryUnavailable()){
      attnBar.style.display='flex';
      attnBar.innerHTML=`<div class="flow-attn-text" role="status"><span>⚠ 恢复状态读取失败，卡点未确认</span></div><button class="mini" onclick="loadWorkflow(state.workflow.workflow.workflow_id)">刷新恢复状态</button>`;
    }else if(state.workflowView==='flow'&&recoveries.length){
      attnBar.style.display='flex';
      attnBar.innerHTML=`<div class="flow-attn-text" role="status"><span>⚠ ${recoveries.length} 项恢复待办：${esc(recoveries.map(o=>cleanStageLabel(o.recovery?.node_label||o.payload?.node_id||'')+' · '+(o.recovery?.summary||'需处理')).join('；'))}</span></div><button class="mini" onclick="openControllerCockpit(${esc(JSON.stringify({workflowId:state.workflowId,operationId:recoveries[0]?.id}))})">在 Controller 中处理</button>`;
    }else{
      attnBar.style.display='none';
      attnBar.innerHTML='';
    }
  }
  renderNodeInspector();
  renderFlowGraph();
}
function dagreLayoutPositions(nodes,edges){const g=new window.dagre.graphlib.Graph();g.setGraph({rankdir:'LR',ranksep:56,nodesep:36,marginx:32,marginy:32});g.setDefaultEdgeLabel(()=>({}));const W=232,H=134;nodes.forEach(n=>g.setNode(n.id,{width:W,height:H}));edges.forEach(e=>{try{g.setEdge(e.from,e.to);}catch(err){}});window.dagre.layout(g);const pos={};nodes.forEach(n=>{const p=g.node(n.id);if(p)pos[n.id]={x:p.x-W/2,y:p.y-H/2};});return pos;}
function renderFlowGraph(){
  const wrap=document.getElementById('flowCanvas');
  const errBox=document.getElementById('flowError');
  if(errBox){errBox.style.display='none';errBox.textContent='';}
  if(!wrap)return;
  const g=flowGraphData();
  const nodes=g.nodes||[];
  const edges=g.edges||[];
  if(!nodes.length){
    destroyFlowGraph();
    if(errBox){errBox.style.display='block';errBox.textContent='当前工作流暂无可用节点定义（legacy 或空定义，已 fail-soft，不伪造连线）。';}
    return;
  }
  const projectionKey=JSON.stringify(g);
  if(state.flowGraph&&state.flowGraphWfId===state.workflowId&&state.flowGraphProjectionKey===projectionKey){
    updateFlowSelection();
    return;
  }
  if(typeof window.dagre==='undefined'||!window.dagre.graphlib||!window.dagre.layout){
    destroyFlowGraph();
    if(errBox){errBox.style.display='block';errBox.textContent='本地 Dagre 资源缺失（/static/vendor/dagre-3.1.1.min.js），请检查 Console 静态资源。页面其他功能不受影响。';}
    return;
  }
  if(typeof window.X6==='undefined'||!window.X6.Graph){
    destroyFlowGraph();
    if(errBox){errBox.style.display='block';errBox.textContent='本地 X6 资源缺失（/static/vendor/x6-3.1.8.min.js），请检查 Console 静态资源。页面其他功能不受影响。';}
    return;
  }
  if(!ensureFlowCardShape()){
    destroyFlowGraph();
    if(errBox){errBox.style.display='block';errBox.textContent='本地 X6 缺少 HTML 节点（Shape.HTML），无法绘制流程卡片。';}
    return;
  }
  let pos={};
  try{pos=dagreLayoutPositions(nodes,edges);}catch(e){
    destroyFlowGraph();
    if(errBox){errBox.style.display='block';errBox.textContent='Dagre 布局失败：'+e.message;}
    return;
  }
  try{
    destroyFlowGraph();
    const W=wrap.clientWidth||800;
    const H=480;
    const graph=new window.X6.Graph({
      container:wrap,width:W,height:H,background:{color:'transparent'},
      panning:{enabled:true},
      mousewheel:{enabled:true,modifiers:['ctrl','meta'],minScale:0.45,maxScale:2.0},
      interacting:{nodeMovable:false,edgeMovable:false,edgeLabelMovable:false,arrowheadMovable:false,vertexMovable:false,vertexAddable:false,vertexDeletable:false,edgeAddable:false},
      connecting:{allowBlank:false,allowLoop:false,allowNode:false,allowEdge:false,snap:true},
      highlighting:{magnetAdsorbed:{name:'stroke',args:{padding:4,attrs:{stroke:'#5e6ad2','stroke-width':2}}}}
    });
    state.flowGraph=graph;
    state.flowGraphWfId=state.workflowId;
    state.flowGraphProjectionKey=projectionKey;
    const nodesMap={};
    nodes.forEach(n=>{nodesMap[n.id]=n;});
    const activeIncomingNodes=new Set();
    const activeOutgoingNodes=new Set();
    edges.forEach(e=>{
      const fromN=nodesMap[e.from];
      const toN=nodesMap[e.to];
      if(fromN&&toN){
        if(toN.status==='working'&&(fromN.status==='completed'||fromN.status==='working')){
          activeIncomingNodes.add(toN.id);
          activeOutgoingNodes.add(fromN.id);
        }
      }
    });
    nodes.forEach(n=>{
      const p=pos[n.id]||{x:20,y:20};
      const nodeData=Object.assign({},n,{
        selected:String(state.flowSelectedNodeId||'')===String(n.id),
        inbound_active:activeIncomingNodes.has(n.id),
        outbound_active:activeOutgoingNodes.has(n.id),
        has_upstream:Boolean(n.depends_on&&n.depends_on.length),
        has_downstream:Boolean(n.downstream&&n.downstream.length)
      });
      graph.addNode({id:n.id,shape:'flow-card',x:p.x,y:p.y,width:232,height:134,data:nodeData});
    });
    edges.forEach(e=>{
      try{
        const fromN=nodesMap[e.from];
        const toN=nodesMap[e.to];
        const isAct=Boolean(fromN&&toN&&toN.status==='working'&&(fromN.status==='completed'||fromN.status==='working'));
        const isDone=Boolean(fromN&&toN&&fromN.status==='completed'&&toN.status==='completed');
        const strokeColor=isAct?'#4f46e5':isDone?'#94a3b8':'#c5c9d3';
        const strokeWidth=isAct?2.2:1.6;
        graph.addEdge({
          source:{cell:e.from,anchor:{name:'right',args:{dx:0}},connectionPoint:'anchor'},
          target:{cell:e.to,anchor:{name:'left',args:{dx:-5}},connectionPoint:'anchor'},
          connector:{name:'smooth'},
          attrs:{
            line:{
              stroke:strokeColor,
              'stroke-width':strokeWidth,
              strokeDasharray:isAct?'6 4':'none',
              style:isAct?{animation:'flowEdgeDash 1.2s linear infinite'}:{},
              targetMarker:{name:'block',size:isAct?7:6,fill:strokeColor,stroke:strokeColor}
            }
          },
          zIndex:isAct?10:1
        });
      }catch(err){}
    });
    graph.on('node:click',({node})=>{try{selectFlowNode(node.id);}catch(err){}});
    updateFlowSelection();
    fitFlowGraph();
  }catch(e){
    destroyFlowGraph();
    if(errBox){errBox.style.display='block';errBox.textContent='Flow Canvas 初始化失败：'+e.message;}
  }
}
function flowNodeById(id){const g=flowGraphData();return (g.nodes||[]).find(n=>n.id===id)||null;}
function flowTasksForNode(nodeId){const ts=(state.workflow&&state.workflow.tasks)||[];return ts.filter(t=>String(t.node||t.stage||'')===String(nodeId));}
function renderNodeInspector(){const body=document.getElementById('flowInspectorBody');const title=document.getElementById('flowInspTitle');const meta=document.getElementById('flowInspMeta');if(!body)return;const node=flowNodeById(state.flowSelectedNodeId);if(!node){if(title)title.textContent='节点详情';if(meta)meta.textContent='未选择';body.innerHTML='<div class="empty insp-empty"><strong>还没有选中节点</strong><span>在流程图里点一个阶段，这里会显示任务、上下文和运行情况。</span></div>';return;}if(title)title.textContent=cleanStageLabel(node.label||node.id);if(meta)meta.textContent=(node.node_type||'agent').toUpperCase()+' · '+flowNodeStatus(node,humanNodeStatus(node.status));const tab=state.flowInspectorTab||'summary';if(tab==='tasks'){const nts=flowTasksForNode(node.id);body.innerHTML=nts.length?nts.map(renderTaskRow).join(''):'<div class="empty">该节点暂无任务</div>';return;}if(tab==='context'){const ctx=(state.workflow&&state.workflow.context)||{required:[],optional:[]};const req=ctx.required||[];const opt=ctx.optional||[];body.innerHTML=`<div class="proj-sec"><div class="proj-lbl">必需上下文</div>${req.length?req.map(c=>`<div>· ${esc(typeof c==='string'?c:(c.id||c))}</div>`).join(''):'<div class="muted">—</div>'}</div><div class="proj-sec"><div class="proj-lbl">可选上下文</div>${opt.length?opt.map(c=>`<div>· ${esc(typeof c==='string'?c:(c.id||c))}</div>`).join(''):'<div class="muted">—</div>'}</div><div class="muted" style="margin-top:8px">仅显示真实 Context Contract；无绑定信息时不伪造已加载。</div>`;return;}if(tab==='runtime'){const nts=flowTasksForNode(node.id);body.innerHTML=nts.length?nts.map(t=>`<div class="flow-node-card" style="margin-bottom:8px"><div><strong>${esc(t.task_id)}</strong> ${taskBadge(t)}${deliveryWaitHtml(t)}</div><dl class="flow-kv" style="margin-top:8px"><dt>执行者</dt><dd>${esc(t.agent||'—')}</dd><dt>工位</dt><dd>${esc(t.pane_id||'—')}</dd><dt>更新</dt><dd>${esc(t.updated_at||t.last_activity_at||'—')}</dd><dt>交付管道</dt><dd>${esc(deliveryPipelineLine(t)||'—')}</dd><dt>结论</dt><dd>${esc(t.stage_verdict||'—')}</dd></dl></div>`).join(''):'<div class="empty">暂无运行时信息</div>';return;}const nts=flowTasksForNode(node.id);const actHtml='<div class="flow-act-group"><button class="mini" onclick="openControllerCockpit('+esc(JSON.stringify({workflowId:state.workflowId,nodeId:node.id}))+')">在 Controller 中处理</button></div>';body.innerHTML=flowOverviewHtml(node, nts)+actHtml;}
let _resizeFlowDebounce=null;
window.addEventListener('resize',()=>{
  clearTimeout(_resizeFlowDebounce);
  _resizeFlowDebounce=setTimeout(()=>{
    try{if(state.workflowView!=='list'){resizeFlowGraph();fitFlowGraph();}}catch(e){}
  },100);
});
return {
  flowCardFoot, flowNodeStatus, flowOverviewHtml, setWorkspaceMode,
  switchWorkflowView, switchFlowInspectorTab, destroyFlowGraph, resizeFlowGraph,
  fitFlowGraph, flowZoomIn, flowZoomOut, renderFlowWorkbench,
  renderFlowGraph, flowNodeById, flowTasksForNode, renderNodeInspector
};
})({
  state, esc, humanNodeStatus,
  cleanStageLabel, taskBadge, deliveryWaitHtml,
  deliveryPipelineLine, renderTasks, renderTaskRow,
  workflowDeliverables:(...args)=>workflowDeliverables(...args), deliverableNoteHtml:(...args)=>deliverableNoteHtml(...args), deliverableRowHtml:(...args)=>deliverableRowHtml(...args),
  renderDeliverables:(...args)=>renderDeliverables(...args), openTaskDrawer:(...args)=>openTaskDrawer(...args), currentRecovery:(...args)=>currentRecovery(...args),
  dispatchConfirmationHtml:(...args)=>dispatchConfirmationHtml(...args), recoveryUnavailable:(...args)=>recoveryUnavailable(...args), recoveryCard:(...args)=>recoveryCard(...args)
});
