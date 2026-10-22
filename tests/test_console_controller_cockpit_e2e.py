"""Real Console HTTP/page journeys, checked against the selected SQLite store.

Requires the local ego-browser service. Only terminal transport and service
probes are controlled; projection, actions, identity, receipts and storage run
unchanged. Each test owns and closes its Page so xdist workers cannot navigate
another test's temporary service. No Agent is launched and no model is called.

The cockpit opens its action modal asynchronously after Enter, so a journey
must wait for it before touching a field inside: without that wait
`page.fill('#cockpitReason', ...)` intermittently reports
`Selector #cockpitReason matched 0 elements`. `confirm_action` therefore waits
for the modal to open, symmetric with the existing wait for it to close.
Tests in this file still fail intermittently from two other causes that are
not addressed here; see the tracking issue before assuming a new red is
yours.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest

from herdr.state_store import SQLiteStateStore

SPACE = f"HAFlow Controller Cockpit Task 6 20261021 process {os.getpid()}"


def collect_cockpit_failure_diagnostics(binary, page_label, script, result):
    if page_label is None or not binary:
        return ""
    selectors = [m.group("sel") for m in re.finditer(r"(?:click|press|fill|waitForFunction|querySelector)\(\s*(?P<q>['\"])(?P<sel>.*?)(?P=q)", script)]
    selectors_json = json.dumps(selectors)
    diag_script = f"""
    const task=await taskSpace({json.dumps(SPACE)});
    const page=task.page({json.dumps(page_label)});
    try {{
        const report = await page.evaluate((selectors) => {{
            const res = {{
                url: location.href,
                workflowId: window.state?.workflowId,
                readState: window.state?.controllerCockpit?.read_state,
                receipt: window.state?.controllerReceipt,
                inFlightFetches: Array.from(window.__inFlightFetches?.values?.() || []).map(f => ({{
                    url: f.url,
                    elapsedMs: Math.round(performance.now() - f.started)
                }})),
                inspections: []
            }};
            for (let raw of selectors) {{
                let sel = raw;
                if (sel.startsWith('loc=css:')) sel = sel.slice(8);
                try {{
                    const el = document.querySelector(sel);
                    if (!el) {{
                        res.inspections.push({{ selector: raw, found: false }});
                        continue;
                    }}
                    const r = el.getBoundingClientRect();
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    const hit = document.elementFromPoint(cx, cy);
                    const hitCs = hit ? window.getComputedStyle(hit) : null;
                    const elCs = window.getComputedStyle(el);
                    res.inspections.push({{
                        selector: raw,
                        found: true,
                        target: {{
                            tag: el.tagName,
                            className: el.className,
                            zIndex: elCs.zIndex,
                            position: elCs.position,
                            rect: {{ x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }}
                        }},
                        centerPoint: {{ x: Math.round(cx), y: Math.round(cy) }},
                        hitElement: hit ? {{
                            tag: hit.tagName,
                            className: hit.className,
                            actionId: hit.getAttribute('data-action-id') || hit.closest('[data-action-id]')?.getAttribute('data-action-id'),
                            zIndex: hitCs.zIndex,
                            position: hitCs.position,
                            rect: (function(){{
                                const hr = hit.getBoundingClientRect();
                                return {{ x: Math.round(hr.x), y: Math.round(hr.y), width: Math.round(hr.width), height: Math.round(hr.height) }};
                            }})(),
                            isTargetOrDescendant: el === hit || el.contains(hit)
                        }} : null
                    }});
                }} catch(e) {{
                    res.inspections.push({{ selector: raw, error: e.message }});
                }}
            }}
            return res;
        }}, {selectors_json});
        console.log("DIAGNOSTICS_JSON=" + JSON.stringify(report));
    }} catch(e) {{
        console.log("DIAGNOSTICS_ERROR=" + e.message);
    }}
    """
    try:
        proc = subprocess.run([binary, "nodejs", "-e", diag_script],
                              capture_output=True, text=True, timeout=8)
        output = proc.stdout + proc.stderr
        if "DIAGNOSTICS_JSON=" in output:
            data = json.loads(output.split("DIAGNOSTICS_JSON=", 1)[1].splitlines()[0])
            return "\n--- Cockpit E2E Failure Diagnostics ---\n" + json.dumps(data, indent=2, ensure_ascii=False) + "\n----------------------------------------\n"
        if "DIAGNOSTICS_ERROR=" in output:
            return f"\n--- Cockpit E2E Failure Diagnostics Error: {output.strip()} ---\n"
    except Exception as exc:
        return f"\n--- Cockpit E2E Failure Diagnostics Collection Failed: {exc} ---\n"
    return ""


def browser(script, *, page_label):
    binary = shutil.which("ego-browser")
    if not binary:
        pytest.skip("real browser E2E requires ego-browser and its local browser service")
    prelude = f"const task=await taskSpace({json.dumps(SPACE)});"
    if page_label is not None:
        prelude += f"const page=task.page({json.dumps(page_label)});"
    result = subprocess.run([binary, "nodejs", "-e", prelude + script],
                            capture_output=True, text=True, timeout=45)
    if result.returncode != 0:
        diagnostics = collect_cockpit_failure_diagnostics(binary, page_label, script, result)
        assert result.returncode == 0, (result.stdout + result.stderr + diagnostics)
    return result.stdout + result.stderr


@pytest.fixture
def cockpit(tmp_path, monkeypatch):
    from console import common, runtime
    from console.http_server import Handler
    from herdr import steering

    store = SQLiteStateStore(tmp_path / "state.db")
    monkeypatch.setenv("HERDR_STATE_DB", str(store.db_path))
    monkeypatch.setenv("HERDR_WORKFLOW_DOCS_DIR", str(tmp_path / "workflow-docs"))
    for key, name in (("TASKS_FILE", "tasks.json"), ("WORKFLOWS_FILE", "workflows.json"),
                      ("STAGE_STATE_FILE", "stage-state.json")):
        monkeypatch.setenv(key, str(tmp_path / name))
    project = {"project_id": "cockpit-e2e", "project_name": "Cockpit 验收", "project_root": str(tmp_path)}
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps({"projects": {project["project_id"]: project}}))
    monkeypatch.setattr(common, "PROJECTS_FILE", projects)
    monkeypatch.setattr(runtime, "service_status", lambda: {"com.user.herdr-controller": "running"})

    config = {"name": "cockpit-e2e", "nodes": [
        {"id": "build", "label": "实现", "node_type": "agent"},
        {"id": "gate", "label": "验收门禁", "node_type": "agent", "depends_on": ["build"]},
        {"id": "dispatch", "label": "等待派发", "node_type": "agent", "depends_on": ["gate"]}]}
    for wid, cfg in (("wf-e2e", config), ("wf-auto", {"nodes": [{"id": "build"}]}),
                     ("wf-confirm", {"control_policy": {"mode": "supervised"}, "nodes": [{"id": "build"}]})):
        store.save_workflow({"workflow_id": wid, "project_id": project["project_id"],
            "project_root": str(tmp_path), "title": wid, "execution_id": "exec-e2e",
            "status": "running" if wid == "wf-e2e" else "paused", "config": cfg})
    for tid, node, status in (("working", "build", "working"), ("blocked", "gate", "blocked")):
        store.save_task({"task_id": tid, "workflow_id": "wf-e2e", "project_id": project["project_id"],
            "node": node, "stage": node, "run_id": tid + "-run", "pane_id": tid + "-pane",
            "execution_id": "exec-e2e", "version": 2, "status": status, "agent": "codex"})
    # Adopt supervision for already registered Runs; new supervised launches
    # must obtain a durable dispatch approval through the production boundary.
    config['nodes'][0]['control_mode'] = 'supervised'
    store.save_workflow({**store.get_workflow('wf-e2e'), 'config': config})
    effects = []

    def interrupt(pane_id, *, reason):
        assert pane_id == store.get_task("working")["pane_id"]
        effects.append({"kind": "interrupt", "pane_id": pane_id, "reason": reason})
        return True

    monkeypatch.setattr(steering, "get_agent_adapter", lambda _: SimpleNamespace(
        supports_soft_steer=True, supports_interrupt=True, protocol_level="tty", name="controlled",
        interrupt=interrupt))

    def transport(argv, timeout=20, check=False):
        # Strictly contain every Console subprocess; unexpected writes fail closed.
        assert timeout > 0
        if argv[:3] == ["herdr", "agent", "prompt"]:
            assert argv[3] == store.get_task("working")["pane_id"]
            assert argv[-3:] == ["--wait", "--timeout", "180000"]
            effects.append({"kind": "prompt", "pane_id": argv[3], "instruction": argv[4]})
            return subprocess.CompletedProcess(argv, 0, "controlled Pane acknowledged", "")
        if argv[:3] == ["herdr", "agent", "get"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agent": {
                "agent_status": "working", "agent": "codex", "pane_id": argv[3]}}}), "")
        if argv[:3] in (["herdr", "workspace", "list"], ["herdr", "pane", "list"], ["herdr", "tab", "list"]):
            return subprocess.CompletedProcess(argv, 0, '{"result":{}}', "")
        raise AssertionError(f"unexpected external command: {argv!r}")

    monkeypatch.setattr(common, "run", transport)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"

    def read(wid="wf-e2e"):
        with urlopen(url + "/api/workflow/controller-cockpit?workflow_id=" + wid, timeout=10) as response:
            return json.load(response)["data"]

    page_label = None
    try:
        created = browser("const owned=await task.newPage();console.log('OWNED_PAGE='+JSON.stringify(owned.label));",
                          page_label=None)
        page_label = json.loads(created.split("OWNED_PAGE=", 1)[1].splitlines()[0])
        yield SimpleNamespace(store=store, url=url, read=read, effects=effects, artifacts=tmp_path,
                              browser=lambda script: browser(script, page_label=page_label))
    finally:
        try:
            if page_label is not None:
                browser("await page.close();", page_label=page_label)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def open_cockpit(fixture, wid="wf-e2e"):
    fixture.browser(f"""
        await page.cdp('Emulation.setDeviceMetricsOverride',{{width:1440,height:1800,deviceScaleFactor:1,mobile:false}});
        await page.goto({json.dumps(fixture.url + '/?workflow_id=' + wid)});
        await page.evaluate(()=>{{
            if(!window.__inFlightFetches){{
                window.__inFlightFetches=new Map();
                const orig=window.fetch;
                window.fetch=function(...args){{
                    const url=typeof args[0]==='string'?args[0]:(args[0]?.url||'unknown');
                    const key=Symbol();
                    window.__inFlightFetches.set(key,{{url,started:performance.now()}});
                    return orig.apply(this,args).finally(()=>window.__inFlightFetches.delete(key));
                }};
            }}
        }});
        await page.waitForFunction(()=>typeof openControllerCockpit==='function'&&state.overview&&state.workflow?.workflow?.workflow_id==={json.dumps(wid)}&&!state.controllerActionsData?.pending&&state.decisionData!==undefined,undefined,{{timeout:15000}});
        await page.evaluate(()=>openControllerCockpit({{workflowId:{json.dumps(wid)}}}));
        await page.waitForFunction(()=>state.controllerCockpit?.workflow?.workflow_id==={json.dumps(wid)}&&state.controllerCockpit.read_state==='confirmed');
    """)


def confirm_action(fixture, aid, *, instruction=None, reason="人工核查当前身份和证据"):
    fixture.browser(f"""
        await page.press('loc=css:[data-action-id="{aid}"] button','Enter');
        await page.waitForFunction(()=>document.getElementById('modal').classList.contains('open'));
        {f'await page.fill("#cockpitInstruction",{json.dumps(instruction)});' if instruction is not None else ''}
        await page.fill('#cockpitReason',{json.dumps(reason)});
        await page.click('#cockpitActionConfirm');
        await page.waitForFunction(()=>!document.getElementById('modal').classList.contains('open'));
        await page.waitForFunction(()=>state.controllerReceipt?.action_id==={json.dumps(aid)});
        console.log(JSON.stringify(await page.evaluate(()=>state.controllerReceipt)));
    """)


def test_steer_custom_instruction_roundtrips_to_current_run(cockpit):
    """Catches an Action modal that silently sends the core's generic default."""
    open_cockpit(cockpit)
    out = cockpit.browser("""
        await page.click('loc=css:[data-action-id="working:steer"] button');
        console.log('INSTRUCTION_FIELD='+await page.evaluate(()=>!!document.getElementById('cockpitInstruction')));
    """)
    assert "INSTRUCTION_FIELD=true" in out, "steer confirmation must accept the human instruction"
    cockpit.browser("""
        await page.evaluate(()=>closeModal());
        await page.waitForFunction(()=>!document.getElementById('modal').classList.contains('open'));
    """)
    confirm_action(cockpit, "working:steer", instruction="请先核查 SQLite 回执再继续")
    queued = cockpit.store.list_steers(task_id="working")
    assert len(queued) == 1
    assert (queued[0]["instruction"], queued[0]["run_id"], queued[0]["status"]) == (
        "请先核查 SQLite 回执再继续", "working-run", "pending")
    receipt = cockpit.read()["receipts"][0]
    assert (receipt["status"], receipt["steer_id"]) == ("applied", queued[0]["steer_id"])
    assert "已生效" in cockpit.browser("console.log(await page.evaluate(()=>document.getElementById('controllerTabView').innerText));")


