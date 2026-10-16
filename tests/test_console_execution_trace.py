"""Task drawer execution-trace tab."""

import shutil
import subprocess

import pytest

from console import herdr_factory_console as console


def _extract(html, name):
    start = html.find(f"function {name}(")
    assert start >= 0, name
    if html[max(0, start - 6):start] == "async ":
        start -= 6
    brace = html.find("{", start)
    depth = 0
    for index in range(brace, len(html)):
        if html[index] == "{":
            depth += 1
        elif html[index] == "}":
            depth -= 1
            if depth == 0:
                return html[start:index + 1]
    raise AssertionError(name)


def test_trace_tab_keeps_the_other_tabs_and_has_no_sample_data():
    html = console.HTML_TEMPLATE
    assert ">执行轨迹</button>" in html
    assert ">概览</button>" in html
    assert ">产物</button>" in html
    assert ">运行时</button>" in html
    assert ">活动</button>" not in html
    assert "id=\"tdTabActivity\"" in html
    assert "/api/task/trajectory" in html
    assert "renderTaskOverview" in html
    assert "renderTaskArtifacts" in html
    assert "renderTaskRuntime" in html
    assert "暂无可用活动记录" in html
    assert "src/order.py" not in html
    assert "实现订单接口" not in html
    assert "state.taskTrace=null" in html
    assert "taskTraceSeq" in html


def test_trace_renderer_states_escape_and_stale_response():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    html = console.HTML_TEMPLATE
    names = [
        "esc", "taskDrawerNumTs", "traceClock", "traceDotClass", "traceDetailRows",
        "renderExecutionTrace", "applyTraceResponse", "toggleTraceEvent", "loadMoreTaskTrace",
    ]
    script = "\n".join(_extract(html, name) for name in names)
    script += r"""
const assert=require('node:assert/strict');
const sample={
  status:'ready', availability:'agent_actions', has_more:true,
  events:[
    {id:'1', kind:'file_read', title:'读取项目文件', detail:'Read · src/billing.py', status:'neutral', timestamp:1700000020, agent:'claude', source:'native_session', source_ref:'claude:s:1', details:{tool:'Read', path:'src/billing.py', source:'native_session', source_ref:'claude:s:1'}},
    {id:'2', kind:'file_edit', title:'修改代码', detail:'Edit · src/billing.py', status:'info', timestamp:1700000030, source:'native_session', source_ref:'claude:s:2'},
    {id:'3', kind:'test_failed', title:'<script>alert(1)</script>', detail:'"><img src=x onerror=alert(1)>', status:'error', timestamp:1700000040, source:'native_session', source_ref:'claude:s:3', details:{command:'pytest', exit_code:1}},
    {id:'4', kind:'test_passed', title:'测试通过', detail:'pytest · exit code 0', status:'success', timestamp:1700000050, source:'native_session', source_ref:'claude:s:4'},
    {id:'5', kind:'commit', title:'生成提交', detail:'Commit · 9d41c07', status:'neutral', timestamp:null, source:'task_history', source_ref:'task:a:commit'}
  ]
};
const view=renderExecutionTrace(sample);
assert.match(view, /\d{2}:\d{2}:\d{2}/);
assert.equal(view.includes('读取项目文件'), true);
assert.equal(view.includes('Read · src/billing.py'), true);
assert.equal(view.includes('td-dot neutral'), true);
assert.equal(view.includes('td-dot edit'), true);
assert.equal(view.includes('td-dot err'), true);
assert.equal(view.includes('td-ev-title err'), true);
assert.equal(view.includes('td-dot ok'), true);
assert.equal(view.includes('<script>'), false);
assert.equal(view.includes('&lt;script&gt;'), true);
assert.equal(view.includes('<img'), false);
assert.equal(view.includes('加载更多'), true);
assert.equal(view.includes('以下记录没有原始时间'), true);
assert.equal(view.includes('src/order.py'), false);
const extra={hidden:true};
toggleTraceEvent({querySelector:()=>extra});
assert.equal(extra.hidden, false);
assert.equal(renderExecutionTrace(null).includes('正在加载执行轨迹'), true);
assert.equal(renderExecutionTrace({status:'error', message:'超时'}).includes('执行轨迹加载失败：超时'), true);
assert.equal(renderExecutionTrace({status:'error', message:'超时'}).includes('重试'), true);
assert.equal(renderExecutionTrace({status:'ready', availability:'none', events:[]}).includes('当前任务暂无可用执行轨迹。'), true);
assert.equal(renderExecutionTrace({status:'ready', availability:'task_events', events:[{id:'t', title:'任务已创建', status:'neutral', timestamp:10, kind:'task_created'}]}).includes('当前仅有任务状态记录，尚无可验证的执行者操作明细。'), true);
assert.equal(renderExecutionTrace({status:'ready', availability:'none', events:[], degradation:[{code:'native_unavailable', message:'记录来源暂不可用。'}]}).includes('记录来源暂不可用。'), true);
const state={selectedTaskId:'task-b', taskTraceSeq:2, taskTrace:{events:[{id:'keep'}]}};
assert.equal(applyTraceResponse(state,'task-a',1,{task_id:'task-a', events:[{id:'old', title:'旧'}], availability:'agent_actions'}), false);
assert.equal(state.taskTrace.events[0].id, 'keep');
assert.equal(applyTraceResponse(state,'task-b',2,{task_id:'task-b', availability:'task_events', events:[{id:'new'}], has_more:false}), true);
assert.deepEqual(state.taskTrace.events.map(e=>e.id), ['new']);
state.selectedTaskId='task-b';
state.taskTraceSeq=2;
state.taskDrawerTab='trace';
state.taskTrace={status:'ready', task_id:'task-b', run_id:'run-b', availability:'task_events', events:[{id:'keep', title:'已有事件', status:'neutral', timestamp:10}], has_more:true, next_cursor:'keep', degradation:[]};
async function api(){ throw new Error('网络中断'); }
function renderTaskDrawer(){}
loadMoreTaskTrace().then(()=>{
  const html=renderExecutionTrace(state.taskTrace);
  assert.equal(state.taskTrace.status, 'ready');
  assert.equal(state.taskTrace.events[0].id, 'keep');
  assert.equal(state.taskTrace.appendError, '网络中断');
  assert.equal(html.includes('已有事件'), true);
  assert.equal(html.includes('后续轨迹加载失败：网络中断'), true);
  assert.equal(html.includes('loadMoreTaskTrace()'), true);
  assert.equal(html.includes('执行轨迹加载失败'), false);
}).catch((err)=>{ console.error(err); process.exit(1); });
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
