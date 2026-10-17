"""Node-level probe of the Console trace poll lifecycle (F4 / DEFECT-3).

Reproduces the real page flow: state.selectedTaskDetail is assigned only by
openTaskDrawer and is never refreshed afterwards, so the poll guard reads a
status snapshot while the task keeps changing server-side.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

node = shutil.which("node")
if not node:
    print("node not found")
    sys.exit(2)

html = (ROOT / "console" / "herdr_factory_console.py").read_text(encoding="utf-8")
start = html.index("HTML_TEMPLATE")


def extract(name):
    key = f"function {name}("
    index = html.find(key)
    assert index >= 0, name
    brace = html.find("{", index)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[index:i + 1]
    raise AssertionError(name)


script = "\n".join(extract(n) for n in ("traceTaskWorking", "stopTaskTracePoll", "pollTaskTrace", "startTaskTracePoll"))
script += r"""
const assert=require('node:assert/strict');
let timers=[]; let cleared=[]; let loads=[];
function setInterval(fn,ms){const id=timers.length+1;timers.push({fn,ms,id});return id}
function clearInterval(id){cleared.push(id)}
function loadTaskTrace(tid,force){loads.push({tid,force})}
const state={taskTracePoll:null,taskDrawerTab:'trace',selectedTaskId:'task-a',
             selectedTaskDetail:{task:{status:'working'}}};
startTaskTracePoll();
assert.equal(timers.length,1);

// Real page flow: nothing re-reads the task after openTaskDrawer, so the
// snapshot below stays 'working' forever even once the run has completed.
state.selectedTaskDetail={task:{status:'working'}};
for(let i=0;i<5;i++) pollTaskTrace();
assert.equal(loads.length,5);
const finished = !cleared.length;   // poll kept running on a finished task
console.log(JSON.stringify({
  loads: loads.length,
  cleared_after_completion: cleared.length,
  poll_survives_finished_task: finished
}));

// Second flow: drawer opened while the task is still 'pending'.
state.taskTracePoll=null; timers=[]; loads=[]; cleared=[];
state.selectedTaskDetail={task:{status:'pending'}};
startTaskTracePoll();
const never_started = state.taskTracePoll===null;
// ...task becomes working server-side; the page never re-evaluates.
state.selectedTaskDetail={task:{status:'working'}};
const still_no_timer = state.taskTracePoll===null;
console.log(JSON.stringify({auto_start_after_pending_to_working: !still_no_timer, never_started_for_pending: never_started}));
"""
result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=20)
print(result.stdout.strip())
if result.returncode != 0:
    print("STDERR:", result.stderr[:800])
sys.exit(result.returncode)