def test_controller_automatic_default_and_template_confirmation(cockpit):
    from services.controller.cockpit_automation import run_automatic_controller_actions
    deps = SimpleNamespace(store=cockpit.store)
    results = run_automatic_controller_actions("wf-auto", deps=deps)
    assert results[0]["status"] == "applied"
    assert cockpit.store.get_workflow("wf-auto")["status"] == "running"
    assert run_automatic_controller_actions("wf-confirm", deps=deps) == []
    assert cockpit.store.get_workflow("wf-confirm")["status"] == "paused"
    open_cockpit(cockpit, "wf-confirm")
    action = next(a for a in cockpit.read("wf-confirm")["actions"] if a["action_id"] == "wf-confirm:resume_workflow")
    assert action["automation"] == {"mode": "confirmation_required", "source": "workflow"}
    confirm_action(cockpit, "wf-confirm:resume_workflow")
    assert cockpit.store.get_workflow("wf-confirm")["status"] == "running"
    assert cockpit.read("wf-confirm")["receipts"][0]["status"] == "applied"
    # Re-open the store: receipts must survive browser/Store lifetime, not live in JS.
    assert SQLiteStateStore(cockpit.store.db_path).get_workflow("wf-auto")["status"] == "running"
    assert cockpit.read("wf-auto")["receipts"][0] == results[0]


