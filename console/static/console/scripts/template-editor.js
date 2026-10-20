/* Template configuration form, YAML generation and save ownership. */
var {
  showTemplateEditor, closeTplModal, switchTplModalTab, updateTplStageField,
  updateTplStageDeps, addNewTplStage, deleteTplStage, copyTplYaml,
  saveTemplate
} = (function({
  state, api, toast,
  esc, fetchTemplates, getDynamicTemplateCategories,
  renderTplCategories, renderTplCards, rememberSpaceTemplate,
  selectTemplateInLibrary, closeModal
}){


const TEMPLATE_SCAFFOLD=`name: my-workflow
label: 我的工作流
version: "1.0"
description: 在这里描述业务流程

nodes:
  - id: analyze
    label: 分析
    node_type: agent
    purpose: 第一步做什么

  - id: deliver
    label: 交付
    node_type: agent
    depends_on: [analyze]
    purpose: 汇总并产出结果
`;

async function showTemplateEditor(id){
  const info=(state.templates||[]).find(t=>t.id===id);
  const builtin=!!(info&&info.is_builtin);
  let yaml=TEMPLATE_SCAFFOLD;
  let detail=null;
  if(id){
    try{
      const d=await api('/api/template?id='+encodeURIComponent(id));
      yaml=d.yaml;
      detail=d;
    }catch(e){return toast(e.message,true)}
  }
  openTplConfigModal(id,builtin,yaml,detail);
}

function openTplConfigModal(id,builtin,yaml,detail){
  const modal=document.getElementById('tplConfigModal');
  if(!modal)return;
  window._tplModalTab='form';
  window._tplIsBuiltin=Boolean(builtin);
  window._curEditingId=id||'';

  const tpl=detail?detail.template:{
    id:id||'custom-flow-'+Math.floor(Math.random()*1000),
    label:'新建自定义工作流',
    category:'biz',
    description:'描述此工作流模板的使用场景…'
  };

  const rawNodes=detail?detail.nodes:[
    {id:'step_1',label:'第一步',depends_on:[],purpose:'初始任务阶段',agents:['claude']},
    {id:'step_2',label:'第二步',depends_on:['step_1'],purpose:'汇总并产出交付物',agents:['codex']}
  ];

  window._curEditingTemplate={
    id:tpl.id||id||'custom-flow',
    label:tpl.label||tpl.id||'我的工作流',
    category:tpl.category_name||tpl.category||'📊 商务咨询',
    category_name:tpl.category_name||tpl.category||'📊 商务咨询',
    version:tpl.version||'1.0',
    description:tpl.description||'',
    nodes:JSON.parse(JSON.stringify(rawNodes))
  };

  document.getElementById('tplModalHint').textContent=id?`[${id}]${builtin?' · 内置只读':''}`:'[新建模板]';
  document.getElementById('tplEditLabel').value=window._curEditingTemplate.label;
  const nameInput=document.getElementById('tplName');
  if(nameInput){
    nameInput.value=window._curEditingTemplate.id;
    nameInput.disabled=Boolean(builtin&&id);
  }
  const catInput=document.getElementById('tplEditCategory');
  const catDatalist=document.getElementById('tplCatDatalist');
  if(catDatalist){
    const dynamicCats=getDynamicTemplateCategories().filter(c=>c.id!=='all');
    catDatalist.innerHTML=dynamicCats.map(c=>`<option value="${esc(c.label)}">${esc(c.label)}</option>`).join('');
  }
  if(catInput){
    catInput.value=window._curEditingTemplate.category_name||window._curEditingTemplate.category||'📊 商务咨询';
  }
  document.getElementById('tplEditDesc').value=window._curEditingTemplate.description||'';
  document.getElementById('tplYaml').value=yaml||generateYamlFromTplState(window._curEditingTemplate);

  const saveBtn=document.getElementById('btnSaveTplConfig');
  if(saveBtn){
    if(builtin&&id){
      saveBtn.textContent='内置模板只读（请修改模板名另存为自定义）';
      nameInput.oninput=()=>{
        const isNew=nameInput.value.trim()!==id;
        saveBtn.disabled=!isNew;
        saveBtn.textContent=isNew?'另存为自定义模板':'内置模板只读';
      };
      saveBtn.disabled=true;
    }else{
      saveBtn.textContent='保存并应用此模板';
      saveBtn.disabled=false;
      if(nameInput)nameInput.oninput=null;
    }
  }

  switchTplModalTab('form');
  renderTplStagesEditorList();
  renderTplLiveDAG();
  modal.style.display='flex';
}

function closeTplModal(){
  const modal=document.getElementById('tplConfigModal');
  if(modal)modal.style.display='none';
}

function switchTplModalTab(tab){
  window._tplModalTab=tab;
  const isForm=tab==='form';
  const tabBtnForm=document.getElementById('tplTabBtnForm');
  const tabBtnYaml=document.getElementById('tplTabBtnYaml');
  const bodyForm=document.getElementById('tplModalTabForm');
  const bodyYaml=document.getElementById('tplModalTabYaml');
  if(tabBtnForm)tabBtnForm.classList.toggle('active',isForm);
  if(tabBtnYaml)tabBtnYaml.classList.toggle('active',!isForm);
  if(bodyForm)bodyForm.style.display=isForm?'flex':'none';
  if(bodyYaml)bodyYaml.style.display=isForm?'none':'flex';

  if(!isForm&&window._curEditingTemplate){
    syncTplFormToState();
    const yaml=generateYamlFromTplState(window._curEditingTemplate);
    const yamlEl=document.getElementById('tplYaml');
    if(yamlEl)yamlEl.value=yaml;
  }
}

function renderTplStagesEditorList(){
  const container=document.getElementById('tplStagesEditorList');
  if(!container||!window._curEditingTemplate)return;
  const nodes=window._curEditingTemplate.nodes||[];

  container.innerHTML=nodes.map((n,idx)=>{
    const otherNodes=nodes.filter(o=>o.id!==n.id);
    const depOptions=otherNodes.map(o=>{
      const isSel=(n.depends_on||[]).includes(o.id);
      return `<option value="${esc(o.id)}" ${isSel?'selected':''}>${esc(o.label||o.id)} (${esc(o.id)})</option>`;
    }).join('');

    return `
      <div class="node-editor-item" style="background:var(--bg-surface);border:1px solid var(--border-default);border-radius:var(--radius-md);padding:8px 12px;display:flex;flex-direction:column;gap:8px;margin-bottom:8px">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="width:18px;height:18px;background:var(--primary-subtle);color:var(--primary);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10.5px;font-weight:600">${idx+1}</span>
            <input type="text" value="${esc(n.label||n.id)}" style="font-weight:600;font-size:13px;border:none;background:transparent;outline:none;color:var(--text-primary)" onchange="updateTplStageField(${idx},'label',this.value)" placeholder="工序环节名称">
            <span style="font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">id: ${esc(n.id)}</span>
          </div>
          <button type="button" class="btn mini" style="color:var(--danger);border-color:transparent" onclick="deleteTplStage(${idx})">删除</button>
        </div>
        <div style="display:flex;gap:8px">
          <div style="flex:1;display:flex;flex-direction:column;gap:4px">
            <label style="font-size:11px;font-weight:600;color:var(--text-secondary)">前置依赖环节 (Depends On)</label>
            <select multiple style="height:54px;font-size:11.5px;border:1px solid var(--border-default);border-radius:var(--radius-sm);padding:4px" onchange="updateTplStageDeps(${idx},this)">
              ${depOptions}
            </select>
          </div>
          <div style="flex:1;display:flex;flex-direction:column;gap:4px">
            <label style="font-size:11px;font-weight:600;color:var(--text-secondary)">工序职责使命 (Purpose)</label>
            <textarea rows="2" style="font-size:11.5px;border:1px solid var(--border-default);border-radius:var(--radius-sm);padding:4px" onchange="updateTplStageField(${idx},'purpose',this.value)">${esc(n.purpose||'')}</textarea>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function updateTplStageField(idx,field,val){
  if(window._curEditingTemplate&&window._curEditingTemplate.nodes[idx]){
    window._curEditingTemplate.nodes[idx][field]=val;
    renderTplLiveDAG();
  }
}

function updateTplStageDeps(idx,selectEl){
  if(window._curEditingTemplate&&window._curEditingTemplate.nodes[idx]){
    const selected=Array.from(selectEl.selectedOptions).map(opt=>opt.value);
    window._curEditingTemplate.nodes[idx].depends_on=selected;
    renderTplLiveDAG();
  }
}

function addNewTplStage(){
  if(!window._curEditingTemplate)return;
  const nodes=window._curEditingTemplate.nodes||[];
  const newId='stage_'+(nodes.length+1);
  const prevId=nodes.length>0?nodes[nodes.length-1].id:null;
  nodes.push({
    id:newId,
    label:'新增工序环节',
    depends_on:prevId?[prevId]:[],
    purpose:'描述该阶段的业务目标与产出',
    agents:['claude']
  });
  renderTplStagesEditorList();
  renderTplLiveDAG();
  toast('已新增工序环节，右侧拓扑图已实时重排！');
}

function deleteTplStage(idx){
  if(!window._curEditingTemplate)return;
  const nodes=window._curEditingTemplate.nodes||[];
  if(nodes.length<=1)return toast('工作流至少需要保留 1 个工序环节',true);
  const removedId=nodes[idx].id;
  nodes.splice(idx,1);
  nodes.forEach(n=>{
    n.depends_on=(n.depends_on||[]).filter(dep=>dep!==removedId);
  });
  renderTplStagesEditorList();
  renderTplLiveDAG();
  toast('工序已删除，拓扑连线已自动更新');
}

function renderTplLiveDAG(){
  const container=document.getElementById('tplLiveCanvas');
  if(!container||!window._curEditingTemplate)return;
  container.innerHTML='';
  if(window._tplLiveGraph&&window._tplLiveGraph.dispose){
    try{window._tplLiveGraph.dispose();}catch(e){}
    window._tplLiveGraph=null;
  }
  if(!window.X6||!window.dagre)return;
  const nodes=window._curEditingTemplate.nodes||[];
  const W=container.clientWidth||340;
  const H=container.clientHeight||450;

  try{
    const g=new window.dagre.graphlib.Graph();
    g.setGraph({rankdir:'TB',ranksep:36,nodesep:20,marginx:16,marginy:16});
    g.setDefaultEdgeLabel(()=>({}));
    const nodeW=170,nodeH=52;
    nodes.forEach(n=>g.setNode(n.id,{width:nodeW,height:nodeH}));
    const edges=[];
    nodes.forEach(n=>{
      (n.depends_on||[]).forEach(dep=>{
        g.setEdge(dep,n.id);
        edges.push({from:dep,to:n.id});
      });
    });
    window.dagre.layout(g);

    const liveGraph=new window.X6.Graph({
      container:container,width:W,height:H,
      background:{color:'transparent'},
      panning:{enabled:true},
      mousewheel:{enabled:true},
      interacting:{nodeMovable:false}
    });
    window._tplLiveGraph=liveGraph;

    nodes.forEach(n=>{
      const pos=g.node(n.id)||{x:50,y:50};
      liveGraph.addNode({
        id:n.id,
        x:pos.x-nodeW/2,
        y:pos.y-nodeH/2,
        width:nodeW,
        height:nodeH,
        attrs:{
          body:{fill:'#ffffff',stroke:'#5e6ad2',strokeWidth:1.5,rx:6,ry:6},
          label:{text:n.label||n.id,fill:'#121316',fontSize:12,fontWeight:'600'}
        }
      });
    });

    edges.forEach(e=>{
      liveGraph.addEdge({
        source:{cell:e.from},
        target:{cell:e.to},
        attrs:{
          line:{stroke:'#5e6ad2',strokeWidth:1.8,targetMarker:{name:'block',size:5,fill:'#5e6ad2',stroke:'#5e6ad2'}}
        }
      });
    });

    setTimeout(()=>{
      if(liveGraph&&liveGraph.zoomToFit)liveGraph.zoomToFit({padding:16});
    },40);
  }catch(e){}
}

function syncTplFormToState(){
  if(!window._curEditingTemplate)return;
  window._curEditingTemplate.label=document.getElementById('tplEditLabel')?.value||window._curEditingTemplate.label;
  window._curEditingTemplate.id=document.getElementById('tplName')?.value||window._curEditingTemplate.id;
  const catVal=document.getElementById('tplEditCategory')?.value||window._curEditingTemplate.category;
  window._curEditingTemplate.category=catVal;
  window._curEditingTemplate.category_name=catVal;
  window._curEditingTemplate.description=document.getElementById('tplEditDesc')?.value||window._curEditingTemplate.description;
}

function generateYamlFromTplState(tpl){
  const cat=tpl.category_name||tpl.category||'💻 软件研发';
  let yaml=`name: ${tpl.id}\nlabel: ${tpl.label}\ncategory: "${cat}"\nversion: "${tpl.version||'1.0'}"\ndescription: ${tpl.description||''}\n\nnodes:\n`;
  (tpl.nodes||[]).forEach(n=>{
    yaml+=`  - id: ${n.id}\n`;
    yaml+=`    label: ${n.label||n.id}\n`;
    yaml+=`    node_type: agent\n`;
    yaml+=`    depends_on: [${(n.depends_on||[]).join(', ')}]\n`;
    yaml+=`    purpose: ${n.purpose||''}\n`;
    yaml+=`    agent_policy:\n`;
    yaml+=`      preferred: [${(n.agents||['claude']).join(', ')}]\n\n`;
  });
  return yaml;
}

function copyTplYaml(){
  const text=document.getElementById('tplYaml')?.value||'';
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(text);
  }
  toast('YAML 已复制到剪贴板');
}

async function saveTemplate(){
  const name=(document.getElementById('tplName')?.value||'').trim();
  let yaml=document.getElementById('tplYaml')?.value||'';
  if(window._tplModalTab==='form'&&window._curEditingTemplate){
    syncTplFormToState();
    yaml=generateYamlFromTplState(window._curEditingTemplate);
    const yamlEl=document.getElementById('tplYaml');
    if(yamlEl)yamlEl.value=yaml;
  }
  if(!name)return toast('请填写模板名',true);
  const sid=state.spaceId; // 保存发起时的空间:继续帧按发起空间记账,不随切换漂移
  try{
    const d=await api('/api/template',{method:'POST',body:JSON.stringify({name:name,yaml:yaml})});
    closeTplModal();
    closeModal();
    await fetchTemplates();
    renderTplCategories();
    renderTplCards();
    if(sid===state.spaceId)selectTemplateInLibrary(d.name,1);
    else rememberSpaceTemplate(d.name,sid);
    toast('模板已保存：'+d.name+'（'+d.node_count+' 节点）');
  }catch(e){toast(e.message,true)}
}
return {
  showTemplateEditor, closeTplModal, switchTplModalTab, updateTplStageField,
  updateTplStageDeps, addNewTplStage, deleteTplStage, copyTplYaml,
  saveTemplate
};
})({
  state, api, toast,
  esc, fetchTemplates, getDynamicTemplateCategories,
  renderTplCategories, renderTplCards, rememberSpaceTemplate,
  selectTemplateInLibrary, closeModal
});
