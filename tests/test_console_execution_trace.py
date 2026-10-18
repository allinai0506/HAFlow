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


def test_polling_preserves_loaded_trace_depth_beyond_first_page():
    # Codex P2：working 任务轨迹超过首页 100 条后，轮询不得把已加载事件截回首页
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    html = console.HTML_TEMPLATE
    names = ["api", "applyTraceResponse", "loadTaskTrace"]
    if "function mergeTraceEvents" in html:
        names.insert(1, "mergeTraceEvents")
    script = "\n".join(_extract(html, name) for name in names)
    script += r"""
const assert=require('node:assert/strict');
globalThis.document={getElementById:()=>null,hidden:false};
globalThis.performance={now:()=>0};
globalThis.sessionStorage={getItem:()=>null};
const apiReads=new Map();
let traceCalls=0;
const traceUrls=[];
function renderTaskDrawer(){}
async function fetch(p){
  if(p.startsWith('/api/task/trajectory')){
    traceCalls+=1;traceUrls.push(p);
    // 后端按 oldest-first 返回 limit 大小的窗口，库里共 255 条
    const lm=p.match(/limit=(\d+)/);const size=lm?Number(lm[1]):100;
    const cm=p.match(/cursor=e(\d+)/);const start=cm?Number(cm[1])+1:0;
    const events=[];for(let i=start;i<Math.min(start+size,255);i++)events.push({id:'e'+i,title:'事件'+i,status:'neutral',timestamp:1700000000+i});
    return {ok:true,status:200,json:async()=>({data:{task_id:'task-a',run_id:'run-a',availability:'agent_actions',events,has_more:false,next_cursor:null}})};
  }
  throw new Error('unexpected path '+p);
}
const state={taskTraceSeq:1,taskDrawerTab:'trace',selectedTaskId:'task-a',taskTrace:null};

(async()=>{
  // 首页：无已加载事件 → 维持 limit=100 全窗口
  await loadTaskTrace('task-a',true);
  assert.equal(traceUrls[0].includes('limit=100'),true,'空视图轮询应取首页窗口');
  assert.equal(traceUrls[0].includes('cursor='),false,'空视图不应带 cursor');

  // 已加载 150 条（跨两页）：轮询窗口必须覆盖已加载深度
  const deep=[];for(let i=0;i<150;i++)deep.push({id:'e'+i,title:'事件'+i,status:'neutral',timestamp:1700000000+i});
  state.taskTrace={status:'ready',task_id:'task-a',run_id:'run-a',availability:'agent_actions',events:deep,has_more:true,next_cursor:'e149'};
  await loadTaskTrace('task-a',true);
  assert.equal(traceUrls[1].includes('limit=150'),true,'轮询窗口必须覆盖已加载深度: '+traceUrls[1]);
  assert.equal(state.taskTrace.events.length>=150,true,'覆盖深度后不得把已加载事件截回首页');

  // 已加载超过后端单窗口上限（200）：改用 cursor 增量追加，不重建整页
  const huge=[];for(let i=0;i<250;i++)huge.push({id:'e'+i,title:'事件'+i,status:'neutral',timestamp:1700000000+i});
  state.taskTrace={status:'ready',task_id:'task-a',run_id:'run-a',availability:'agent_actions',events:huge,has_more:false,next_cursor:null};
  const before=traceCalls;
  await loadTaskTrace('task-a',true);
  assert.equal(traceCalls,before+1,'超深度后轮询仍应发请求以发现新活动');
  assert.equal(traceUrls[traceUrls.length-1].includes('cursor=e249'),true,'深度超过单窗口上限应取增量窗口: '+traceUrls[traceUrls.length-1]);
  assert.equal(state.taskTrace.events.length,255,'增量窗口只追加新事件，保留既有 250 条');
})().catch((err)=>{console.error(err);process.exit(1)});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)  # noqa: PLW1510
    assert result.returncode == 0, result.stderr


def test_polling_tail_cursor_at_window_limit_and_timed_ordering():
    # Codex P2：恰好 200 条时即进入增量窗口；cursor 必须取最后一条有时间戳的事件，
    # 因为无时间戳的任务历史/提交事件排在所有有时间戳事件之后，用尾部无时间戳事件做
    # cursor 会永久漏掉随后到达的新原生动作。
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    html = console.HTML_TEMPLATE
    names = ["api", "applyTraceResponse", "loadTaskTrace"]
    if "function mergeTraceEvents" in html:
        names.insert(1, "mergeTraceEvents")
    script = "\n".join(_extract(html, name) for name in names)
    script += r"""