def test_stale_identity_rejected_then_halt_uses_current_pane(cockpit):
    open_cockpit(cockpit)
    cockpit.browser("await page.press('loc=css:[data-action-id=\"working:halt\"] button','Enter');")
    cockpit.store.update_task_metadata("working", {"pane_id": "replacement-pane"})
    cockpit.browser("""
        await page.waitForFunction(()=>document.getElementById('modal').classList.contains('open'));
        await page.fill('#cockpitReason','核查旧快照'); await page.click('#cockpitActionConfirm');
        await page.waitForFunction(()=>state.controllerReceipt?.status==='rejected');
        await page.waitForFunction(()=>state.controllerCockpit.tasks.some(t=>t.task_id==='working'&&t.pane_id==='replacement-pane'));
    """)
    assert cockpit.effects == []
    assert cockpit.store.get_task("working")["status"] == "working"
    assert cockpit.read()["receipts"][0]["reason_code"] == "identity_changed"
    confirm_action(cockpit, "working:halt")
    assert cockpit.effects == [{"kind": "interrupt", "pane_id": "replacement-pane", "reason": "人工核查当前身份和证据"}]
    assert cockpit.store.get_task("working")["status"] == "interrupted"
    assert cockpit.read()["receipts"][0]["status"] == "applied"


