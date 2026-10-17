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
    paren = html.index("(", start)
    depth = 0
    for index in range(paren, len(html)):
        if html[index] == "(":
            depth += 1
        elif html[index] == ")":
            depth -= 1
            if depth == 0:
                break
    brace = html.find("{", index)
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


def test_trajectory_api_uses_authoritative_state_db_path(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    monkeypatch.delenv("HERDR_STATE_DB", raising=False)
    monkeypatch.setenv("TASKS_FILE", str(tmp_path / "tasks.json"))
    seen = {}

    def fake_read_trace_tasks(path, tid):
        seen["path"] = path
        raise FileNotFoundError(path)

    from herdr import execution_trace

    monkeypatch.setattr(execution_trace, "read_trace_tasks", fake_read_trace_tasks)
    with pytest.raises(console.TraceQueryError) as caught:
        console.api_task_trajectory("task-a")
    assert caught.value.status == 503
    assert str(seen["path"]) == str(db)


def test_polling_and_icons_are_wired_in_the_console():
    html = console.HTML_TEMPLATE
    assert "state.taskTracePoll=null" in html
    assert "function startTaskTracePoll()" in html
    assert "function stopTaskTracePoll()" in html
    assert "function pollTaskTrace()" in html
    assert "function traceTaskWorking()" in html
    assert "setInterval(pollTaskTrace,8000)" in html
    assert "['working','dispatched','rework'].includes(t.status)" in html
    poll_body = html.split("function pollTaskTrace(){")[1].split("function startTaskTracePoll")[0]
    assert "refreshSelectedTaskStatus" in poll_body
    assert "document.hidden" in poll_body
    # Codex P1：终态分支停表前必须先做最后一次轨迹拉取
    assert "loadTaskTrace" in poll_body.split("cleanup_ready")[0].split("stopTaskTracePoll();return")[-1]
    refresh_body = html.split("async function refreshSelectedTaskStatus(){")[1].split("\nfunction ")[0]
    assert "api('/api/task?id='" in refresh_body
    assert "else stopTaskTracePoll()" in html
    assert "stopTaskTracePoll();" in html.split("function closeTaskDrawer(){")[1].split("function switchTaskDrawerTab")[0]
    for glyph in ("file_read:'▤'", "file_edit:'✎'", "test_passed:'✓'", "test_failed:'✗'", "retry:'↻'", "commit:'⊂⊃'"):
        assert glyph in html
    assert "const clock=hasTime?traceClock(e.timestamp):'时间未知'" in html


def test_trace_poll_guard_reads_live_task_status_from_fetch_boundary():
    # F4 regression: the poll decision must come from the live /api/task chain,
    # not the snapshot frozen at drawer open. Only the network fetch is stubbed.
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    html = console.HTML_TEMPLATE
    names = [
        "api", "applyTraceResponse", "loadTaskTrace", "traceTaskWorking",
        "stopTaskTracePoll", "pollTaskTrace", "startTaskTracePoll",
        "refreshSelectedTaskStatus",
    ]
    script = "\n".join(_extract(html, name) for name in names)
    script += r"""
const assert=require('node:assert/strict');
globalThis.performance={now:()=>0};
globalThis.sessionStorage={getItem:()=>null};
globalThis.document={getElementById:()=>null,hidden:false};
const apiReads=new Map();
let taskStatus='pending';
let statusCalls=0, traceCalls=0, cleared=0;
const intervals=[];
function setInterval(fn,ms){intervals.push({fn,ms});return intervals.length}
function clearInterval(id){cleared+=1}
function renderTaskDrawer(){}
async function fetch(p){
  if(p.startsWith('/api/task/trajectory')){
    traceCalls+=1;
    return {ok:true,status:200,json:async()=>({data:{task_id:'task-a',run_id:'run-a',availability:'agent_actions',events:[{id:'e'+traceCalls,title:'事件',status:'neutral',timestamp:1700000000}]}})};
  }
  if(p.startsWith('/api/task?')){
    statusCalls+=1;
    return {ok:true,status:200,json:async()=>({data:{task:{task_id:'task-a',status:taskStatus},runtime:{pane_id:'pane-1'}}})};
  }
  throw new Error('unexpected path '+p);
}
const state={taskTracePoll:null,taskDrawerTab:'trace',selectedTaskId:'task-a',taskTraceSeq:1,taskTrace:null,selectedTaskDetail:{task:{task_id:'task-a',status:'pending'},proj:{},live:{}}};

(async()=>{
  // 反向场景：打开抽屉时 pending —— 轮询定时器仍要建立，且先只做状态刷新
  startTaskTracePoll();
  assert.equal(intervals.length,1,'trace 页打开后必须建立轮询定时器');
  assert.equal(intervals[0].ms,8000);
  await pollTaskTrace();
  assert.equal(statusCalls,1,'每个轮询回调必须经 /api/task 刷新任务状态');
  assert.equal(traceCalls,0,'pending 阶段不应拉取轨迹');
  assert.equal(cleared,0,'pending 阶段应保持轮询');
  assert.equal(state.selectedTaskDetail.task.status,'pending');

  // 转 working 后：同一轮询链路自动开始拉取轨迹
  taskStatus='working';
  await pollTaskTrace();
  assert.equal(state.selectedTaskDetail.task.status,'working','状态刷新必须写回 selectedTaskDetail');
  assert.equal(state.selectedTaskDetail.live.pane_id,'pane-1','live 运行时快照应随刷新更新');
  assert.equal(traceCalls,1,'任务转 working 后应启动轨迹拉取');
  assert.equal(state.taskTrace.status,'ready');

  // 完成场景：后端转 completed 后下一 tick 依真实状态自动停止，不再无限扫描
  taskStatus='completed';
  const doneBaseTrace=traceCalls;
  await pollTaskTrace();
  assert.equal(cleared>=1,true,'任务完成后轮询必须自动停止');
  assert.equal(cleared,1,'完成后停表只发生一次');
  assert.equal(state.taskTracePoll,null,'定时器句柄必须清空，生产链路不再有后续 tick');
  // Codex P1：停表前必须以最终状态做一次收尾拉取，否则上一 tick 之后记录的
  // 最终 test/commit 事件永久缺失且抽屉停留在 working 快照
  assert.equal(traceCalls,doneBaseTrace+1,'终态停表前必须完成最后一次轨迹拉取');
  assert.equal(state.selectedTaskDetail.task.status,'completed','终态状态必须写回后再停表');

  // Codex P2：working → blocked/agent_done 等非终态活跃状态变化必须刷新一次
  // 轨迹与头部，阻塞前的最终动作不得停留在旧 working 快照；稳定态不得重复拉取
  taskStatus='blocked';
  state.selectedTaskDetail.task.status='working';
  startTaskTracePoll();
  const blockedBaseTrace=traceCalls;
  await pollTaskTrace();
  assert.equal(traceCalls,blockedBaseTrace+1,'状态变化进入非终态活跃态必须刷新一次轨迹');
  assert.equal(state.selectedTaskDetail.task.status,'blocked','阻塞状态必须写回并渲染');
  await pollTaskTrace();
  assert.equal(traceCalls,blockedBaseTrace+1,'稳定 blocked 不得反复全量拉取轨迹');
  taskStatus='agent_done';
  const adBaseTrace=traceCalls;
  await pollTaskTrace();
  assert.equal(traceCalls,adBaseTrace+1,'agent_done 状态变化同样必须刷新');
  stopTaskTracePoll();

  // document.hidden 守卫：隐藏页 tick 不发任何网络请求，恢复可见后继续
  taskStatus='working';
  startTaskTracePoll();
  const hiddenBaseStatus=statusCalls, hiddenBaseTrace=traceCalls;
  document.hidden=true;
  await pollTaskTrace();
  assert.equal(statusCalls,hiddenBaseStatus,'隐藏页不应刷新任务状态');
  assert.equal(traceCalls,hiddenBaseTrace,'隐藏页不应拉取轨迹');
  document.hidden=false;
  await pollTaskTrace();
  assert.equal(statusCalls,hiddenBaseStatus+1,'恢复可见后应继续刷新');
  assert.equal(traceCalls,hiddenBaseTrace+1,'恢复可见后 working 任务应继续拉取轨迹');

  // 非 trace 页不建立轮询
  state.taskDrawerTab='overview';
  startTaskTracePoll();
  assert.equal(state.taskTracePoll,null,'概览页不应启动轨迹轮询');
})().catch((err)=>{console.error(err);process.exit(1)});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)  # noqa: PLW1510
    assert result.returncode == 0, result.stderr


def test_render_shows_six_icons_and_unknown_time():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    html = console.HTML_TEMPLATE
    names = [
        "esc", "taskDrawerNumTs", "traceClock", "traceDotClass", "traceDetailRows", "renderExecutionTrace",
    ]
    script = "\n".join(_extract(html, name) for name in names)
    script += r"""
const assert=require('node:assert/strict');
const kinds=['file_read','file_edit','test_passed','test_failed','retry','commit'];
const events=kinds.map((kind,i)=>({id:'e'+i,kind,title:'T'+i,detail:'d',status:'neutral',timestamp:1700000000+i,source:'native_session',source_ref:'r'+i}));
events.push({id:'u',kind:'commit',title:'无时间',detail:'',status:'neutral',timestamp:null,source:'task_history',source_ref:'task:u'});
const view=renderExecutionTrace({status:'ready',availability:'agent_actions',events});
for(const g of ['▤','✎','✓','✗','↻','⊂⊃'])assert.equal(view.includes(g),true);
assert.equal(view.includes('时间未知'),true);
assert.equal(view.includes('<script>'),false);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)  # noqa: PLW1510
    assert result.returncode == 0, result.stderr