const assert=require('node:assert/strict');
globalThis.document={getElementById:()=>null,hidden:false};
globalThis.performance={now:()=>0};
globalThis.sessionStorage={getItem:()=>null};
const apiReads=new Map();
const traceUrls=[];
function renderTaskDrawer(){}
async function fetch(p){
  if(p.startsWith('/api/task/trajectory')){
    traceUrls.push(p);
    const lm=p.match(/limit=(\d+)/);const size=lm?Number(lm[1]):100;
    const cm=p.match(/cursor=e(\d+)/);const start=cm?Number(cm[1])+1:0;
    const events=[];for(let i=start;i<Math.min(start+size,255);i++)events.push({id:'e'+i,title:'事件'+i,status:'neutral',timestamp:1700000000+i});
    return {ok:true,status:200,json:async()=>({data:{task_id:'task-a',run_id:'run-a',availability:'agent_actions',events,has_more:false,next_cursor:null}})};
  }
  throw new Error('unexpected path '+p);
}
const state={taskTraceSeq:1,taskDrawerTab:'trace',selectedTaskId:'task-a',taskTrace:null};
function build(n,tailUntimed){
  const evs=[];for(let i=0;i<n;i++)evs.push({id:'e'+i,title:'事件'+i,status:'neutral',timestamp:1700000000+i});
  if(tailUntimed)for(let i=n-10;i<n;i++)evs[i].timestamp=null;
  return evs;
}

(async()=>{
  // 恰好等于单页上限 200：必须已进入增量窗口，而不是反复重取前 200 条
  state.taskTrace={status:'ready',task_id:'task-a',run_id:'run-a',availability:'agent_actions',events:build(200,false),has_more:true,next_cursor:'e199'};
  await loadTaskTrace('task-a',true);
  assert.equal(traceUrls[0].includes('cursor=e199'),true,'达到单页上限即应转增量窗口: '+traceUrls[0]);
  assert.equal(state.taskTrace.events.length,255,'增量窗口追加第 201 条之后的新事件（库内共 255 条）');

  // 尾部为无时间戳事件：cursor 取最后一条有时间戳的事件，并按 id 合并不重复
  state.taskTrace={status:'ready',task_id:'task-a',run_id:'run-a',availability:'agent_actions',events:build(250,true),has_more:false,next_cursor:null};
  await loadTaskTrace('task-a',true);
  assert.equal(traceUrls[1].includes('cursor=e239'),true,'尾部无时间戳时 cursor 必须取最后一条有时间戳事件: '+traceUrls[1]);
  const ids=state.taskTrace.events.map(e=>e.id);
  assert.equal(new Set(ids).size,ids.length,'合并按 id 去重，不得出现重复事件');
  assert.equal(ids.length,255,'保留既有 250 条（含 10 条无时间戳尾部）并追加新增 5 条');
  assert.equal(ids.filter(id=>id==='e249').length,1);
})().catch((err)=>{console.error(err);process.exit(1)});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)  # noqa: PLW1510
    assert result.returncode == 0, result.stderr