def test_redrive_transport_ack_is_not_fabricated_completion(cockpit):
    open_cockpit(cockpit)
    confirm_action(cockpit, "working:redrive")
    assert len(cockpit.effects) == 1
    assert cockpit.effects[0]["kind"] == "prompt"
    assert cockpit.effects[0]["pane_id"] == "working-pane"
    assert cockpit.store.get_task("working")["status"] == "working"
    assert cockpit.read()["receipts"][0]["status"] == "accepted_waiting"


def test_canvas_deeplink_and_responsive_cockpit(cockpit):
    open_cockpit(cockpit)
    out = cockpit.browser("""
        await page.evaluate(()=>returnFromControllerCockpit());
        await page.waitForFunction(()=>state.workflowId!=='__ctl__'&&document.getElementById('flowWrap')?.style.display==='flex');
        await page.click('loc=css:.x6-node[data-cell-id="gate"]');
        await page.waitForFunction(()=>state.flowSelectedNodeId==='gate'&&state.selectedTaskId==='blocked');
        await page.keyboard.press('Escape');
        console.log(await page.snapshot());
    """)
    assert "Controller" in out
    cockpit.browser("""
        await page.press('loc=css:#flowInspectorBody .flow-act-group > button','Enter');
        await page.waitForFunction(()=>state.workflowId==='__ctl__'&&state.controllerTarget.nodeId==='gate');
        console.log(await page.snapshot());
    """)
    for width in (1440, 390):
        out = cockpit.browser(f"""
            await page.cdp('Emulation.setDeviceMetricsOverride',{{width:{width},height:900,deviceScaleFactor:1,mobile:false}});
            console.log('FIT='+await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
            await page.screenshot({{path:{json.dumps(str(cockpit.artifacts / ('cockpit-' + str(width) + '.png')))}}});
        """)
        assert "FIT=true" in out


