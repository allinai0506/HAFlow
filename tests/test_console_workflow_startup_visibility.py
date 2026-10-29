import importlib.machinery
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative_path):
    path = ROOT / relative_path
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("WORKFLOWS_FILE", str(tmp_path / "workflows.json"))
    from herdr.state_store import SQLiteStateStore, reset_state_store

    reset_state_store()
    store = SQLiteStateStore(tmp_path / "state.db")
    store.save_workflow({"workflow_id": "wf-startup-pending", "project_id": "p1", "status": "running", "startup_ready": False, "title": "", "requirement": "用户看不到的有效运行内容", "created_at": 1_000_000.0})
    store.save_workflow({"workflow_id": "wf-startup-failed", "project_id": "p1", "status": "completed", "startup_ready": False, "outcome": "startup_failed", "failed_stage": "preflight", "startup_failure_reason": "<script>alert('x')</script>" + "x" * 5000, "title": "", "requirement": "启动失败的需求", "created_at": 1_000_001.0})
    store.save_workflow({"workflow_id": "wf-old-record", "project_id": "p1", "status": "completed", "title": "已有标题", "requirement": "普通需求", "created_at": 999_999.0})
    yield _load("console_read_api_startup", "console/read_api.py")
    reset_state_store()


def test_workflow_list_exposes_bounded_startup_fields_and_title_fallback(console):
    items = {item["workflow_id"]: item for item in console.api_workflows("p1")}
    pending = items["wf-startup-pending"]
    assert pending["startup_ready"] is False
    assert pending["outcome"] == ""
    assert pending["failed_stage"] == ""
    assert pending["startup_failure_reason"] == ""
    assert pending["title"] == ""
    assert pending["requirement_subject"] == "用户看不到的有效运行内容"
    assert items["wf-old-record"]["startup_ready"] is True
    failed = items["wf-startup-failed"]
    assert failed["outcome"] == "startup_failed"
    assert failed["failed_stage"] == "preflight"
    assert 0 < len(failed["startup_failure_reason"]) <= 2048


def test_workflow_detail_matches_list_semantics(console):
    workflow = console.workflow_detail("wf-startup-failed", lightweight=True)["workflow"]
    assert workflow["outcome"] == "startup_failed"
    assert workflow["failed_stage"] == "preflight"
    assert workflow["startup_ready"] is False
    assert 0 < len(workflow["startup_failure_reason"]) <= 2048


def _run_node(script, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    path = tmp_path / "workflow-visibility.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_presentation_workflow_labels_escape_reason_and_keep_task_labels(tmp_path):
    source = (ROOT / "console/static/console/scripts/presentation.js").read_text(encoding="utf-8")
    start = source.index("/* workflow-status:start */")
    end = source.index("/* workflow-status:end */") + len("/* workflow-status:end */")
    _run_node("const humanStatus = s => ({running:'运行中', completed:'待收尾'})[s] || s;\n" + source[start:end] + """
const cases = [[{status:'running', startup_ready:false},'启动中（尚未就绪）'],[{status:'completed',outcome:'startup_failed'},'启动失败'],[{status:'completed',outcome:'abandoned'},'已放弃'],[{status:'running',startup_ready:true},'运行中']];
for (const [item, expected] of cases) if (workflowStatusLabel(item) !== expected) process.exit(1);
if (workflowStatusLabel({status:'completed'}) !== '待收尾') process.exit(2);
const hint=workflowStartupHint({workflow_id:'wf-x',status:'completed',outcome:'startup_failed',failed_stage:'<x>',startup_failure_reason:'<script>x</script>'});
if (hint.includes('<script>')) process.exit(3);
if (!hint.includes('herdr-task close-workflow wf-x --abandon')) process.exit(4);
if (humanStatus('completed') !== '待收尾') process.exit(5);
""", tmp_path)


def test_simple_workflow_renderer_uses_workflow_status_and_escapes_hint(tmp_path):
    source = (ROOT / "console/static/simple.js").read_text(encoding="utf-8")
    start = source.index("/* workflow-status:start */")
    end = source.index("/* workflow-status:end */") + len("/* workflow-status:end */")
    _run_node(source[start:end] + """
if (workflowLabel({status:'running',startup_ready:false}) !== '启动中（尚未就绪）') process.exit(1);
if (workflowLabel({status:'completed',outcome:'startup_failed'}) !== '启动失败') process.exit(2);
if (workflowLabel({status:'completed',outcome:'abandoned'}) !== '已放弃') process.exit(3);
const hint=workflowHint({workflow_id:'wf-y',status:'completed',outcome:'startup_failed',startup_failure_reason:'<img src=x>'});
if (hint.includes('<img')) process.exit(4);
if (!hint.includes('herdr-task close-workflow wf-y --abandon')) process.exit(5);
""", tmp_path)


def test_sidebar_workflow_renderer_uses_workflow_status(tmp_path):
    source = (ROOT / "console/static/console/scripts/sidebar-tabs.js").read_text(encoding="utf-8")
    start = source.index("function renderWorkflowTabs(){")
    end = source.index("function setWorkflowTabsRenderer", start)
    _run_node("""
const box={innerHTML:'',dataset:{}};
const document={getElementById:id=>id==='workflowTabsList'?box:null};
const state={openWorkflowTabIds:['wf-x'],workflowId:'wf-x',project:{workflows:[{workflow_id:'wf-x',status:'completed',outcome:'abandoned',title:'<unsafe>'}]}};
const workflowSubject=w=>w.title||w.requirement_subject||'';
const esc=v=>String(v).replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
const workflowStatusLabel=w=>w.outcome==='abandoned'?'已放弃':w.status;
const workflowStatusTone=w=>w.outcome==='abandoned'?'danger':'completed';
const workflowStartupHint=w=>'';
""" + source[start:end] + """
renderWorkflowTabs();
if (!box.innerHTML.includes('已放弃')) process.exit(1);
if (box.innerHTML.includes('<unsafe>')) process.exit(2);
""", tmp_path)