def test_invalid_incremental_cursor_falls_back_instead_of_clearing_trace():
    # Codex P2：cursor 指向的事件可能已被后续证据抑制（tests_completed 被
    # verification_completed 取代）而返回 400 "cursor 无效"；此时必须退回无 cursor 的
    # 覆盖窗口并按 id 合并，而不是清空已加载轨迹——终态那一次刷新后轮询已停，清空即永久坏掉。
    node = shutil.which("node")
    if not node:
        pytest.skip("node not found")
    html = console.HTML_TEMPLATE
    names = ["api", "applyTraceResponse", "loadTaskTrace"]
    if "function mergeTraceEvents" in html:
        names.insert(1, "mergeTraceEvents")
    script = "\n".join(_extract(html, name) for name in names)
    script += r"""
const assert=require('node:assert/strict');
globalThis.document={getElementById:()=>null,hidden:false};
globalThis.performance={now:()=>0};
globalThis.sessionStorage={getItem:()=>null};
const apiReads=new Map();
const traceUrls=[];
function renderTaskDrawer(){}
async function fetch(p){
  if(p.startsWith('/api/task/trajectory')){
    traceUrls.push(p);
    if(p.includes('cursor=')){
      // 模拟 herdr-task 侧 cursor 校验失败：raise TraceQueryError(400,'cursor 无效')
      return {ok:false,status:400,json:async()=>({ok:false,error:'cursor 无效'})};
    }
    const lm=p.match(/limit=(\d+)/);const size=lm?Number(lm[1]):100;
    const events=[];for(let i=0;i<Math.min(size,255);i++)events.push({id:'e'+i,title:'事件'+i,status:'neutral',timestamp:1700000000+i});
    return {ok:true,status:200,json:async()=>({data:{task_id:'task-a',run_id:'run-a',availability:'agent_actions',events,has_more:false,next_cursor:null}})};
  }
  throw new Error('unexpected path '+p);
}
const state={taskTraceSeq:1,taskDrawerTab:'trace',selectedTaskId:'task-a',taskTrace:null};

(async()=>{
  const loaded=[];for(let i=0;i<250;i++)loaded.push({id:'e'+i,title:'事件'+i,status:'neutral',timestamp:1700000000+i});
  state.taskTrace={status:'ready',task_id:'task-a',run_id:'run-a',availability:'agent_actions',events:loaded,has_more:false,next_cursor:null};
  await loadTaskTrace('task-a',true);
  assert.equal(traceUrls.length,2,'cursor 失效后必须再发一次无 cursor 覆盖窗口请求');
  assert.equal(traceUrls[1].includes('cursor='),false,'兜底请求不得继续携带失效 cursor');
  assert.equal(state.taskTrace.status,'ready','兜底成功不得把抽屉打成 error 态');
  assert.equal(state.taskTrace.events.length,250,'保留全部已加载事件，不清空轨迹');
})().catch((err)=>{console.error(err);process.exit(1)});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)  # noqa: PLW1510
    assert result.returncode == 0, result.stderr


def test_flow_and_list_render_identical_task_content_and_history():
    names = ['esc', 'renderTasks', 'renderTaskRow', 'flowTasksForNode', 'renderNodeInspector']
    script = '\n'.join(_extract(console.HTML_TEMPLATE, n) for n in names if n != 'renderTaskRow' or 'function renderTaskRow(' in console.HTML_TEMPLATE)
    script += r'''
const assert=require('node:assert/strict');
const elements={tasks:{},flowInspectorBody:{},flowInspTitle:{},flowInspMeta:{}};
const document={getElementById:id=>elements[id]};
const task={task_id:'test-auto',node:'test',agent:'qodercli',status:'superseded',superseded_by:'test-r2'};
const state={workflow:{tasks:[task]},workflowId:'wf',flowSelectedNodeId:'test',flowInspectorTab:'tasks'};
function updateAttentionHub(){}
function taskDisplayName(){return '洁净室回归测试'}
function badge(s){return '<b>'+s+'</b>'}
function flowNodeById(){return {id:'test',label:'测试',node_type:'agent',status:'waiting'}}
function cleanStageLabel(s){return s}
function humanNodeStatus(s){return s}
function flowNodeStatus(n,s){return s}
renderTasks();
renderNodeInspector();
assert.equal(flowTasksForNode('test').length,1);
assert.equal(elements.flowInspectorBody.innerHTML,elements.tasks.innerHTML);
assert.ok(elements.tasks.innerHTML.includes('洁净室回归测试'));
assert.ok(elements.tasks.innerHTML.includes('qodercli'));
assert.ok(elements.tasks.innerHTML.includes('superseded'));
assert.ok(elements.tasks.innerHTML.includes('成果会签'));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_recorded_reports_are_visible_and_routine_status_is_collapsed():
    names = ['esc', 'taskDrawerNumTs', 'traceClock', 'traceDotClass', 'traceDetailRows', 'renderExecutionTrace']
    script = '\n'.join(_extract(console.HTML_TEMPLATE, n) for n in names)
    script += r'''
const assert=require('node:assert/strict');
const html=renderExecutionTrace({status:'ready',availability:'recorded_actions',degradation:[{code:'native_unsupported'}],events:[
  {kind:'task_completed',title:'任务完成',timestamp:20},
  {kind:'progress_report',title:'阶段报告',timestamp:10,detail:'174项回归已执行',details:{observation_id:'obs-report',sha256:'abc'}}
]});
assert.ok(html.includes('174项回归已执行'));
assert.ok(html.includes('产物回执'));
assert.ok(html.includes('obs-report'));
assert.ok(html.includes('暂无原生工具轨迹适配'));
assert.ok(!html.includes('当前仅有任务状态记录'));
assert.ok(html.indexOf('174项回归已执行') < html.indexOf('<details'));
assert.ok(html.includes('<summary>任务状态记录（1）</summary>'));
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_task_menu_opens_the_clicked_view_instead_of_a_duplicate_task_id():
    script = _extract(console.HTML_TEMPLATE, 'toggleTaskMenu')
    script += r'''
const assert=require('node:assert/strict');
const clicked={classList:{contains:()=>false,add:s=>{clicked.open=s==='open'}}};
const document={getElementById:()=>{throw Error('would select hidden list menu')}};
function closeAllTaskMenus(){}
function closeMoreMenu(){}
toggleTaskMenu({stopPropagation(){},currentTarget:{closest:()=>clicked}},'same-task');
assert.equal(clicked.open,true);
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_trace_drawer_prioritizes_execution_over_repeated_attributes_and_artifacts():
    script = _extract(console.HTML_TEMPLATE, 'renderTaskDrawer')
    script += r'''
const assert=require('node:assert/strict');
const elements={};
const document={getElementById:id=>elements[id]||(elements[id]={classList:{toggle(){}},setAttribute(){}})};
const task={task_id:'t',agent:'qodercli',status:'completed',stage_label:'测试',pane_id:'p'};
const state={selectedTaskDetail:{task,proj:{}},taskDrawerTab:'trace',taskTrace:{}};
function taskDisplayName(){return '测试'}
function taskDurationSecs(){return 12}
function humanStatus(s){return s}
function formatElapsed(){return '12s'}
function taskDrawerMenuHtml(){return ''}
function taskDisplayStart(){return null}
function esc(s){return String(s)}
function renderExecutionTrace(){return '<div>具体执行报告</div>'}
function renderTaskArtifacts(){return '<div>产物查看下载</div>'}
renderTaskDrawer();
const html=elements.taskDrawerBody.innerHTML;
assert.ok(html.indexOf('具体执行报告')<html.indexOf('产物查看下载'));
assert.ok(!html.includes('td-attrs'));
assert.ok(html.includes('产物查看下载'));
'''
    result = subprocess.run(['node','-e',script],capture_output=True,text=True,check=False)
    assert result.returncode == 0, result.stderr


def test_flow_graph_refreshes_changed_projection_but_reuses_unchanged_graph():
    script = _extract(console.HTML_TEMPLATE, 'renderFlowGraph')
    script += r'''
const assert=require('node:assert/strict');
const state={workflowId:'wf',flowSelectedNodeId:'a'};
let projection={nodes:[{id:'a',label:'实现',status:'working',agents:['claude'],task_count:1},{id:'b',status:'waiting'}],edges:[{from:'a',to:'b'}]};
const document={getElementById:id=>id==='flowCanvas'?{clientWidth:800}:null};
let builds=0;
const window={dagre:{graphlib:{},layout(){}},X6:{Graph:class{
 constructor(){this.nodes=[];this.edges=[];this.generation=++builds}
 addNode(n){this.nodes.push(n)}
 addEdge(e){this.edges.push(e)}
 on(){}
}}};
function flowGraphData(){return projection}
function ensureFlowCardShape(){return true}
function dagreLayoutPositions(){return {}}
function destroyFlowGraph(){state.flowGraph=null;state.flowGraphWfId=null}
function updateFlowSelection(){}
function fitFlowGraph(){}
renderFlowGraph();
assert.equal(state.flowGraph.nodes[0].data.status,'working');
renderFlowGraph();
assert.equal(builds,1);
projection=JSON.parse(JSON.stringify(projection));
projection.nodes[0].status='completed';
projection.nodes[0].agents=['qodercli'];
projection.nodes[0].task_count=2;
projection.nodes[1].status='working';
renderFlowGraph();
assert.equal(state.flowGraph.nodes[0].data.status,'completed');
assert.deepEqual(state.flowGraph.nodes[0].data.agents,['qodercli']);
assert.equal(state.flowGraph.nodes[0].data.task_count,2);
assert.equal(state.flowGraph.edges[0].attrs.line.stroke,'#4f46e5');
renderFlowGraph();
assert.equal(builds,2);
'''
    result = subprocess.run(['node','-e',script],capture_output=True,text=True,check=False)
    assert result.returncode == 0, result.stderr