def test_recovery_read_failure_shows_unknown(cockpit, monkeypatch):
    from herdr import state_db
    open_cockpit(cockpit)
    original = state_db.get_readonly_db_connection

    def unavailable(path=None):
        if Path(path) == cockpit.store.db_path:
            raise OSError("controlled recovery read failure")
        return original(path)

    monkeypatch.setattr(state_db, "get_readonly_db_connection", unavailable)
    out = cockpit.browser("""
        await page.evaluate(()=>loadControllerCockpit('wf-e2e'));
        console.log(await page.evaluate(()=>document.getElementById('controllerTabView').innerText));
        console.log('ENABLED='+await page.evaluate(()=>[...document.querySelectorAll('[data-action-id] button')].filter(b=>!b.disabled).length));
    """)
    assert "未确认" in out
    assert "ENABLED=0" in out


def test_dispatch_waits_for_registered_task_and_never_resends(cockpit):
    from herdr import node_dispatch_store as nd
    from herdr.task_resources import begin_launch_intent, finish_launch_intent
    wid = "wf-dispatch"
    cockpit.store.save_workflow({"workflow_id": wid, "project_id": "cockpit-e2e",
        "status": "running", "execution_id": "dispatch-exec", "created_at": time.time(),
        "config": {"nodes": [{"id": "dispatch", "node_type": "agent"}]}})
    nd.reconcile_workflow(cockpit.store.db_path, wid)
    operation = nd.operation_for_node(cockpit.store.db_path, wid, "dispatch")
    assert nd.claim(cockpit.store.db_path, operation["id"], "controlled-transport")
    nd.start(cockpit.store.db_path, operation["id"], "controlled-transport", expected_task_ids=["registered"])
    operation = nd.transport_finished(cockpit.store.db_path, operation["id"], "controlled-transport",
                                      reason="dispatch_awaiting_task")
    assert operation["status"] == "awaiting_result"
    assert cockpit.store.list_tasks(workflow_id=wid) == []
    open_cockpit(cockpit, wid)
    out = cockpit.browser("console.log(await page.evaluate(()=>document.getElementById('controllerTabView').innerText));")
    assert "等待总指挥建立任务" in out
    assert "未登记任务" in out
    # Only the controlled launch transport is replaced: real intent and registration
    # own the evidence that resolves the production node-dispatch state machine.
    intent = begin_launch_intent(cockpit.store, workflow_id=wid, node_id="dispatch", candidate_sha=None,
        task_id="registered", run_id="registered-run", execution_id="dispatch-exec",
        dispatch_operation_id=operation["id"])["intent"]
    cockpit.store.save_task({"task_id": "registered", "workflow_id": wid, "node": "dispatch",
        "status": "working", "run_id": "registered-run", "pane_id": "registered-pane",
        "execution_id": "dispatch-exec", "launch_intent_id": intent["intent_id"],
        "dispatch_operation_id": operation["id"]})
    finish_launch_intent(cockpit.store, intent)
    nd.reconcile_workflow(cockpit.store.db_path, wid)
    assert nd.operation_for_node(cockpit.store.db_path, wid, "dispatch")["status"] == "resolved"
    assert nd.claim(cockpit.store.db_path, operation["id"], "duplicate") is None
    out = cockpit.browser("""
        await page.evaluate(()=>loadControllerCockpit('wf-dispatch'));
        console.log(await page.evaluate(()=>document.getElementById('controllerTabView').innerText));
    """)
    assert "registered-run" in out
    assert "registered-pane" in out
    assert "等待总指挥建立任务" not in out
    assert cockpit.effects == []


