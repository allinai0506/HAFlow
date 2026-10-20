/* Template browsing, space selection, DAG and node drawer. */
var {
  fetchTemplates, getDynamicTemplateCategories, renderTplCategories, setTplCategory,
  filterTemplates, renderTplCards, rememberSpaceTemplate, effectiveTemplateId,
  selectTemplateInLibrary, fitTplGraph, zoomTplGraph, openTemplateNodeDrawer,
  closeTplNodeDrawer, launchWithCurrentTemplate, showTemplateLibrary, showTemplateDAG,
  populateTemplateSelect
} = (function({
  state, saveViewState, api,
  toast, esc, renderWorkflowTabs,
  paintCrumb, setWorkspaceMode, openModal,
  showNewWorkflow
}){

async function fetchTemplates(){
  const d=await api('/api/templates');
  state.templates=d.templates||[];
  return state.templates
}

const BUILTIN_TEMPLATE_CATEGORIES=[
  {id:'all',label:'全部'},
  {id:'eng',label:'💻 软件研发'},
  {id:'biz',label:'📊 商务咨询'},
  {id:'ops',label:'🚀 运营服务'},
  {id:'sys',label:'🧪 系统测试'}
];

const CUSTOM_BADGE_CLASSES=['cat-purple','cat-emerald','cat-rose','cat-teal','cat-indigo','cat-amber'];

function getTplBadgeClass(catId){
  if(!catId)return 'cat-biz';
  if(['eng','biz','ops','sys'].includes(catId))return 'cat-'+catId;
  let hash=0;
  for(let i=0;i<catId.length;i++){
    hash=((hash<<5)-hash)+catId.charCodeAt(i);
    hash|=0;
  }
  const idx=Math.abs(hash)%CUSTOM_BADGE_CLASSES.length;
  return CUSTOM_BADGE_CLASSES[idx];
}

function getDynamicTemplateCategories(){
  const ts=state.templates||[];
  const map=new Map();
  BUILTIN_TEMPLATE_CATEGORIES.forEach(c=>{
    map.set(c.id,{id:c.id,label:c.label,count:0});
  });
  ts.forEach(t=>{
    const cId=t.category||'biz';
    const cLabel=t.category_name||t.category||'其它';
    if(map.has(cId)){
      map.get(cId).count++;
    }else{
      map.set(cId,{id:cId,label:cLabel,count:1});
    }
  });
  const all=map.get('all');
  if(all)all.count=ts.length;
  return Array.from(map.values());
}

function ensureTplLayoutDOM(tplEl){
  if(!tplEl||document.getElementById('tplDagCanvas'))return;
  tplEl.innerHTML=`
    <div class="tpl-top-header">
      <div style="display:flex;align-items:center;gap:8px">
        <strong>工作流模板规范资产库</strong>
        <span class="muted" style="font-size:12px">· 定义工作流阶段与 DAG 依赖拓扑</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <button type="button" class="btn" onclick="showTemplateEditor()">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
          <span>新建模板</span>
        </button>
        <button type="button" class="btn primary" onclick="launchWithCurrentTemplate()">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
          <span>选用并启动需求</span>
        </button>
      </div>
    </div>
    <div class="tpl-layout">
      <aside class="tpl-left-panel">
        <div class="tpl-filter-section">
          <div class="tpl-search-box">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
            <input type="text" id="tplSearchInput" placeholder="搜索工作流模板、工序名称、业务场景…" oninput="filterTemplates()">
          </div>
          <div class="tpl-category-pills" id="tplCategoryPills"></div>
        </div>
        <div class="tpl-cards-list" id="tplCardsList"></div>
      </aside>
      <main class="tpl-right-panel">
        <div class="tpl-detail-header">
          <div class="tpl-detail-meta">
            <h2 style="font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px">
              <span id="tplCurTitle">加载中…</span>
              <span class="tpl-badge cat-eng" id="tplCurCat">软件研发</span>
              <span style="font-size:11px;font-weight:400;color:var(--text-tertiary);font-family:var(--font-mono)" id="tplCurId"></span>
            </h2>
            <p id="tplCurDesc" style="font-size:12px;color:var(--text-secondary);margin-top:4px;line-height:1.4"></p>
          </div>
          <div class="tpl-detail-actions">
            <button type="button" class="btn" onclick="showTemplateDAG(effectiveTemplateId())">
              <span>节点依赖清单</span>
            </button>
            <button type="button" class="btn" onclick="showTemplateEditor(effectiveTemplateId())">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
              <span id="tplEditBtnLabel">可视化配置 / YAML</span>
            </button>
          </div>
        </div>
        <div class="tpl-canvas-container">
          <div class="tpl-canvas-legend">
            <div style="display:flex;align-items:center;gap:5px"><span class="tpl-legend-dot start"></span><span>起始工序</span></div>
            <div style="display:flex;align-items:center;gap:5px"><span class="tpl-legend-dot middle"></span><span>流转工序</span></div>
            <div style="display:flex;align-items:center;gap:5px"><span class="tpl-legend-dot gate"></span><span>门禁质询</span></div>
            <span style="color:var(--border-default)">|</span>
            <span style="font-size:11px;color:var(--text-tertiary)">提示：点击节点卡片查看属性，支持滚轮缩放与画布平移</span>
          </div>
          <div id="tplDagCanvas"></div>
          <div class="tpl-canvas-toolbar">
            <button type="button" class="tool-btn" onclick="zoomTplGraph(0.15)" title="放大">+</button>
            <button type="button" class="tool-btn" onclick="zoomTplGraph(-0.15)" title="缩小">-</button>
            <button type="button" class="tool-btn" onclick="fitTplGraph()" title="自适应居中" style="width:36px">居中</button>
          </div>
        </div>
        <dialog class="tpl-node-drawer" id="tplNodeDrawer" aria-labelledby="tplDrawerTitle">
          <div class="tpl-node-drawer-head">
            <div class="tpl-node-drawer-heading">
              <span id="tplDrawerTitle">工序详情</span>
              <span class="tpl-badge cat-eng" id="tplDrawerType">agent 节点</span>
            </div>
            <button type="button" class="tool-btn" aria-label="关闭节点属性" onclick="closeTplNodeDrawer()">✕</button>
          </div>
          <div class="tpl-node-details">
            <div class="tpl-node-field wide"><label>工序职责</label><div id="tplDrawerPurpose">-</div></div>
            <div class="tpl-node-field"><label>负责智能体</label><div id="tplDrawerAgents">-</div></div>
            <div class="tpl-node-field"><label>前置依赖</label><div id="tplDrawerDeps">-</div></div>
            <div class="tpl-node-field wide"><label>准出门禁 / 验收要求</label><div id="tplDrawerGate">-</div></div>
          </div>
        </dialog>
      </main>
    </div>
  `;
}

function renderTplCategories(){
  const pillsEl=document.getElementById('tplCategoryPills');
  if(!pillsEl)return;
  const categories=getDynamicTemplateCategories();
  const activeCat=state.tplCategoryFilter||'all';
  pillsEl.innerHTML=categories.map(cat=>{
    const isAct=cat.id===activeCat;
    return `<button type="button" class="tpl-cat-pill ${isAct?'active':''}" onclick="setTplCategory('${esc(cat.id)}')"><span>${esc(cat.label)}</span><span class="count">${cat.count}</span></button>`;
  }).join('');
}

function setTplCategory(catId){
  state.tplCategoryFilter=catId;
  renderTplCategories();
  renderTplCards();
}

function filterTemplates(){
  renderTplCards();
}

function renderTplCards(){
  const listEl=document.getElementById('tplCardsList');
  if(!listEl)return;
  const ts=state.templates||[];
  const cat=state.tplCategoryFilter||'all';
  const q=(document.getElementById('tplSearchInput')?.value||'').trim().toLowerCase();
  const filtered=ts.filter(t=>{
    if(cat!=='all'&&t.category!==cat)return false;
    if(q){
      const matchText=[t.label||'',t.id||'',t.description||'',(t.nodes_preview||[]).map(n=>n.label).join(' ')].join(' ').toLowerCase();
      if(!matchText.includes(q))return false;
    }
    return true;
  });

  if(!filtered.length){
    listEl.innerHTML='<div class="empty">无匹配工作流模板</div>';
    return;
  }

  const curId=effectiveTemplateId();
  listEl.innerHTML=filtered.map(t=>{
    const isSel=t.id===curId;
    const is_builtin=Boolean(t.is_builtin);
    const steps=(t.nodes_preview||[]).map((n,i)=>`
      <span class="step-pill">${esc(n.label||n.id)}</span>
      ${i<(t.nodes_preview||[]).length-1?'<span class="step-arrow">➔</span>':''}
    `).join('');
    return `
      <div class="tpl-card ${isSel?'selected':''}" onclick="selectTemplateInLibrary('${esc(t.id)}',1)">
        <div class="tpl-head">
          <span class="tpl-title">
            <span>${esc(t.label||t.id)}</span>
            ${t.id==='software-development-v1'?'<span style="font-size:10px;background:var(--primary-subtle);color:var(--primary);padding:2px 4px;border-radius:4px;font-weight:600">官方推荐</span>':''}
            <span class="badge ${is_builtin?'waiting':'good-text'}">${is_builtin?'内置':'自定义'}</span>
          </span>
          <span class="tpl-badge ${getTplBadgeClass(t.category)}">${esc(t.category_name||'工作流')}</span>
        </div>
        <div class="tpl-desc">${esc(t.description||'暂无业务说明')}</div>
        <div class="tpl-pipeline" title="流程工序节点微缩示意">${steps||'<span class="muted">无节点</span>'}</div>
        <div class="tpl-foot">
          <span><b>${t.node_count||0}</b> 个工作流节点 · v${esc(t.version||'1.0')}</span>
          <span style="color:var(--primary);font-weight:600">查看拓扑 ➔</span>
        </div>
      </div>
    `;
  }).join('');
}

function projectTemplateId(){const p=state.project&&state.project.project;return (p&&p.workflow_template)||''}
function spaceTemplateSelections(){return state.templateSelections}
function rememberSpaceTemplate(id,spaceId){if(id)spaceTemplateSelections()[spaceId||state.spaceId||'']=id}
function effectiveTemplateId(){return spaceTemplateSelections()[state.spaceId||'']||projectTemplateId()||'software-development-v1'}

async function selectTemplateInLibrary(id,remember){ // remember=1 仅用户点选(卡片 onclick / 保存模板)记账;程序化显示不得覆盖空间选择
  if(remember)rememberSpaceTemplate(id);
  const ts=state.templates||[];
  const t=ts.find(x=>x.id===id)||{id:id,label:id};
  const titleEl=document.getElementById('tplCurTitle');
  const idEl=document.getElementById('tplCurId');
  const catEl=document.getElementById('tplCurCat');
  const descEl=document.getElementById('tplCurDesc');
  const editBtnEl=document.getElementById('tplEditBtnLabel');
  if(titleEl)titleEl.textContent=t.label||t.id;
  if(idEl)idEl.textContent=t.id;
  if(catEl){
    catEl.textContent=t.category_name||'工作流';
    catEl.className='tpl-badge '+getTplBadgeClass(t.category);
  }
  if(descEl)descEl.textContent=t.description||'';
  if(editBtnEl)editBtnEl.textContent=t.is_builtin?'查看 YAML / 向导':'可视化配置 / YAML';
  renderTplCards();
  if(state.currentTemplateDetail?.template?.id!==id)closeTplNodeDrawer();

  // 过期响应丢弃:切空间/连续点选后,迟到的 /api/template 继续帧不得覆盖最新选择
  const sequence=state.templateDetailSequence=(state.templateDetailSequence||0)+1;
  try{
    const d=await api('/api/template?id='+encodeURIComponent(id));
    if(sequence!==state.templateDetailSequence)return;
    if(d.template){
      if(titleEl)titleEl.textContent=d.template.label||d.template.id;
      if(descEl)descEl.textContent=d.template.description||'';
    }
    state.currentTemplateDetail=d;
    renderTplDAG(d.nodes||[]);
  }catch(e){
    if(sequence!==state.templateDetailSequence)return;
    if(t.nodes_preview)renderTplDAG(t.nodes_preview);
  }
}

function tplNodeCardHTML(d){
  const agents=(d.agents||[]).join('、')||'Auto';
  const purpose=d.purpose||'暂无说明';
  return `<div class="tpl-node-top"><span class="tpl-node-kind">${(d.depends_on||[]).length?'工序环节':'起始工序'}</span><span class="tpl-node-agents" title="${esc(agents)}">${esc(agents)}</span></div>
    <div class="tpl-node-title" title="${esc(d.label||d.id)}">${esc(d.label||d.id)}</div>
    <div class="tpl-node-purpose" title="${esc(purpose)}">${esc(purpose)}</div>
    <div class="tpl-node-footer"><span>${d.gate?'门禁校验':'自动流转'}</span><strong>查看完整属性 →</strong></div>`;
}

function ensureTplCardShape(){
  if(window._tplShapeRegistered)return true;
  if(!window.X6||!X6.Shape||!X6.Shape.HTML||!X6.Shape.HTML.register)return false;
  try{
    X6.Shape.HTML.register({
      shape:'tpl-flow-card',width:280,height:188,effect:['data'],
      html(cell){
        const d=(cell&&cell.getData)?(cell.getData()||{}):{};
        const div=document.createElement('div');
        div.className='tpl-node-card'+(d.selected?' selected':'');
        div.innerHTML=tplNodeCardHTML(d);
        div.onclick=()=>openTemplateNodeDrawer(d);
        return div;
      }
    });
    window._tplShapeRegistered=true;
    return true;
  }catch(e){return false;}
}

function renderTplDAG(nodes){
  const container=document.getElementById('tplDagCanvas');
  if(!container)return;
  container.innerHTML='';
  if(state.tplGraph&&state.tplGraph.dispose){
    try{state.tplGraph.dispose();}catch(e){}
    state.tplGraph=null;
  }
  if(!window.X6||!window.dagre){
    renderTplFallbackSVG(container,nodes);
    return;
  }
  ensureTplCardShape();
  const W=container.clientWidth||800;
  const H=container.clientHeight||500;

  try{
    const g=new window.dagre.graphlib.Graph();
    g.setGraph({rankdir:'LR',ranksep:56,nodesep:32,marginx:36,marginy:36});
    g.setDefaultEdgeLabel(()=>({}));
    const nodeW=280,nodeH=188;
    nodes.forEach(n=>g.setNode(n.id,{width:nodeW,height:nodeH}));
    const edges=[];
    nodes.forEach(n=>{
      (n.depends_on||[]).forEach(dep=>{
        g.setEdge(dep,n.id);
        edges.push({from:dep,to:n.id});
      });
    });
    window.dagre.layout(g);

    const graph=new window.X6.Graph({
      container:container,width:W,height:H,
      background:{color:'transparent'},
      panning:{enabled:true},
      mousewheel:{enabled:true,modifiers:['ctrl','meta'],minScale:0.4,maxScale:2.2},
      interacting:{nodeMovable:false,edgeMovable:false}
    });
    state.tplGraph=graph;

    nodes.forEach(n=>{
      const pos=g.node(n.id)||{x:120,y:100};
      const isStart=!n.depends_on||n.depends_on.length===0;
      graph.addNode({
        id:n.id,
        shape:'tpl-flow-card',
        x:pos.x-nodeW/2,
        y:pos.y-nodeH/2,
        width:nodeW,
        height:nodeH,
        data:{...n,isStart}
      });
    });

    edges.forEach(e=>{
      graph.addEdge({
        source:{cell:e.from,anchor:'right'},
        target:{cell:e.to,anchor:'left'},
        connector:{name:'smooth'},
        attrs:{
          line:{
            stroke:'#5e6ad2',
            strokeWidth:1.8,
            targetMarker:{name:'block',size:6,fill:'#5e6ad2',stroke:'#5e6ad2'}
          }
        }
      });
    });

    setTimeout(()=>fitTplGraph(),40);
  }catch(e){
    renderTplFallbackSVG(container,nodes);
  }
}

function renderTplFallbackSVG(container,nodes){
  if(!container)return;
  const list=document.createElement('div');
  list.style.cssText='padding:32px;display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:center;height:100%;overflow:auto;box-sizing:border-box';
  nodes.forEach(n=>{
    const card=document.createElement('div');
    card.className='tpl-node-card';
    card.innerHTML=tplNodeCardHTML(n);
    card.onclick=()=>openTemplateNodeDrawer(n);
    list.appendChild(card);
  });
  container.replaceChildren(list);
}

function fitTplGraph(){
  if(state.tplGraph&&state.tplGraph.zoomToFit){
    state.tplGraph.zoomToFit({padding:32,maxScale:1});
    if(state.tplGraph.centerContent)state.tplGraph.centerContent();
  }
}

function zoomTplGraph(delta){
  if(state.tplGraph&&state.tplGraph.zoom)state.tplGraph.zoom(delta);
}

function openTemplateNodeDrawer(node){
  const drawer=document.getElementById('tplNodeDrawer');
  if(!drawer)return;
  if(!drawer.open)drawer.showModal();
  document.getElementById('tplDrawerType').textContent=(node.node_type||'agent')+' 节点';
  document.getElementById('tplDrawerTitle').textContent=(node.label||node.id)+` (${node.id})`;
  document.getElementById('tplDrawerPurpose').textContent=node.purpose||'该阶段主要负责交付目标任务推进。';
  document.getElementById('tplDrawerAgents').textContent=(node.agents&&node.agents.length)?node.agents.join('、'):'auto (根据任务类型智能路由)';
  document.getElementById('tplDrawerDeps').textContent=(node.depends_on&&node.depends_on.length)?node.depends_on.join('、')+'（前置工序完备后自动触发）':'起始工序（无需前置依赖，工作流启动即就绪）';
  document.getElementById('tplDrawerGate').textContent=node.gate||'自动流转（未单独配置节点门禁）';
}

function closeTplNodeDrawer(){
  const drawer=document.getElementById('tplNodeDrawer');
  if(drawer&&drawer.open)drawer.close();
}

function launchWithCurrentTemplate(){
  showNewWorkflow(effectiveTemplateId());
}

async function showTemplateLibrary(){
  const tplEl=document.getElementById('templatesTabView');
  setWorkspaceMode('templates');
  if(!state.openWorkflowTabIds)state.openWorkflowTabIds=[];
  if(!state.openWorkflowTabIds.includes('__templates__'))state.openWorkflowTabIds.push('__templates__');
  state.workflowId='__templates__';
  state.opsMode=false;state.dashMode=false;state.shellView='templates';
  saveViewState();
  renderWorkflowTabs();
  paintCrumb();
  ensureTplLayoutDOM(tplEl);
  try{
    const ts=await fetchTemplates();
    renderTplCategories();
    renderTplCards();
    const targetId=effectiveTemplateId();
    const found=ts.find(t=>t.id===targetId)||ts[0];
    if(found){
      // 项目模板已删除/不可用时,回落项同步为空间选择,保证卡片选中/详情/DAG/启动与显示一致(仅会话内)
      if(found.id!==targetId)rememberSpaceTemplate(found.id);
      selectTemplateInLibrary(found.id);
    }
  }catch(e){toast(e.message,true)}
}

async function showTemplateDAG(id){
  try{
    const d=await api('/api/template?id='+encodeURIComponent(id));
    const rows=(d.nodes||[]).map(n=>`
      <div class="agent-row">
        <div>
          <div class="task-name">${esc(n.label||n.id)} <span class="task-id">${esc(n.id)} · ${esc(n.node_type||'agent')}</span></div>
          <div class="task-meta">${esc(n.purpose||'')}${(n.gate&&n.gate.type)?' · 门禁: '+esc(n.gate.type):''}${(n.worker_policy&&n.worker_policy.permissions)?' · 权限: '+esc(n.worker_policy.permissions.join('/')):''}</div>
        </div>
        <div class="task-meta">${(n.depends_on||[]).length?'← '+(n.depends_on||[]).map(esc).join('、'):'起始节点'}</div>
      </div>`).join('');
    openModal('节点依赖 · '+((d.template&&d.template.label)||id),rows||'<div class="empty">无节点</div>');
    selectTemplateInLibrary(id);
  }catch(e){toast(e.message,true)}
}

async function populateTemplateSelect(selId='newTemplate',preferredId=null){
  try{
    // 弹窗重开/替换:旧弹窗迟到的填充必须丢弃,不得覆盖新弹窗已选模板(按 selId 各自计数)
    const seqs=state.templateSelectSeq=state.templateSelectSeq||{};
    const seq=seqs[selId]=(seqs[selId]||0)+1;
    const ts=await fetchTemplates();
    if(seq!==seqs[selId])return;
    const sel=document.getElementById(selId);
    if(!sel||!ts.length)return;
    const wanted=(preferredId&&ts.some(t=>t.id===preferredId))?preferredId:'software-development-v1';
    sel.innerHTML=ts.map(t=>`<option value="${esc(t.id)}"${t.id===wanted?' selected':''}>${esc(t.id)} · ${esc(t.label||'')}（${t.node_count} 节点）</option>`).join('');
    sel.value=wanted;
  }catch(e){}
}
return {
  fetchTemplates, getDynamicTemplateCategories, renderTplCategories, setTplCategory,
  filterTemplates, renderTplCards, rememberSpaceTemplate, effectiveTemplateId,
  selectTemplateInLibrary, fitTplGraph, zoomTplGraph, openTemplateNodeDrawer,
  closeTplNodeDrawer, launchWithCurrentTemplate, showTemplateLibrary, showTemplateDAG,
  populateTemplateSelect
};
})({
  state, saveViewState, api,
  toast, esc, renderWorkflowTabs:(...args)=>renderWorkflowTabs(...args),
  paintCrumb:(...args)=>paintCrumb(...args), setWorkspaceMode:(...args)=>setWorkspaceMode(...args), openModal,
  showNewWorkflow
});
