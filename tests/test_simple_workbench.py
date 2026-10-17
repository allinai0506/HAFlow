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