def test_force_pass_advance_failure_is_durable_partial_without_repeat(cockpit):
    wid = "wf-partial"
    cockpit.store.save_workflow({"workflow_id": wid, "project_id": "cockpit-e2e", "status": "running",
        "execution_id": "partial-exec", "config": {"nodes": [{"id": "gate"}]}})
    cockpit.store.save_task({"task_id": "partial-task", "workflow_id": wid, "node": "gate", "stage": "gate",
        "status": "working", "run_id": "partial-run", "pane_id": "partial-pane", "execution_id": "partial-exec"})
    open_cockpit(cockpit, wid)
    task = cockpit.store.get_task("partial-task")
    request = {"workflow_id": wid, "type": "force_pass_advance", "task_id": "partial-task",
        "gate_node_id": "gate", "expected_version": task["version"], "expected_pane_id": "partial-pane",
        "reason": "核验现有产物，测试无下一阶段的失败回执", "confirmed": True}
    out = cockpit.browser(f"""
        console.log(JSON.stringify(await page.evaluate(async payload=>{{
            const response=await fetch('/api/controller/execute-action',{{method:'POST',headers:{{'Content-Type':'application/json','X-HAFlow-Console-Request':'1'}},body:JSON.stringify(payload)}});
            return await response.json();
        }},{json.dumps(request)})));
        await page.evaluate(()=>loadControllerCockpit({json.dumps(wid)}));
        console.log(await page.evaluate(()=>document.getElementById('controllerTabView').innerText));
    """)
    assert '"status":"partial"' in out
    assert "部分生效" in out and "force_pass" in out and "advance" in out
    assert cockpit.store.get_task("partial-task")["stage_verdict"] == "pass"
    before = cockpit.store.list_events(workflow_id=wid)
    cockpit.browser(f"await page.evaluate(()=>loadControllerCockpit({json.dumps(wid)}));")
    assert cockpit.store.list_events(workflow_id=wid) == before
    assert cockpit.read(wid)["receipts"][0]["applied_steps"] == ["force_pass"]


