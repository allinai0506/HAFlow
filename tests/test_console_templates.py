"""Acceptance tests for the console workflow template library and page-based authoring."""

import importlib.machinery
import importlib.util
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent

VALID_YAML = """name: demo-flow
label: 演示流程
version: "1.0"
description: 验收测试用
nodes:
  - id: gather
    label: 采集
    node_type: agent
    purpose: 收集输入
  - id: report
    label: 汇总
    node_type: agent
    depends_on: [gather]
    purpose: 汇总产出
"""


def load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_templates_test",
        importlib.machinery.SourceFileLoader("herdr_console_templates_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTemplateLibraryBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_console()

    def test_templates_summary_lists_bundled_templates(self):
        data = self.module.templates_summary()
        ids = {t["id"] for t in data["templates"]}
        self.assertIn("software-development-v1", ids)
        self.assertIn("bidding", ids)
        builtin = next(t for t in data["templates"] if t["id"] == "software-development-v1")
        self.assertTrue(builtin["is_builtin"])
        self.assertGreaterEqual(builtin["node_count"], 6)

    def test_template_detail_returns_nodes_dependencies_and_yaml(self):
        detail = self.module.template_detail("bidding")
        self.assertTrue(detail["is_builtin"])
        self.assertIn("name: bidding", detail["yaml"])
        ids = [n["id"] for n in detail["nodes"]]
        self.assertEqual(len(ids), 7)
        strategy = next(n for n in detail["nodes"] if n["id"] == "strategy")
        self.assertEqual(sorted(strategy["depends_on"]), ["history_search", "scoring_extract"])

    def test_template_detail_includes_implicit_verdict_gates(self):
        detail = self.module.template_detail("software-development-v1")
        nodes = {n["id"]: n for n in detail["nodes"]}
        for key in ("test", "review", "wrapup"):
            self.assertTrue(nodes[key]["gate"], key)
        self.assertFalse(nodes["implementation"]["gate"])

    def test_template_detail_unknown_name_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.module.template_detail("no-such-template")
        self.assertIn("模板不存在", str(ctx.exception))

    def test_templates_summary_includes_category_and_nodes_preview(self):
        data = self.module.templates_summary()
        t_dev = next(t for t in data["templates"] if t["id"] == "software-development-v1")
        self.assertEqual(t_dev["category"], "eng")
        self.assertEqual(t_dev["category_name"], "💻 软件研发")
        self.assertGreaterEqual(len(t_dev["nodes_preview"]), 6)
        labels = [n["label"] for n in t_dev["nodes_preview"]]
        self.assertTrue(any("需求" in l for l in labels))

        t_bid = next(t for t in data["templates"] if t["id"] == "bidding")
        self.assertEqual(t_bid["category"], "biz")
        self.assertEqual(t_bid["category_name"], "📊 商务咨询")
        self.assertGreaterEqual(len(t_bid["nodes_preview"]), 7)

    def test_template_detail_includes_rich_node_metadata(self):
        detail = self.module.template_detail("software-development-v1")
        self.assertEqual(detail["template"]["category"], "eng")
        self.assertEqual(detail["template"]["category_name"], "💻 软件研发")
        req_node = next(n for n in detail["nodes"] if n["id"] == "requirements")
        self.assertIn("需求", req_node["label"])
        self.assertTrue(len(req_node["purpose"]) > 0)
        self.assertIn("gate", req_node)
        self.assertIn("agents", req_node)

    def test_derive_template_category_custom_and_builtins(self):
        derive = self.module._derive_template_category
        self.assertEqual(derive("custom-1", {"category": "dev"}), ("eng", "💻 软件研发"))
        self.assertEqual(derive("custom-2", {"category": "商务咨询"}), ("biz", "📊 商务咨询"))
        self.assertEqual(derive("custom-3", {"category": "operation"}), ("ops", "🚀 运营服务"))
        self.assertEqual(derive("custom-4", {"category": "sys"}), ("sys", "🧪 系统测试"))
        slug, name = derive("custom-legal", {"category": "⚖️ 法务合规"})
        self.assertTrue(slug.startswith("cat_"))
        self.assertEqual(name, "⚖️ 法务合规")
        self.assertEqual(derive("custom-mkt", {"category": "marketing-ops"}), ("marketing-ops", "marketing-ops"))
        self.assertEqual(derive("software-development-v1", {}), ("eng", "💻 软件研发"))


class TestTemplateSave(unittest.TestCase):
    def setUp(self):
        self.module = load_console()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.user_dir = Path(tmp.name)
        patcher = patch.object(self.module.herdr_workflow, "USER_TEMPLATES_DIR", self.user_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_template_writes_valid_yaml_to_user_dir(self):
        result = self.module.save_template("demo-flow", VALID_YAML)
        self.assertTrue((self.user_dir / "demo-flow.yaml").exists())
        self.assertEqual(result["node_count"], 2)
        ids = {t["id"] for t in self.module.templates_summary()["templates"]}
        self.assertIn("demo-flow", ids)
        saved = next(t for t in self.module.templates_summary()["templates"] if t["id"] == "demo-flow")
        self.assertFalse(saved["is_builtin"])

    def test_save_template_rejects_circular_dependency(self):
        cyclic = VALID_YAML.replace("depends_on: [gather]", "depends_on: [gather, report]")
        with self.assertRaises(RuntimeError) as ctx:
            self.module.save_template("demo-flow", cyclic)
        self.assertIn("cycle", str(ctx.exception))
        self.assertFalse((self.user_dir / "demo-flow.yaml").exists())

    def test_save_template_rejects_unknown_dependency(self):
        broken = VALID_YAML.replace("depends_on: [gather]", "depends_on: [ghost]")
        with self.assertRaises(RuntimeError):
            self.module.save_template("demo-flow", broken)
        self.assertFalse((self.user_dir / "demo-flow.yaml").exists())

    def test_save_template_rejects_path_traversal_name(self):
        with self.assertRaises(RuntimeError):
            self.module.save_template("../evil", VALID_YAML)
        self.assertFalse((self.user_dir.parent / "evil.yaml").exists())

    def test_save_template_rejects_builtin_override(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.module.save_template("bidding", VALID_YAML)
        self.assertIn("内置模板只读", str(ctx.exception))

    def test_save_template_rejects_missing_nodes(self):
        with self.assertRaises(RuntimeError):
            self.module.save_template("no-nodes", "name: no-nodes\nlabel: 空\n")

    def test_save_template_rejects_mismatched_name_field(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.module.save_template("demo-flow", VALID_YAML.replace("name: demo-flow", "name: other"))
        self.assertIn("不一致", str(ctx.exception))

    def test_save_template_supports_custom_category(self):
        custom_yaml = VALID_YAML.replace("name: demo-flow", "name: legal-flow").replace("label: 演示流程", 'label: 法务审查\ncategory: "⚖️ 法务合规"')
        self.module.save_template("legal-flow", custom_yaml)
        summary = self.module.templates_summary()
        saved = next(t for t in summary["templates"] if t["id"] == "legal-flow")
        self.assertEqual(saved["category_name"], "⚖️ 法务合规")
        self.assertTrue(saved["category"].startswith("cat_"))

        detail = self.module.template_detail("legal-flow")
        self.assertEqual(detail["template"]["category_name"], "⚖️ 法务合规")
        self.assertEqual(detail["template"]["category"], saved["category"])


class TestRunWorkflowTemplate(unittest.TestCase):
    def setUp(self):
        self.module = load_console()

    @staticmethod
    def completed():
        return type("Completed", (), {"returncode": 0, "stdout": "WORKFLOW_ID\n", "stderr": ""})()

    def test_run_workflow_forwards_template_flag(self):
        with patch.object(self.module, "run", return_value=self.completed()) as run_command:
            self.module.run_workflow("/tmp/project", "需求", "auto", "bidding")
        cmd = run_command.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--template") + 1], "bidding")

    def test_run_workflow_defaults_to_software_development(self):
        with patch.object(self.module, "run", return_value=self.completed()) as run_command:
            self.module.run_workflow("/tmp/project", "需求")
        cmd = run_command.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--template") + 1], "software-development-v1")

    def test_start_workflow_job_carries_template_to_run_workflow(self):
        with patch.object(self.module, "run_workflow", return_value="WORKFLOW_ID") as run_workflow:
            job = self.module.start_workflow_job("/tmp/project", "需求", "auto", "bidding")
        for _ in range(50):
            if self.module.workflow_job_status(job["job_id"])["status"] != "running":
                break
            time.sleep(0.01)
        run_workflow.assert_called_once_with("/tmp/project", "需求", "auto", "bidding")


class TestTemplateLibraryUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = load_console().HTML

    def function_body(self, name):
        match = re.search(
            rf"(?:async )?function {name}\([^)]*\)\{{(.*?)\n\}}",
            self.html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, f"missing JavaScript function: {name}")
        return match.group(1)

    def test_template_library_button_exists_in_factory_actions(self):
        self.assertIn("showTemplateLibrary()", self.html)
        self.assertIn(">模板库</button>", self.html.replace("\n", ""))

    def test_template_library_fetches_and_renders_templates(self):
        body = self.function_body("showTemplateLibrary")
        self.assertIn("fetchTemplates()", body)
        self.assertIn("/api/templates", self.function_body("fetchTemplates"))
        self.assertIn("is_builtin", self.function_body("selectTemplateInLibrary"))

    def test_template_dag_preview_renders_dependencies(self):
        body = self.function_body("showTemplateDAG")
        self.assertIn("/api/template?id=", body)
        self.assertIn("depends_on", body)

    def test_template_editor_saves_via_post(self):
        body = self.function_body("saveTemplate")
        self.assertIn("'/api/template'", body)
        self.assertIn("name", body)
        self.assertIn("yaml", body)

    def test_new_workflow_modal_has_template_selector(self):
        body = self.function_body("showNewWorkflow")
        self.assertIn("newTemplate", body)

    def test_template_select_defaults_to_software_development(self):
        body = self.function_body("populateTemplateSelect")
        self.assertIn("software-development-v1", body)
        self.assertIn("selected", body)

    def test_submit_new_workflow_sends_template(self):
        body = self.function_body("submitNewWorkflowAsync")
        self.assertIn("newTemplate", body)
        self.assertIn("template:", body)

    def test_show_template_library_uses_space_effective_template(self):
        """模板库选中态必须按工作空间隔离,不得沿用全局选择。"""
        body = self.function_body("showTemplateLibrary")
        self.assertIn("effectiveTemplateId()", body)
        self.assertNotIn("state.currentTemplateId", body)

    def test_launch_with_current_template_carries_selection(self):
        body = self.function_body("launchWithCurrentTemplate")
        self.assertIn("showNewWorkflow(effectiveTemplateId())", body)
        self.assertNotIn("sel.value", body)

    def test_show_new_workflow_forwards_preferred_template(self):
        body = self.function_body("showNewWorkflow")
        self.assertIn("preferredTemplateId", body)
        self.assertIn("populateTemplateSelect('newTemplate',preferredTemplateId)", body)

    def test_populate_template_select_honors_preferred_id(self):
        body = self.function_body("populateTemplateSelect")
        self.assertIn("preferredId", body)
        self.assertIn("software-development-v1", body)

    def test_template_cards_mark_user_pick_for_remembering(self):
        body = self.function_body("renderTplCards")
        self.assertIn("selectTemplateInLibrary('${esc(t.id)}',1)", body)
        self.assertIn("effectiveTemplateId()", body)

    def test_space_switch_refreshes_open_template_library(self):
        body = self.function_body("selectSpace")
        self.assertIn("rer&&state.shellView==='templates'", body)
        self.assertIn("showTemplateLibrary()", body)

    def test_save_template_remembers_saved_template_as_space_pick(self):
        body = self.function_body("saveTemplate")
        self.assertIn("selectTemplateInLibrary(d.name,1)", body)


class TestTemplateLibraryBehavior(unittest.TestCase):
    """run_js 行为回归:模板选中态按工作空间隔离,弹窗携带首选模板。"""

    @classmethod
    def setUpClass(cls):
        cls.html = load_console().HTML

    def run_js(self, block, script):
        result = subprocess.run(
            ['node', '-e', "const assert=require('node:assert/strict');\n"
             + script.split('// SOURCE')[0] + '\n' + block + '\n'
             + script.split('// SOURCE')[1]],
            text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refreshing_same_template_preserves_node_details(self):
        source = self.html
        block = source[source.index('async function selectTemplateInLibrary('):
                       source.index('function tplNodeCardHTML(')]
        self.run_js(block, r'''
const state={templates:[],currentTemplateDetail:{template:{id:'alpha'}}};
const document={getElementById:()=>null};
let closed=0;
function renderTplCards(){}
function closeTplNodeDrawer(){closed++}
function renderTplDAG(){}
async function api(url){return {template:{id:url.split('=')[1]},nodes:[]}}
// SOURCE
(async()=>{
 await selectTemplateInLibrary('alpha');
 assert.equal(closed,0,'刷新同一模板不得打断属性阅读');
 await selectTemplateInLibrary('beta');
 assert.equal(closed,1,'切换模板必须关闭旧节点属性');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_template_selection_is_scoped_per_space(self):
        source = self.html
        block = source[source.index('function projectTemplateId('):
                       source.index('async function selectTemplateInLibrary(')]
        self.run_js(block, r'''
const state={spaceId:'w1',project:{project:{workflow_template:'software-development-v1'}},templateSelections:{}};
// SOURCE
assert.equal(effectiveTemplateId(),'software-development-v1','无选择时回落到项目实际模板');
rememberSpaceTemplate('bidding');
assert.equal(effectiveTemplateId(),'bidding');
state.spaceId='w2';
assert.equal(effectiveTemplateId(),'software-development-v1','其他空间不得继承 w1 的选择');
state.spaceId='w1';
assert.equal(effectiveTemplateId(),'bidding','回到 w1 恢复其自身选择');
state.project=null;
state.spaceId='w3';
assert.equal(effectiveTemplateId(),'software-development-v1','无项目无选择时回落默认模板');
''')

    def test_select_template_in_library_remembers_only_user_pick(self):
        source = self.html
        block = source[source.index('function projectTemplateId('):
                       source.index('function ensureTplCardShape(')]
        self.run_js(block, r'''
const state={spaceId:'w1',templateSelections:{},templates:[{id:'bidding',label:'投标'}],currentTemplateDetail:null};
const nodes={};
function element(id){return nodes[id]||(nodes[id]={textContent:'',className:''});}
const document={getElementById:element};
async function api(){return {template:{label:'投标',id:'bidding'},nodes:[]};}
function renderTplCards(){}
function closeTplNodeDrawer(){}
function renderTplDAG(){}
function getTplBadgeClass(){return 'cat-biz'}
// SOURCE
(async()=>{
 await selectTemplateInLibrary('bidding',1);
 assert.equal(state.templateSelections['w1'],'bidding','用户点选必须记入当前空间');
 await selectTemplateInLibrary('software-development-v1');
 assert.equal(state.templateSelections['w1'],'bidding','程序化显示不得覆盖用户选择');
 assert.equal(nodes['tplCurId'].textContent,'software-development-v1','显示仍应跟随传入模板');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_stale_template_detail_response_does_not_overwrite_newer_selection(self):
        """切换空间/连续点选时,迟到的旧模板详情响应不得覆盖最新选择(Codex P2)。"""
        source = self.html
        block = source[source.index('function projectTemplateId('):
                       source.index('function ensureTplCardShape(')]
        self.run_js(block, r'''
const state={spaceId:'w1',templateSelections:{},templates:[{id:'bidding',label:'投标'},{id:'code',label:'代码'}],currentTemplateDetail:null};
const nodes={};
function element(id){return nodes[id]||(nodes[id]={textContent:'',className:''});}
const document={getElementById:element};
const dagCalls=[];
async function api(url){
  if(url.includes('bidding'))return new Promise(r=>setTimeout(()=>r({template:{id:'bidding',label:'迟到旧响应'},nodes:[{id:'old-node'}]}),30));
  return {template:{id:'code',label:'代码'},nodes:[{id:'new-node'}]};
}
function renderTplCards(){}
function closeTplNodeDrawer(){}
function renderTplDAG(ns){dagCalls.push((ns||[]).map(n=>n.id).join(','))}
function getTplBadgeClass(){return 'cat-biz'}
// SOURCE
(async()=>{
 const p1=selectTemplateInLibrary('bidding',1);
 const p2=selectTemplateInLibrary('code');
 await Promise.all([p1,p2]);
 assert.equal(nodes['tplCurId'].textContent,'code','迟到旧响应不得覆盖最新面板');
 assert.equal(state.currentTemplateDetail&&state.currentTemplateDetail.template.id,'code','currentTemplateDetail 必须属于最新选择');
 assert.equal(dagCalls[dagCalls.length-1],'new-node','DAG 必须属于最新选择');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_library_fallback_reconciles_missing_project_template(self):
        """项目模板缺失时,库视图回落项必须同步为有效选择(Codex P2)。"""
        source = self.html
        block = (source[source.index('function renderTplCards('):
                        source.index('function ensureTplCardShape(')]
                 + '\n'
                 + source[source.index('async function showTemplateLibrary('):
                          source.index('async function showTemplateDAG(')])
        self.run_js(block, r'''
const ts=[{id:'alpha',label:'Alpha',category:'biz',nodes_preview:[{label:'a1'}]},{id:'bidding',label:'投标',category:'biz',nodes_preview:[]}];
const state={spaceId:'w1',shellView:'',templateSelections:{},templates:[],currentTemplateDetail:null,project:{project:{workflow_template:'gone'}}};
const nodes={};
function element(id){return nodes[id]||(nodes[id]={textContent:'',className:'',innerHTML:'',value:''});}
const document={getElementById:element};
function esc(s){return String(s==null?'':s)}
function toast(){}
function setWorkspaceMode(){}
function renderWorkflowTabs(){}
function paintCrumb(){}
function ensureTplLayoutDOM(){}
function saveViewState(){}
async function fetchTemplates(){state.templates=ts;return ts}
function renderTplCategories(){}
function closeTplNodeDrawer(){}
function renderTplDAG(){}
function getTplBadgeClass(){return 'cat-biz'}
async function api(){return {template:{id:'alpha',label:'Alpha'},nodes:[{id:'n1'}]};}
// SOURCE
(async()=>{
 await showTemplateLibrary();
 assert.equal(effectiveTemplateId(),'alpha','项目模板缺失时回落项必须同步为有效选择');
 assert.equal(state.templateSelections['w1'],'alpha','回落选择记入当前空间');
 assert.equal(nodes['tplCurId'].textContent,'alpha','详情面板必须显示回落模板');
 const html=nodes['tplCardsList'].innerHTML;
 const i=html.indexOf('tpl-card selected');
 assert.ok(i>=0&&html.slice(i,i+160).includes("selectTemplateInLibrary('alpha',1)"),'回落模板卡片必须显示为选中');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_save_template_records_to_originating_space(self):
        """保存请求挂起期间切换空间:记账必须归属发起空间(Codex P2)。"""
        source = self.html
        block = (source[source.index('function projectTemplateId('):
                        source.index('function ensureTplCardShape(')]
                 + '\n'
                 + source[source.index('async function saveTemplate('):
                          source.index('async function populateTemplateSelect(')])
        self.run_js(block, r'''
const state={spaceId:'wA',templateSelections:{},templates:[{id:'my-tpl',label:'My',node_count:2}],currentTemplateDetail:null,project:null};
const nodes={};
function element(id){return nodes[id]||(nodes[id]={textContent:'',className:'',innerHTML:'',value:''});}
const document={getElementById:element};
const window={};
element('tplName').value='my-tpl';
element('tplYaml').value='nodes: []';
element('tplCurId');
function toast(){}
function closeTplModal(){}
function closeModal(){}
async function fetchTemplates(){return state.templates}
function renderTplCategories(){}
function renderTplCards(){}
function closeTplNodeDrawer(){}
function renderTplDAG(){}
function getTplBadgeClass(){return 'cat-biz'}
async function api(url,opts){
  if(opts&&opts.method==='POST'){
    await new Promise(r=>setTimeout(r,15));
    state.spaceId='wB'; // 保存挂起期间用户切到空间 B
    return {name:'my-tpl',node_count:2};
  }
  return {template:{id:'my-tpl',label:'My'},nodes:[]};
}
// SOURCE
(async()=>{
 await saveTemplate();
 assert.equal(state.templateSelections['wA'],'my-tpl','保存结果必须记账到发起空间');
 assert.equal(state.templateSelections['wB'],undefined,'切换后的空间不得继承保存结果');
 assert.equal(nodes['tplCurId'].textContent,'','不得改写当前空间的面板显示');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_stale_template_select_population_does_not_override_new_modal(self):
        """弹窗重开时,旧弹窗迟到的模板填充不得覆盖新弹窗的选择(Codex P2)。"""
        source = self.html
        block = source[source.index('async function populateTemplateSelect('):
                       source.index('let opsTimer=')]
        self.run_js(block, r'''
const state={spaceId:'w1'};
const nodes={};
function element(id){return nodes[id]||(nodes[id]={textContent:'',className:'',innerHTML:'',value:''});}
const document={getElementById:element};
function esc(s){return s}
let fetches=0;
async function fetchTemplates(){
  fetches++;
  const ts=[{id:'bidding',label:'投标',node_count:3},{id:'code',label:'代码',node_count:2}];
  if(fetches===1)return new Promise(r=>setTimeout(()=>r(ts),25));
  return ts;
}
// SOURCE
(async()=>{
 const p1=populateTemplateSelect('newTemplate','bidding');
 const p2=populateTemplateSelect('newTemplate','code');
 await Promise.all([p1,p2]);
 assert.equal(nodes['newTemplate'].value,'code','旧弹窗迟到的填充不得覆盖新弹窗选择');
 assert.ok(nodes['newTemplate'].innerHTML.includes('value="code" selected'),'选项 HTML 必须属于新弹窗');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_direct_templates_view_load_hydrates_project_before_render(self):
        """直开/恢复模板库视图时必须水合项目并按有效模板重绘(Codex P2)。"""
        source = self.html
        block = source[source.index('async function initFromUrlOrState('):
                       source.index("window.addEventListener('popstate'")]
        self.run_js(block, r'''
const calls=[];
const state={dashMode:false,opsMode:false,shellView:'workbench',workflowId:null};
const window={location:{search:'?view=templates'}};
async function api(){throw new Error('unexpected api call')}
async function refreshAll(){calls.push('refreshAll')}
async function showTemplateLibrary(){calls.push('showTemplateLibrary')}
async function showDashboard(){calls.push('showDashboard')}
async function showOpsCenter(){calls.push('showOpsCenter')}
function showShellView(v){calls.push('showShellView:'+v)}
async function loadWorkflow(){}
function toast(){}
// SOURCE
(async()=>{
 await initFromUrlOrState();
 assert.ok(calls.includes('showTemplateLibrary'),'?view=templates 必须渲染模板库');
 assert.ok(calls.includes('refreshAll'),'?view=templates 必须水合项目/空间并重绘,否则有效模板回落默认');
 assert.ok(calls.indexOf('showTemplateLibrary')<calls.indexOf('refreshAll'),'先渲染,水合后再重绘');
 calls.length=0;
 window.location.search='';
 state.shellView='templates';state.workflowId='__templates__';
 await initFromUrlOrState();
 assert.ok(calls.includes('showTemplateLibrary')&&calls.includes('refreshAll'),'恢复模板库视图同样必须水合后重绘');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_populate_template_select_prefers_requested_template(self):
        source = self.html
        block = source[source.index('async function populateTemplateSelect('):
                       source.index('let opsTimer=')]
        self.run_js(block, r'''
const state={spaceId:'w1'};
const elements={newTemplate:{value:'',innerHTML:''}};
const document={getElementById:id=>elements[id]||null};
async function fetchTemplates(){return [{id:'software-development-v1',label:'软件开发',node_count:6},{id:'bidding',label:'投标',node_count:7}]}
function esc(s){return s}
// SOURCE
(async()=>{
 await populateTemplateSelect('newTemplate','bidding');
 assert.equal(elements.newTemplate.value,'bidding','preferred 模板必须被选中');
 await populateTemplateSelect('newTemplate');
 assert.equal(elements.newTemplate.value,'software-development-v1','缺省回落 software-development-v1');
 await populateTemplateSelect('newTemplate','no-such-template');
 assert.equal(elements.newTemplate.value,'software-development-v1','未知模板回落默认');
})().catch(e=>{console.error(e);process.exitCode=1});
''')

    def test_template_library_ui_has_categories_filter_and_search(self):
        self.assertIn("tplCategoryPills", self.html)
        self.assertIn("filterTemplates()", self.html)
        self.assertIn("tplSearchInput", self.html)
        self.assertIn("step-pill", self.html)

    def test_template_library_ui_has_dag_canvas_and_inspector_drawer(self):
        self.assertIn("tplDagCanvas", self.html)
        self.assertIn("tplNodeDrawer", self.html)
        self.assertIn("openTemplateNodeDrawer", self.html)
        self.assertIn("closeTplNodeDrawer", self.html)
        self.assertIn("zoomTplGraph", self.html)
        self.assertIn("fitTplGraph", self.html)

    def test_template_library_ui_has_dual_mode_visual_editor(self):
        self.assertIn("tplConfigModal", self.html)
        self.assertIn("switchTplModalTab", self.html)
        self.assertIn("tplLiveCanvas", self.html)
        self.assertIn("addNewTplStage", self.html)
        self.assertIn("deleteTplStage", self.html)
        self.assertIn("tplStagesEditorList", self.html)

    def test_template_category_customization_and_dynamic_aggregation(self):
        self.assertIn("tplCatDatalist", self.html)
        self.assertIn('list="tplCatDatalist"', self.html)
        self.assertIn("getDynamicTemplateCategories()", self.html)
        self.assertIn("getTplBadgeClass(", self.html)
        self.assertIn(".tpl-badge.cat-purple", self.html)
        self.assertIn(".tpl-badge.cat-emerald", self.html)
        self.assertIn(".tpl-badge.cat-rose", self.html)


class TestTermTranslation(unittest.TestCase):
    """用户确认的术语映射：Tab=工作流节点、Pane=智能体工位、Agent=执行者、Task=任务、Workspace=项目空间、Workflow=工作流。"""

    @classmethod
    def setUpClass(cls):
        cls.html = load_console().HTML

    def test_confirmed_terms_are_translated(self):
        for snippet in (
            'data-nav="workbench"',
            ">执行者阵容<",
            ">常驻智能体工位<",
            "执行者舰队",
            ">执行者自检<",
            ">指定执行者<",
            "个工作流节点 · ",
            "个智能体工位",
            "工作流 / 阶段节点<",
            "<span>项目空间</span>",
            "<span>活跃工作流</span>",
            ">启动工作流<",
            "<label>工作流</label>",
        ):
            self.assertIn(snippet, self.html, f"missing translation: {snippet}")

    def test_english_terms_are_gone_from_visible_copy(self):
        content = self.html.replace("让人和多个 AI Agent 一起把事情做完", "")
        for term in ("Agent ", "Task ", "Pane ", "Tab ", "Herdr 空间", "活跃 Workflow"):
            self.assertNotIn(term, content, f"untranslated term remains: {term!r}")


if __name__ == "__main__":
    unittest.main()
