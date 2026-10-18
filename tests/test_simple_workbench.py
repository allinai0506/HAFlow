"""Real HTTP contracts for the simple workbench; external terminal is isolated."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from console import herdr_factory_console as c
from herdr import projects


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), c.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    yield f'http://127.0.0.1:{httpd.server_port}'
    httpd.shutdown()
    httpd.server_close()
    worker.join()


def test_simple_page_and_project_reload(server, tmp_path, monkeypatch):
    registry = tmp_path / 'projects.json'
    registry.write_text(json.dumps({'projects': {'/business': {
        'project_id': 'p-business', 'project_root': '/business', 'project_name': '业务'
    }}}))
    monkeypatch.setattr(c, 'PROJECTS_FILE', registry)
    with urllib.request.urlopen(server + '/simple') as response:
        page = response.read().decode()
    assert '新建项目' in page and '新建需求' in page and '获得成果' in page
    for _ in range(2):
        with urllib.request.urlopen(server + '/api/projects') as response:
            assert json.load(response)['data'][0]['project_id'] == 'p-business'


def test_create_context_project_accepts_plain_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, 'project_by_root', lambda root: None)
    called = []
    monkeypatch.setattr(projects, 'provision_project', lambda root, **kw: called.append((root, kw)) or {'project_root': root})
    record = projects.create_project(str(tmp_path), template_name='business-task-v1')
    assert record['project_root'] == str(tmp_path)
    assert called[0][1]['template_name'] == 'business-task-v1'


def test_business_template_has_real_context_contract():
    data = c.template_detail('business-task-v1')
    assert data['execution_mode'] == 'context'
    assert data['context']['required'] == []
    assert [n['id'] for n in data['nodes']] == ['scope', 'execute', 'deliver']


def test_run_workflow_passes_context_to_real_cli(tmp_path, monkeypatch):
    calls = []
    def run(command, timeout):
        calls.append(command)
        return type('Result', (), {'returncode': 0, 'stdout': 'WORKFLOW_ID=wf-real\n', 'stderr': ''})()
    monkeypatch.setattr(c, 'run', run)
    c.run_workflow(str(tmp_path), '分析', template='context-smoke-test', context={'workspace': str(tmp_path), 'common': str(tmp_path)})
    assert calls[0].count('--context') == 2
    assert f'workspace={tmp_path}' in calls[0]


def test_real_http_create_start_decide_and_download(tmp_path, monkeypatch):
    import contextlib
    import importlib.util
    import time
    from pathlib import Path

    from herdr import state_db
    spec = importlib.util.spec_from_file_location('simple_sandbox', Path(__file__).parent / 'support/simple_workbench_sandbox.py')
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    with contextlib.ExitStack() as stack:
        console, _ = sandbox.configure(tmp_path, stack)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), console.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        stack.callback(worker.join)
        stack.callback(httpd.server_close)
        stack.callback(httpd.shutdown)
        base = f'http://127.0.0.1:{httpd.server_port}'
        def request(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            with urllib.request.urlopen(urllib.request.Request(base+path, data=data, headers={'Content-Type':'application/json'})) as response:
                raw = response.read()
                if 'application/json' in response.headers.get('Content-Type', ''):
                    return json.loads(raw)['data']
                return raw
        business = tmp_path / 'business'
        business.mkdir()
        project = request('/api/project/create', {'project_root':str(business), 'project_name':'普通业务', 'template':'business-task-v1'})
        assert request('/api/workflows?project_id='+project['project_id']) == []
        assert not (business / '.git').exists()
        assert request('/api/projects')[0]['project_id'] == project['project_id']
        # Loading from disk rather than a cached object must preserve ownership.
        console.projects()[0]['project_name'] = 'not saved'
        assert request('/api/projects')[0]['project_name'] == '普通业务'
        job = request('/api/run', {'project_root':str(business), 'requirement':'分析项目资料', 'title':'真实分析', 'template':'business-task-v1'})
        for _ in range(100):
            status = request('/api/run/status?id='+job['job_id'])
            if status['status'] != 'running':
                break
            time.sleep(.02)
        assert status['status'] == 'succeeded', status
        import re
        wid = re.search(r'^WORKFLOW_ID=(\S+)', status['output'], re.MULTILINE)[1]
        workflows = request('/api/workflows?project_id='+project['project_id'])
        assert workflows[0]['workflow_id'] == wid
        detail = request('/api/workflow?id='+wid+'&lightweight=1')
        assert detail['workflow']['status'] == 'running'
        assert detail['workflow']['startup_ready'] is True
        duplicate = request('/api/run', {'project_root':str(business), 'requirement':'重复启动', 'template':'business-task-v1'})
        for _ in range(100):
            duplicate_status = request('/api/run/status?id='+duplicate['job_id'])
            if duplicate_status['status'] != 'running':
                break
            time.sleep(.02)
        assert duplicate_status['status'] == 'failed'
        assert '已有活跃 Workflow' in duplicate_status['error']
        assert [s['key'] for s in detail['stages']] == ['scope', 'execute', 'deliver']
        request('/api/workflow/decision/raise', {'workflow_id':wid, 'decision_id':'scope-choice', 'question':'是否包含附录？', 'options':['包含','不包含']})
        assert request('/api/workflow/decisions?workflow_id='+wid)['decisions'][0]['question'] == '是否包含附录？'
        request('/api/workflow/decision', {'workflow_id':wid, 'decision_id':'scope-choice', 'decision':'包含附录'})
        assert request('/api/workflow/decisions?workflow_id='+wid)['decisions'] == []
        workspace = tmp_path / 'clones' / 'analysis'
        workspace.mkdir(parents=True)
        (workspace / 'analysis.md').write_text('# 真实成果\n分析结论。')
        state_db.save_task({'task_id':'analysis', 'workflow_id':wid, 'project_id':project['project_id'], 'stage':'execute', 'node':'execute', 'status':'completed', 'clone_path':str(workspace), 'deliverables':['analysis.md']}, db_path=tmp_path / 'state.db')
        items = request('/api/workflow/deliverables?workflow_id='+wid)['items']
        item = next(row for row in items if row.get('name') == 'analysis.md')
        url = '/api/workflow/deliverable?'+urllib.parse.urlencode({'workflow_id':wid, 'task_id':'analysis', 'id':item['id']})
        assert '真实成果' in request(url)['text']
        assert request(url+'&download=1') == (workspace / 'analysis.md').read_bytes()
        with pytest.raises(urllib.error.HTTPError) as error:
            request(url.replace('task_id=analysis', 'task_id=foreign'))
        assert error.value.code == 404


def test_run_rejects_empty_root_before_starting(server, monkeypatch):
    started = []
    monkeypatch.setattr(c, 'start_workflow_job', lambda *a, **kw: started.append(a) or {})
    request = urllib.request.Request(server+'/api/run', data=b'{"requirement":"work"}', headers={'Content-Type':'application/json'})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request)
    assert not started


@pytest.mark.parametrize('refresh_failed', [False, True])
def test_started_workflow_uses_actual_owner_after_git_root_normalization(refresh_failed):
    """The selected business subdirectory can resolve to a different Git project."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    source = (Path(__file__).parents[1] / 'console/static/simple.js').read_text()
    source = source[source.index('let polling=false;'):source.index("$('newProject').onclick")]
    harness = """
const assert=require('node:assert/strict');
const model={job:{id:'job-1',projectId:'business-child'},projectId:'business-child'};
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,{close(){},textContent:'',disabled:false});return elements.get(id)};
const sessionStorage={removeItem(){}};
const formError=()=>{};
let message='';
const notice=value=>{message=value};
let refreshed=false;
const selectProject=async()=>{};
const refreshProjects=async()=>{refreshed=true;elements.set('notice',{hidden:!REFRESH_FAILED})};
const api=async url=>url.startsWith('/api/run/status')?{status:'succeeded',output:'WORKFLOW_ID=wf-actual'}:{workflow:{project_id:'git-parent'}};
""" + source + """
(async()=>{await pollJob();assert.equal(model.projectId,'git-parent');assert.equal(model.workflowId,'wf-actual');assert.ok(refreshed);assert.equal(message.includes('需求已启动'),!REFRESH_FAILED)})().catch(e=>{console.error(e);process.exitCode=1});
"""
    harness = harness.replace('REFRESH_FAILED', 'true' if refresh_failed else 'false')
    result = subprocess.run([node, '-e', harness], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def _run_simple_js(case):
    """Execute production JS with only DOM/transport boundaries replaced."""
    import subprocess
    from pathlib import Path
    source = (Path(__file__).parents[1] / 'console/static/simple.js').read_text()
    source = source[:source.rindex('refreshProjects();')]
    harness = r"""
const assert=require('node:assert/strict');
const elements=new Map();
class Element {
 constructor(){this.value='';this.textContent='';this.innerHTML='';this.hidden=false;this.disabled=false;this.children=[];this.dataset={};}
 replaceChildren(...items){this.children=items;this.value='';}
 add(o){this.children.push(o);if(this.children.length===1)this.value=o.value;}
 append(o){this.children.push(o);}
 querySelector(){return new Element()}
 querySelectorAll(){return []}
 showModal(){} close(){} focus(){document.activeElement=this} scrollIntoView(){}
}
const document={getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id)},createElement(){return new Element()},querySelectorAll(){return []}};
const Option=function(text,value){this.text=text;this.value=value};
const location={search:''};const history={replaceState(){}};
const localStorage={getItem(){return null},setItem(){}};const sessionStorage={getItem(){return null},setItem(){}};
const setInterval=()=>{};const setTimeout=()=>{};
let responses={};
const fetch=async url=>({ok:true,json:async()=>({data:typeof responses[url]==='function'?await responses[url]():responses[url]})});
"""
    result = subprocess.run(['node', '-e', harness + source + '\n(async()=>{' + case + '\n})().catch(e=>{console.error(e);process.exitCode=1});'], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_r2_template_summary_exposes_mode():
    for item in c.templates_summary()['templates']:
        assert item['execution_mode'] == c.template_detail(item['id'])['execution_mode']


def test_r2_project_template_roundtrip_and_search():
    _run_simple_js(r"""
model.projects=[{project_id:'A',execution:{mode:'context'}},{project_id:'B'}];
const templates=[{id:'contract',label:'合同审阅',description:'条款风险',category:'legal',execution_mode:'context'}, {id:'analysis',label:'分析',description:'经营复盘',category:'biz',execution_mode:'context'}, {id:'code',label:'开发',description:'代码审查',category:'eng',execution_mode:'git'}];
responses['/api/templates']={templates};
for(const t of templates)responses['/api/template?id='+t.id]={template:t,nodes:[],context:{},execution_mode:t.execution_mode};
for(const id of ['A','B'])responses['/api/workflows?project_id='+id]=[];
$('project').value='A';await selectProject();await openRequirement();
$('category').value='legal';$('category').onchange();await loadTemplate();
assert.equal($('template').value,'contract');
$('project').value='B';await selectProject();await openRequirement();
assert.equal($('category').value,'');assert.equal($('template').value,'code');
$('project').value='A';await selectProject();await openRequirement();
assert.equal($('template').value,'contract');assert.equal($('category').value,'legal');
$('category').value='';$('templateSearch').value='经营';$('templateSearch').oninput();await loadTemplate();
assert.deepEqual($('template').children.map(x=>x.value),['analysis']);
$('templateSearch').value='合同';$('templateSearch').oninput();await loadTemplate();
assert.deepEqual($('template').children.map(x=>x.value),['contract']);
$('category').value='biz';$('category').onchange();await loadTemplate();assert.equal($('template').value,'');assert.equal($('start').disabled,true);
""")


def test_r2_late_template_response_cannot_enable_other_project():
    _run_simple_js(r"""
model.projects=[{project_id:'A'},{project_id:'B'}];model.projectId='A';
$('template').value='old';let finish;
responses['/api/template?id=old']=()=>new Promise(r=>{finish=r});
const pending=loadTemplate();await Promise.resolve();
responses['/api/workflows?project_id=B']=[];
$('project').value='B';await selectProject();
finish({template:{label:'OLD'},nodes:[],context:{required:[{id:'secret'}]}});await pending;
assert.equal($('start').disabled,true);assert.equal($('contextInputs').children.length,0);assert.ok(!$('templateInfo').textContent.includes('OLD'));
""")


def test_r2_execution_shows_actual_template_and_aggregates_other_requirements():
    _run_simple_js(r"""
model.projects=[{project_id:'A',project_name:'真实项目'}];
responses['/api/workflows?project_id=A']=[{workflow_id:'one',title:'需求一',updated_at:1700000000},{workflow_id:'two',title:'需求二',updated_at:1700000100}];
for(const wid of ['one','two'])responses['/api/workflow/decisions?workflow_id='+wid]={decisions:[{decision_id:wid,question:'裁决'+wid}]};
responses['/api/workflow?id=one&lightweight=1']={workflow:{project_id:'A',title:'需求一',template:'actual-frozen'},stages:[],tasks:[]};
loadDecisions=async()=>{};loadResults=async()=>{};
$('project').value='A';await selectProject();
assert.ok($('workflowTemplate').textContent.includes('actual-frozen'));
assert.equal($('attentionList').children.length,2);
assert.ok($('attentionList').children[1].textContent.includes('需求二'));
await $('attentionList').children[1].onclick();assert.equal(model.workflowId,'two');
""")


def test_r2_context_url_without_searchparams_size():
    _run_simple_js(r"""
Object.defineProperty(URLSearchParams.prototype,'size',{get(){return undefined}});
model.projectId='project A';model.workflowId='w';assert.ok(contextUrl('/').includes('project_id=project+A'));
assert.equal(contextUrl('/'),'/?project_id=project+A&workflow_id=w');
""")


def test_r2_null_submitter_still_creates_project():
    _run_simple_js(r"""
responses['/api/project/create']={project_id:'new'};refreshProjects=async()=>{};
await $('projectForm').onsubmit({preventDefault(){},target:$('projectForm'),submitter:null});
assert.equal(model.projectId,'new');
""")


def test_r2_limits_are_explicit():
    from pathlib import Path
    root = Path(__file__).parents[1]
    page = (root / 'console/static/simple.html').read_text()
    doc = (root / 'docs/walkthroughs/20261017-simple-workbench-v1.md').read_text()
    for text in (page, doc):
        assert '仅支持已存在目录' in text
    # This implementation limitation belongs in the technical record, not the UI.
    assert '项目目标/简介采集与持久化尚未实现' in doc
    assert 'business-task-v1' not in page
    assert 'software-development-v1' not in page


def test_project_selection_reads_only_selected_project_without_duplicate_overview():
    _run_simple_js(r"""
responses['/api/projects']=[{project_id:'A',project_name:'项目甲'},{project_id:'B',project_name:'项目乙'}];
responses['/api/workflows?project_id=A']=[];
let otherRead=false;responses['/api/workflows?project_id=B']=()=>{otherRead=true;throw Error('不应读取其他项目')};
await refreshProjects();
assert.deepEqual($('project').children.map(x=>x.text),['项目甲','项目乙']);
assert.equal(model.projectId,'A');assert.equal(otherRead,false);
""")


def test_r2_late_attention_response_is_not_shown_in_other_project():
    _run_simple_js(r"""
model.projectId='A';let finish;
responses['/api/workflow/decisions?workflow_id=one']=()=>new Promise(r=>{finish=r});
const pending=loadAttention([{workflow_id:'one',title:'项目甲秘密需求'}],'A');await Promise.resolve();
model.projectId='B';await loadAttention([],'B');
finish({decisions:[{question:'不能混入乙项目'}]});await pending;
assert.equal($('attentionList').children.length,0);assert.ok(!$('attentionList').textContent.includes('不能混入'));
""")


@pytest.mark.parametrize('term,category,want', [('合同','','contract'), ('风险','','contract'), ('风险','biz','')])
def test_r2_search_name_and_scenario_preserves_category(term, category, want):
    _run_simple_js("""
model.templates=[{id:'contract',label:'合同审阅',description:'条款风险',category:'legal'},{id:'analysis',label:'经营分析',description:'经营复盘',category:'biz'}];
$('templateSearch').value=TERM;$('category').value=CATEGORY;populateTemplates();
assert.equal($('template').value,WANT);assert.deepEqual($('template').children.map(x=>x.value),[WANT]);
""".replace('TERM', json.dumps(term)).replace('CATEGORY', json.dumps(category)).replace('WANT', json.dumps(want)))


def test_r2_new_project_templates_come_from_real_metadata():
    _run_simple_js(r"""
responses['/api/templates']={templates:[{id:'custom-only',label:'自定义业务',execution_mode:'context'}]};
await $('newProject').onclick();
assert.deepEqual($('projectType').children.map(x=>[x.value,x.text]),[['custom-only','业务目录 · 自定义业务']]);
""")


def test_result_summary_bounds_files_and_keeps_all_history_and_errors():
    _run_simple_js(r"""
const file=i=>({id:'f'+i,task_id:'t',name:'成果'+i+'.md',readable:true,actions:['view','download']});
responses['/api/workflow/deliverables?workflow_id=w']={items:[{id:'commit',name:'提交记录',actions:[]},...Array.from({length:5},(_,i)=>file(i)),{id:'old',name:'旧成果',readable:false,reason:'任务已替代',actions:[]}],error:'部分工作区不可读',truncated:true,notice:'列表截断'};
model.sequence=1;await loadResults('w',1);
const children=$('resultList').children;
assert.equal(children.filter(c=>c.className==='result').length,3);
const history=children.find(c=>c.className==='resultHistory');assert.ok(history);
assert.notEqual(history.open,true);
assert.equal(history.children.filter(c=>c.className==='result').length,4);
assert.ok(children.some(c=>c.textContent.includes('部分工作区不可读')));
assert.ok(children.some(c=>c.textContent.includes('列表截断')));
assert.equal($('resultCount').textContent,'5 项可用成果 · 7 项记录');
""")


def test_result_summary_does_not_promote_unreadable_or_stale_records():
    _run_simple_js(r"""
model.sequence=2;
responses['/api/workflow/deliverables?workflow_id=w']={items:[{name:'不可读取',readable:false,actions:['view']},{name:'提交',actions:[]}]};
await loadResults('w',2);
assert.equal($('resultList').children.filter(c=>c.className==='result').length,0);
assert.ok($('resultList').children.some(c=>c.className==='resultHistory'));
const before=$('resultCount').textContent;await loadResults('w',1);assert.equal($('resultCount').textContent,before);
""")


def test_result_summary_includes_download_only_documents_and_safe_links():
    _run_simple_js(r"""
model.sequence=1;
responses['/api/workflow/deliverables?workflow_id=w']={items:[{name:'报告.pdf',readable:true,actions:['download']},{name:'PR',actions:['open'],url:'https://example.com/pr/1'},{name:'无效链接',actions:['open'],url:'javascript:alert(1)'}]};
await loadResults('w',1);
assert.equal($('resultCount').textContent,'2 项可用成果 · 3 项记录');
assert.equal($('resultList').children.filter(c=>c.className==='result').length,2);
""")


def test_template_search_no_match_keeps_keyboard_focus():
    _run_simple_js(r"""
model.projectId='A';$('template').value='';$('templateSearch').focus();
await loadTemplate();
assert.equal(document.activeElement,$('templateSearch'));
assert.equal($('start').disabled,true);
""")


def test_escape_in_template_search_closes_dialog_without_clearing_draft():
    _run_simple_js(r"""
$('title').value='保留标题';$('requirement').value='保留需求';
let closed=false,prevented=false;$('requirementDialog').close=()=>{closed=true};
assert.equal(typeof $('templateSearch').onkeydown,'function');
$('templateSearch').onkeydown({key:'Escape',preventDefault(){prevented=true}});
assert.equal(closed,true);assert.equal(prevented,true);
assert.equal($('title').value,'保留标题');assert.equal($('requirement').value,'保留需求');
""")


def test_switching_workflow_removes_previous_decision_before_new_response():
    _run_simple_js(r"""
model.projectId='A';$('workflow').value='new';$('decisions').textContent='旧需求的裁决';
let finish;responses['/api/workflow?id=new&lightweight=1']=()=>new Promise(r=>{finish=r});
const pending=loadWorkflow();await Promise.resolve();
assert.ok(!$('decisions').textContent.includes('旧需求'));
finish({workflow:{project_id:'wrong-project'}});await pending;
assert.ok(!$('decisions').textContent.includes('旧需求'));
$('workflow').value='';await loadWorkflow();assert.ok(!$('decisions').textContent.includes('正在读取'));
""")


def test_shared_console_palette_revalidates_after_release_changes(server):
    with urllib.request.urlopen(server + '/static/console-tokens.css') as response:
        assert response.headers['Cache-Control'] == 'no-cache'
        assert response.headers['Content-Type'].startswith('text/css')