def test_unimplemented_automatic_action_rejects_without_terminal_effect(cockpit):
    from services.controller.cockpit_automation import execute_action
    # A policy recommendation cannot create an automatic terminal capability.
    workflow = cockpit.store.get_workflow("wf-auto")
    workflow["status"] = "running"
    cockpit.store.save_workflow(workflow)
    cockpit.store.save_task({"task_id": "auto-task", "workflow_id": "wf-auto", "node": "build",
        "status": "working", "run_id": "auto-run", "pane_id": "auto-pane", "execution_id": "exec-e2e"})
    action = next(a for a in cockpit.read("wf-auto")["actions"] if a["action_id"] == "auto-task:redrive")
    assert action["automation"]["mode"] == "automatic"
    result = execute_action({"workflow_id": "wf-auto", "action_id": action["action_id"],
        "expected_identity": action["expected_identity"]}, deps=SimpleNamespace(store=cockpit.store), automatic=True)
    assert result["receipt"]["status"] == "rejected"
    assert result["receipt"]["reason_code"] == "unsupported_handler"
    assert cockpit.effects == []


def test_default_policy_keeps_steer_redrive_halt_operable(cockpit):
    wf = cockpit.store.get_workflow('wf-e2e')
    wf['config']['nodes'][0]['control_mode'] = 'automatic'
    cockpit.store.save_workflow(wf)
    open_cockpit(cockpit)
    assert '手动执行' in cockpit.browser("console.log(await page.evaluate(()=>document.getElementById('controllerTabView').innerText));")
    confirm_action(cockpit, 'working:steer', instruction='默认自动策略仍可留下自定义指导')
    assert cockpit.store.list_steers(task_id='working')[0]['instruction'] == '默认自动策略仍可留下自定义指导'
    confirm_action(cockpit, 'working:redrive')
    assert any(effect['kind'] == 'prompt' for effect in cockpit.effects)
    confirm_action(cockpit, 'working:halt')
    assert cockpit.store.get_task('working')['status'] == 'interrupted'


def test_cockpit_dispatch_confirmation_is_durable_before_controller_claim(cockpit):
    from herdr import node_dispatch_store
    cockpit.store.save_workflow({'workflow_id': 'wf-dispatch-confirm', 'status': 'running',
        'execution_id': 'exec-e2e', 'config': {'control_policy': {'mode': 'supervised'},
        'nodes': [{'id': 'build'}]}})
    op = node_dispatch_store.reconcile_workflow(cockpit.store.db_path, 'wf-dispatch-confirm')[0]
    assert node_dispatch_store.claim(cockpit.store.db_path, op['id'], 'test-controller') is None
    open_cockpit(cockpit, 'wf-dispatch-confirm')
    confirm_action(cockpit, f'wf-dispatch-confirm:confirm_dispatch:{op["id"]}')
    approval = node_dispatch_store.operation_for_node(cockpit.store.db_path, 'wf-dispatch-confirm', 'build')
    assert approval['detail']['dispatch_confirmation']['operator'] == 'human_console'
    assert cockpit.read('wf-dispatch-confirm')['receipts'][0]['status'] == 'applied'
    assert node_dispatch_store.claim(cockpit.store.db_path, op['id'], 'test-controller')
    assert node_dispatch_store.start(cockpit.store.db_path, op['id'], 'test-controller')['started']


def test_header_pause_checkpoint_rollback_and_step_use_cockpit_receipts(cockpit):
    from services.controller.cockpit_automation import run_automatic_controller_actions
    cockpit.store.save_workflow({'workflow_id': 'wf-controls', 'status': 'running',
        'execution_id': 'exec-e2e', 'config': {'nodes': [{'id': 'build'}]}})
    open_cockpit(cockpit, 'wf-controls')
    cockpit.browser('await page.evaluate(()=>toggleWorkflowPause());')
    confirm_action(cockpit, 'wf-controls:pause')
    assert cockpit.store.get_workflow('wf-controls')['status'] == 'paused'
    run_automatic_controller_actions('wf-controls', deps=SimpleNamespace(store=cockpit.store))
    assert cockpit.store.get_workflow('wf-controls')['status'] == 'paused'
    confirm_action(cockpit, 'wf-controls:resume_workflow')
    confirm_action(cockpit, 'wf-controls:checkpoint')
    cp = cockpit.store.list_checkpoints('wf-controls')[0]
    assert cockpit.read('wf-controls')['receipts'][0]['status'] == 'applied'
    confirm_action(cockpit, 'wf-controls:rollback:build')
    assert cockpit.store.get_workflow('wf-controls')['history'][-1]['action'] == 'rollback'
    assert cockpit.read('wf-controls')['receipts'][0]['status'] == 'applied'
    confirm_action(cockpit, 'wf-controls:restore_checkpoint:' + cp['checkpoint_id'])
    assert cockpit.read('wf-controls')['receipts'][0]['status'] == 'applied'
    confirm_action(cockpit, 'wf-controls:step')
    assert cockpit.store.get_workflow('wf-controls')['status'] == 'paused'
    assert cockpit.store.list_tasks(workflow_id='wf-controls') == []
    assert '尚未派发任务' in cockpit.read('wf-controls')['receipts'][0]['message']


def test_resume_control_visible_and_executable_with_101_tasks(cockpit):
    from herdr import kernel
    for i in range(101):
        cockpit.store.save_task({'task_id': f'many-{i}', 'workflow_id': 'wf-auto', 'node': 'build',
            'run_id': f'run-{i}', 'pane_id': f'pane-{i}', 'execution_id': 'exec-e2e', 'status': 'working'})
    kernel.pause_workflow('wf-auto', store=cockpit.store)
    open_cockpit(cockpit, 'wf-auto')
    out = cockpit.browser("console.log('CARDS='+await page.evaluate(()=>document.querySelectorAll('[data-action-id]').length));")
    assert int(out.split('CARDS=')[1].splitlines()[0]) <= 100
    confirm_action(cockpit, 'wf-auto:resume_workflow')
    assert cockpit.store.get_workflow('wf-auto')['status'] == 'running'


def test_operation_deep_link_fetches_confirmation_outside_display_window(cockpit):
    from herdr import node_dispatch_store
    wid = 'wf-target'
    cockpit.store.save_workflow({'workflow_id': wid, 'status': 'running', 'execution_id': 'exec-e2e',
        'startup_ready': True, 'config': {'control_policy': {'mode': 'supervised'},
            'nodes': [{'id': f'n{i:03}'} for i in range(105)]}})
    operations = node_dispatch_store.reconcile_workflow(cockpit.store.db_path, wid)
    target = min(operations, key=lambda op: op['id'])
    aid = f"{wid}:confirm_dispatch:{target['id']}"
    assert not any(a['action_id'] == aid for a in cockpit.read(wid)['actions'])
    open_cockpit(cockpit, wid)
    cockpit.browser(f"""
        await page.evaluate(()=>{{ openControllerCockpit({{workflowId:'{wid}',operationId:{target['id']}}}); }});
        await page.waitForFunction(()=>!!document.querySelector('[data-action-id="{aid}"]'),undefined,{{timeout:5000}});
        console.log('CARDS='+await page.evaluate(()=>document.querySelectorAll('[data-action-id]').length));
    """)
    confirm_action(cockpit, aid)
    current = node_dispatch_store.operation_for_node(cockpit.store.db_path, wid, target['payload']['node_id'])
    assert current['detail']['dispatch_confirmation']['identity_key'] == target['identity_key']
