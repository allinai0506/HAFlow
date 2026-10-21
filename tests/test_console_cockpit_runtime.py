#!/usr/bin/env python3
"""Runtime smoke test for the Controller cockpit (executed JS, not greps).

`tests/test_console_frontend_syntax.py` runs ``node --check``, which only
proves the script *parses*.  It cannot catch a scope error (a function
referencing a closure variable that no longer exists) or a malformed inline
event-handler attribute, and both of those shipped as fully-green suites:
the cockpit threw ``ReferenceError`` on open, and the decision option chips
carried a ``SyntaxError`` handler.

These tests extract the real ``<script>`` from the console, run it under
Node with a stubbed DOM, and drive ``openControllerCockpitModal()`` and the
option-chip markup against realistic payloads.
"""

import json
import html
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE_SRC = ROOT / "console" / "static" / "console.html"

NODE = shutil.which("node")

# Stubs for everything the cockpit path touches outside pure rendering.
# NOTE: `openModal` is intentionally NOT stubbed — the console defines its own
# and our document stub captures what it renders, so the probe observes the
# markup the real function produced.
PRELUDE = r"""
const __probe = { modal: null };
// Persistent per-id elements: the console reads back what it wrote, so a stub
// that returns a fresh object per call would hide every render.
const __els = {};
function document_getElementById() {
  const id = arguments[0];
  if (!__els[id]) {
    const el = { id: id, textContent: '', innerHTML: '', value: '',
                 style: {}, dataset: {}, hidden: false,
                 classList: { toggle(){}, add(){}, remove(){}, contains(){ return false; } },
                 setAttribute(){}, getAttribute(){ return null; },
                 appendChild(){}, querySelectorAll(){ return []; },
                 addEventListener(){}, focus(){}, remove(){} };
    __els[id] = el;
    // Intercept both the legacy modal body and the new tab panel so
    // __probe.modal always captures the most-recently rendered cockpit HTML.
    if (id === 'modalBody' || id === 'controllerTabView') {
      Object.defineProperty(el, 'innerHTML', {
        set(v) { __probe.modal = v; }, get() { return __probe.modal || ''; },
      });
    }
  }
  return __els[id];
}
globalThis.__els = __els;
globalThis.document = {
  getElementById: document_getElementById,
  querySelectorAll: function(){ return []; },
  querySelector: function(){ return null; },
  addEventListener(){}, createElement(){ return document_getElementById('x'); },
};
globalThis.window = {
  localStorage: { getItem(){ return null; }, setItem(){} },
  addEventListener(){}, removeEventListener(){},
  location: { search: '', href: 'http://127.0.0.1:8765/' },
  matchMedia(){ return { matches: false, addListener(){}, removeListener(){} }; },
  getComputedStyle(){ return { getPropertyValue(){ return ''; } }; },
};
globalThis.localStorage = globalThis.window.localStorage;
globalThis.location = globalThis.window.location;
globalThis.navigator = { clipboard: null, userAgent: 'node' };
globalThis.setInterval = function(){};
globalThis.setTimeout = function(){};
globalThis.requestAnimationFrame = function(){};
globalThis.X6 = { Graph: function(){}, Shape: {}, GraphView: {} };
globalThis.CSS = { escape: function(v){ return String(v); } };
"""


def _script() -> str:
    from console.page import HTML_TEMPLATE
    src = HTML_TEMPLATE
    match = re.search(r"<script>(.*?)</script>", src, re.DOTALL)
    assert match, "<script> block not found in console HTML_TEMPLATE"
    return match.group(1)


def _action(action_id, **over):
    action = {
        "action_id": action_id,
        "title": "集成到基线分支",
        "description": "把交付分支 rebase 到基线并合并",
        "category": "pipeline",
        "group": "pipeline",
        "recommended": True,
        "is_destructive": False,
        "effect": "点按钮后：进入基线",
        "command_line": "bin/herdr-task integrate impl-x",
        "blocker_task_id": "impl-x",
        "api_endpoint": "/api/controller/execute-action",
        "api_payload": {"type": "task_git_step", "task_id": "impl-x", "step": "integrate"},
        "commands": [["integrate", "impl-x"]],
        "command_base": "herdr-task",
        "old_task_id": "", "new_task_id": "", "new_agent": "",
        "stage": "implementation",
        "target": {"workflow_id": "wf-1", "node_id": "build", "task_id": "impl-x", "run_id": "run-1", "pane_id": "pane-1"},
        "expected_identity": {"task_version": 2, "pane_id": "pane-1", "execution_id": "exec-1"},
        "automation": {"mode": "manual", "source": "default", "reason": "manual_policy"},
        "risk": "normal",
    }
    action.update(over)
    return action


def _advice():
    return {
        "note_id": "n-2", "kind": "delivery", "title": "T1 契约地基",
        "summary": "契约已落地", "node": "implementation", "task_id": "",
        "agent": "opencode", "source": "agent", "workflow_id": "wf-1",
        "raised_at": 1700003100.0, "stale": False, "is_decision": False,
    }


def _decision(decision_id="DU-10", options=("入", "不入"), recommended="入"):
    return {
        "decision_id": decision_id,
        "status": "open",
        "title": decision_id,
        "question": "MATCH 是否入 V1？",
        "options": list(options),
        "recommended": recommended,
        "workflow_id": "wf-1",
        "node": "implementation",
        "task_id": "",
        "source": "human",
        "note_id": "n-1",
        "raised_at": 1700003000.0,
    }


def _cockpit():
    return {
        'schema': 'controller-cockpit/v1', 'workflow': {'workflow_id': 'wf-1', 'status': 'running'},
        'controller': {'status': 'running', 'last_check_at': '2026-10-21T10:00:00Z'},
        'read_state': 'confirmed', 'errors': {}, 'missing_sources': [],
        'judgment': {'now': 'build_running', 'blocked_reason': 'dependencies_incomplete', 'next': 'start_review', 'requires_user': True},
        'nodes': [{'id': 'build', 'label': '实现', 'status': 'working', 'tasks': [
            {'task_id': 'impl-x', 'workflow_id': 'wf-1', 'run_id': 'run-1', 'pane_id': 'pane-1',
             'execution_id': 'exec-1', 'version': 2, 'status': 'working'}]}],
        'tasks': [], 'current_nodes': ['build'], 'ready_nodes': [],
        'todos': [{'kind': 'action', 'action_id': 'impl-x:integrate', 'target': {'task_id': 'impl-x'}}],
        'actions': [_action('impl-x:integrate')],
        'recovery': [], 'receipts': [{'workflow_id': 'wf-1', 'action_id': 'past', 'status': 'applied'}],
        'timeline': [{'id': 1, 'workflow_id': 'wf-1', 'event_type': 'workflow_transition', 'timestamp': '2026-10-21T09:00:00Z'}],
    }


@unittest.skipIf(NODE is None, "node executable not found in PATH")
class TestControllerCockpitRuntime(unittest.TestCase):
    """Drive the real JS; a green ``node --check`` is not enough."""

    def test_projection_renders_service_judgment_identity_and_receipts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, f"""
                state.workflowId='wf-1';state.workflow={{workflow:{{workflow_id:'wf-1'}}}};
                state.controllerCockpit={json.dumps(_cockpit(), ensure_ascii=False)};
                openControllerCockpit({{workflowId:'wf-1',taskId:'impl-x',runId:'run-1'}});
                console.log('HTML='+__probe.modal);
            """)
        for token in ('调度服务', '2026-10-21T10:00:00Z', 'build_running', 'dependencies_incomplete',
                      'start_review', 'run-1', 'pane-1', 'impl-x', '待办', '执行记录', '已生效'):
            self.assertIn(token, out)

    def test_unknown_projection_never_claims_empty_or_healthy(self):
        import tempfile
        data = {**_cockpit(), 'read_state': 'unknown', 'todos': [], 'actions': [], 'controller': {'status': 'unknown'}}
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, f"""
                state.workflowId='wf-1';state.workflow={{workflow:{{workflow_id:'wf-1'}}}};
                state.controllerCockpit={json.dumps(data, ensure_ascii=False)};
                openControllerCockpit({{workflowId:'wf-1'}});console.log('HTML='+__probe.modal);
            """)
        self.assertIn('role="alert"', out)
        self.assertIn('读取未确认', out)
        self.assertNotIn('暂无待办', out)
        self.assertNotIn('调度运转中', out)

    def test_receipts_distinguish_waiting_partial_and_rejection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, """
                for(const status of ['applied','accepted_waiting','partial','rejected'])
                  console.log(renderControllerReceipt({workflow_id:'wf-1',action_id:'a',status,
                    applied_steps:['force_pass'],failed_step:'advance',reason_code:'identity_changed'}));
            """)
        for text in ('已生效', '已受理，等待核验', '部分生效', '已拒绝', 'force_pass', 'advance', '刷新'):
            self.assertIn(text, out)

    def test_slow_cockpit_response_cannot_replace_new_workflow(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, """
                (async()=>{
                  let finish;globalThis.fetch=()=>new Promise(resolve=>finish=resolve);
                  state.workflowId='wf-1';state.workflow={workflow:{workflow_id:'wf-1'}};
                  const reading=loadControllerCockpit('wf-1');
                  state.workflowId='wf-2';state.workflow={workflow:{workflow_id:'wf-2'}};
                  finish({ok:true,json:async()=>({ok:true,data:{workflow:{workflow_id:'wf-1'}}})});
                  await reading;console.log('STALE='+Boolean(state.controllerCockpit?.workflow?.workflow_id==='wf-1'));
                })();
            """)
        self.assertIn('STALE=false', out)

    def test_action_posts_server_identity_and_keeps_rejected_card_after_refresh(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, """
                (async()=>{
                  state.workflowId='wf-1';state.workflow={workflow:{workflow_id:'wf-1'}};
                  state.controllerCockpit=__cockpitFixture;openControllerCockpit({workflowId:'wf-1'});
                  const calls=[];
                  globalThis.fetch=async(path,options)=>{
                    calls.push({path,body:options.body&&JSON.parse(options.body)});
                    return {ok:true,json:async()=>({ok:true,data:options.method==='POST'?
                      {receipt:{workflow_id:'wf-1',action_id:'impl-x:integrate',status:'rejected',reason_code:'identity_changed'}}:
                      {...__cockpitFixture,actions:[],todos:[]}})};
                  };
                  executeCockpitAction('impl-x:integrate');
                  await document.getElementById('cockpitActionConfirm').onclick();
                  console.log('CALLS='+JSON.stringify(calls));console.log('HTML='+document.getElementById('controllerTabView').innerHTML);
                })();
            """)
        calls = json.loads(out.split('CALLS=', 1)[1].split('\n')[0])
        self.assertEqual(calls[0]['path'], '/api/controller/execute-action')
        self.assertEqual(calls[0]['body']['action_id'], 'impl-x:integrate')
        self.assertEqual(calls[0]['body']['expected_identity'], {'task_version': 2, 'pane_id': 'pane-1', 'execution_id': 'exec-1'})
        self.assertNotIn('type', calls[0]['body'])
        self.assertIn('controller-cockpit?workflow_id=wf-1', calls[1]['path'])
        self.assertIn('此操作卡片已过期', out)
        self.assertIn('disabled', out)

    def test_waiting_receipt_survives_unrelated_receipts_until_operation_resolves(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, """
                (async()=>{
                  state.workflowId='wf-1';state.workflow={workflow:{workflow_id:'wf-1'}};
                  state.controllerReceipt={workflow_id:'wf-1',action_id:'a',status:'accepted_waiting',operation_id:42};
                  let resolved=false;
                  globalThis.fetch=async()=>({ok:true,json:async()=>({ok:true,data:{...__cockpitFixture,
                    receipts:[{workflow_id:'wf-1',action_id:'a',status:'applied',operation_id:41}],
                    recovery:[{id:42,status:resolved?'resolved':'waiting'}]}})});
                  await loadControllerCockpit('wf-1');console.log('WAITING='+state.controllerReceipt?.status);
                  resolved=true;await loadControllerCockpit('wf-1');console.log('RESOLVED='+Boolean(state.controllerReceipt));
                })();
            """)
        self.assertIn('WAITING=accepted_waiting', out)
        self.assertIn('RESOLVED=false', out)

    def test_automatic_and_blocked_actions_cannot_be_executed_in_browser(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, """
                state.workflowId='wf-1';state.workflow={workflow:{workflow_id:'wf-1'}};
                state.controllerCockpit=__cockpitFixture;
                let calls=0;globalThis.fetch=async()=>{calls++;throw new Error('unexpected action')};
                for(const mode of ['automatic','blocked','unknown',undefined]){
                  state.controllerCockpit.actions[0].automation.mode=mode;
                  executeCockpitAction('impl-x:integrate');
                }
                console.log('CALLS='+calls);console.log('MODAL='+Boolean(__probe.modal));
            """)
        self.assertIn('CALLS=0', out)
        self.assertIn('MODAL=false', out)

    def test_recovery_form_uses_cockpit_operation_and_preserves_confirmations(self):
        import tempfile
        operation = {'id': 7, 'workflow_id': 'wf-1', 'version': 2, 'status': 'waiting_human',
                     'payload': {'node_id': 'build'}, 'recovery': {
                         'summary': '源仓库改动需要核查', 'next_step': '核查旧任务后恢复',
                         'node_label': '实现', 'candidate_sha': 'candidate-1', 'explanation': '不能自动重发',
                         'actions': [{'action': 'restore_scope', 'label': '确认恢复范围'}]}}
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, f"""
                state.workflowId='wf-1';state.workflow={{workflow:{{workflow_id:'wf-1'}}}};
                state.controllerCockpit=__cockpitFixture;
                state.controllerCockpit.recovery=[{json.dumps(operation, ensure_ascii=False)}];
                state.controllerCockpit.todos=[{{kind:'recovery',...state.controllerCockpit.recovery[0]}}];
                openControllerCockpit({{workflowId:'wf-1',operationId:7}});
                console.log('CARD='+document.getElementById('controllerTabView').innerHTML);
                openRecoveryDecision(7,'restore_scope');console.log('FORM='+__probe.modal);
            """)
        for token in ('源仓库改动需要核查', '确认恢复范围', 'recoveryOperator', 'recoveryReason',
                      'recoveryConfirmed', 'candidate-1'):
            self.assertIn(token, out)

    def test_explicit_workflow_target_survives_reader_repaint_and_bad_identity_is_unknown(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, """
                (async()=>{
                  state.workflowId='old';state.workflow={workflow:{workflow_id:'old'}};
                  let mismatch=false;
                  globalThis.fetch=async()=>({ok:true,json:async()=>({ok:true,data:{...__cockpitFixture,
                    workflow:{workflow_id:mismatch?'foreign':'wf-1'}}})});
                  await openControllerCockpit({workflowId:'wf-1',nodeId:'build'});
                  console.log('TARGET='+state.controllerTarget.workflowId);
                  console.log('LOADED='+state.controllerCockpit?.workflow?.workflow_id);
                  mismatch=true;await loadControllerCockpit('wf-1');
                  console.log('BAD='+state.controllerCockpit?.read_state);
                })();
            """)
        self.assertIn('TARGET=wf-1', out)
        self.assertIn('LOADED=wf-1', out)
        self.assertIn('BAD=unknown', out)

    def test_high_risk_reason_and_current_projection_are_required_at_confirmation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, """
                (async()=>{
                  state.workflowId='wf-1';state.workflow={workflow:{workflow_id:'wf-1'}};
                  state.controllerCockpit=__cockpitFixture;openControllerCockpit({workflowId:'wf-1'});
                  state.controllerCockpit.actions[0].risk='evidence_bypass';
                  let calls=0;globalThis.fetch=async()=>{calls++;throw new Error('unexpected post')};
                  executeCockpitAction('impl-x:integrate');
                  await document.getElementById('cockpitActionConfirm').onclick();
                  console.log('REASON='+document.getElementById('cockpitActionError').textContent);
                  document.getElementById('cockpitReason').value='已核验';
                  state.controllerCockpit.read_state='unknown';
                  await document.getElementById('cockpitActionConfirm').onclick();
                  console.log('UNKNOWN='+document.getElementById('cockpitActionError').textContent);
                  console.log('CALLS='+calls);
                })();
            """)
        self.assertIn('REASON=必须填写人工核验依据', out)
        self.assertIn('UNKNOWN=目标已变化，请刷新', out)
        self.assertIn('CALLS=0', out)

    def test_decision_submission_uses_workflow_identity_not_controller_tab_id(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run_in(tmp, f"""
                (async()=>{{
                  state.workflowId='wf-1';state.workflow={{workflow:{{workflow_id:'wf-1'}}}};
                  state.controllerCockpit=__cockpitFixture;
                  state.decisionData={{workflow_id:'wf-1',decisions:[{json.dumps(_decision(), ensure_ascii=False)}],advice:[]}};
                  openControllerCockpit({{workflowId:'wf-1'}});document.getElementById('decChosen').value='入';
                  globalThis.fetch=async(path,options)=>{{console.log('BODY='+options.body);throw new Error('stop at HTTP boundary')}};
                  await submitDecision('DU-10');
                  state.decisionData.workflow_id='foreign';openControllerCockpit({{workflowId:'wf-1'}});
                  console.log('FOREIGN='+document.getElementById('controllerTabView').innerHTML.includes('DU-10'));
                }})();
            """)
        body = json.loads(out.split('BODY=', 1)[1].split('\n')[0])
        self.assertEqual(body['workflow_id'], 'wf-1')
        self.assertIn('FOREIGN=false', out)

    def test_cockpit_opens_and_renders_pipeline_section_without_blockers(self):
        """The reported bug: healthy-but-unintegrated, zero blockers.

        Regression guard for a ``ReferenceError`` that shipped green: the card
        helper was hoisted to module scope but still referenced the enclosing
        function's ``catMeta``, so the whole modal failed to open.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            driver = f"""
            state.workflowId = 'wf-1';
            state.workflow = {{workflow:{{workflow_id:'wf-1'}},stages:[],stall:null}};
            state.controllerActionsData = {{
              workflow_id:'wf-1', blockers:[],
              actions: {json.dumps([_action("impl-x:integrate")], ensure_ascii=False)},
            }};
            state.decisionData = {{workflow_id:'wf-1',decisions:[],advice:[]}};
            try {{
              state.controllerCockpit=__cockpitFixture;
              state.controllerCockpit.actions=state.controllerActionsData.actions;
              openControllerCockpitModal();
              console.log('OPEN_OK');
              console.log('HAS_PIPELINE=' + (String(__probe.modal||'').indexOf('继续推进') >= 0));
            }} catch (e) {{
              console.log('THREW ' + e.constructor.name + ': ' + e.message);
            }}
            """
            out = self._run_in(tmp, driver)
        self.assertIn("OPEN_OK", out, out)
        self.assertNotIn("THREW", out, out)
        self.assertIn("HAS_PIPELINE=true", out, out)

    def test_cockpit_renders_blocker_cards_too(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            driver = f"""
            state.workflowId = 'wf-1';
            state.workflow = {{workflow:{{workflow_id:'wf-1'}},stages:[],stall:null}};
            state.controllerActionsData = {{
              workflow_id:'wf-1', blockers:[{{task_id:'impl-x',status:'failed',
                stage_verdict:'blocked',stage_verdict_note:'FAIL',agent:'codex'}}],
              actions: {json.dumps([_action("impl-x:relaunch_with_agent", group="blocker", category="rework", blocker_task_id="impl-x")], ensure_ascii=False)},
            }};
            state.decisionData = {{workflow_id:'wf-1',decisions:[],advice:[]}};
            try {{
              state.controllerCockpit=__cockpitFixture;
              state.controllerCockpit.actions=state.controllerActionsData.actions;
              openControllerCockpitModal();
              console.log('OPEN_OK');
              console.log('HAS_UNBLOCK=' + (String(__probe.modal||'').indexOf('调度待办') >= 0));
            }} catch (e) {{
              console.log('THREW ' + e.constructor.name + ': ' + e.message);
            }}
            """
            out = self._run_in(tmp, driver)
        self.assertIn("OPEN_OK", out, out)
        self.assertNotIn("THREW", out, out)
        self.assertIn("HAS_UNBLOCK=true", out, out)

    def test_dashboard_renders_decision_rows_and_advice(self):
        """The dashboard is the landing surface; it must show the same asks."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            driver = f"""
            state.dash = {{
              tasks:[], attention:[], deliveries:[], stuck:[], decisions:{json.dumps([_decision()], ensure_ascii=False)},
              counts:{{tasks:0,attention:0,decisions:1,deliveries:0,stuck:0}},
              generated_at_text:'-', scope:'all',
              workflows:[{{workflow_id:'wf-1',title:'统一待办 V1',active:1,attention:1}}],
            }};
            renderDashboard();
            const html = document.getElementById('tasks').innerHTML;
            console.log('HAS_SEC=' + (html.indexOf('待你裁决') >= 0));
            console.log('HAS_DU=' + (html.indexOf('DU-10') >= 0));
            console.log('HAS_Q=' + (html.indexOf('MATCH') >= 0));
            console.log('HAS_WF=' + (html.indexOf('wf-1') >= 0));
            """
            out = self._run_in(tmp, driver)
        self.assertNotIn("THREW", out, out)
        for token in ("HAS_SEC=true", "HAS_DU=true", "HAS_Q=true", "HAS_WF=true"):
            self.assertIn(token, out, out)

    def test_dashboard_decision_button_targets_the_decision_workflow(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            driver = f"""
            state.dash = {{
              tasks:[], attention:[], deliveries:[], stuck:[],
              decisions:{json.dumps([_decision()], ensure_ascii=False)},
              counts:{{tasks:0,attention:0,decisions:1,deliveries:0,stuck:0}},
              generated_at_text:'-', scope:'all', workflows:[],
            }};
            renderDashboard();
            console.log('HTML=' + document.getElementById('tasks').innerHTML);
            """
            out = self._run_in(tmp, driver)
        html = out.split("HTML=", 1)[1]
        # A ruling is workflow-scoped: clicking must target wf-1, not the
        # dashboard's workflow selector.
        self.assertIn("dashOpenWorkflowOf('wf-1')", html)
        self.assertNotIn('onclick="dashOpenWorkflow()"', html)

    def test_cockpit_renders_decision_panel_and_paused_resume(self):
        import tempfile

        resume = _action("wf-1:resume_workflow", blocker_task_id="", stage="",
                         title="恢复工作流调度", effect="把 wf-1 从 paused 恢复为可调度",
                         commands=[], api_endpoint="/api/kernel/resume",
                         api_payload={"workflow_id": "wf-1", "node_id": None})
        with tempfile.TemporaryDirectory() as tmp:
            driver = f"""
            state.workflowId = 'wf-1';
            state.workflow = {{workflow:{{workflow_id:'wf-1'}},stages:[],stall:null}};
            state.controllerActionsData = {{
              workflow_id:'wf-1', blockers:[],
              actions: {json.dumps([resume], ensure_ascii=False)},
            }};
            state.decisionData = {{
              workflow_id:'wf-1',
              decisions: {json.dumps([_decision()], ensure_ascii=False)},
              advice: {json.dumps([_advice()], ensure_ascii=False)},
            }};
            try {{
              state.controllerCockpit=__cockpitFixture;
              state.controllerCockpit.actions=state.controllerActionsData.actions;
              openControllerCockpitModal();
              console.log('OPEN_OK');
              const m = String(__probe.modal||'');
              console.log('HAS_DECISION=' + (m.indexOf('待你裁决') >= 0));
              console.log('HAS_DU10=' + (m.indexOf('DU-10') >= 0));
              console.log('HAS_ADVICE=' + (m.indexOf('总指挥最新建议') >= 0));
              console.log('HAS_RESUME=' + (m.indexOf('恢复工作流调度') >= 0));
            }} catch (e) {{
              console.log('THREW ' + e.constructor.name + ': ' + e.message);
            }}
            """
            out = self._run_in(tmp, driver)
        self.assertNotIn("THREW", out, out)
        for token in ("OPEN_OK", "HAS_DECISION=true", "HAS_DU10=true",
                      "HAS_ADVICE=true", "HAS_RESUME=true"):
            self.assertIn(token, out, out)

    def test_decision_option_chips_carry_valid_inline_handlers(self):
        """`JSON.stringify` emits double quotes inside a double-quoted attribute.

        The HTML parser terminates the handler at the first quote, so the
        chip shipped as a `SyntaxError`.  Every inline handler the cockpit
        generates must survive `node --check` as a standalone expression.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            driver = f"""
            state.workflowId = 'wf-1';
            state.decisionData = {{workflow_id:'wf-1',
              decisions: {json.dumps([_decision(options=('入 V1', '不入 V1'), recommended='入 V1')], ensure_ascii=False)},
              advice:[]}};
            try {{ openDecisionPanel('DU-10'); console.log('OPEN_OK'); }}
            catch (e) {{ console.log('THREW ' + e.constructor.name + ': ' + e.message); }}
            console.log('MODAL=' + String(__probe.modal||''));
            """
            out = self._run_in(tmp, driver)
        self.assertIn("OPEN_OK", out, out)
        assert "MODAL=" in out
        modal = out.split("MODAL=", 1)[1]
        self.assertIn("decChosen", modal)
        handlers = re.findall(r'onclick="([^"]*)"', modal)
        self.assertTrue(handlers, "expected inline handlers in the decision modal")
        with tempfile.TemporaryDirectory() as tmp:
            for i, handler in enumerate(handlers):
                f = Path(tmp) / f"h{i}.js"
                f.write_text(f"({html.unescape(handler)});", encoding="utf-8")
                res = subprocess.run([NODE, "--check", str(f)],
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(
                    res.returncode, 0,
                    f"inline handler {i} is not valid JS: {handler!r}\n{res.stderr[:400]}",
                )

    def test_every_inline_handler_in_rendered_markup_is_valid_js(self):
        """Attribute-level regression guard for the whole cockpit surface.

        A double quote inside a double-quoted ``onclick`` truncates the handler
        and emits a junk attribute.  Rendering the cockpit and compiling every
        handler it produced catches that for all card types at once.
        """
        import tempfile

        actions = [
            _action("impl-x:integrate"),
            _action("impl-x:redrive", category="recovery", group="pipeline",
                    effect="向工位重推提示",
                    command_line="herdr agent prompt w1:p1 'x' --wait"),
            _action("impl-x:halt", category="recovery", group="pipeline",
                    is_destructive=True, effect="中断任务",
                    command_line="bin/herdr-task halt impl-x"),
            _action("wf-1:resume_workflow", blocker_task_id="", stage="",
                    title="恢复工作流调度", effect="恢复调度", commands=[],
                    api_endpoint="/api/kernel/resume",
                    api_payload={"workflow_id": "wf-1", "node_id": None}),
            _action("impl-y:relaunch_with_agent", group="blocker",
                    category="rework", blocker_task_id="impl-y",
                    new_task_id="impl-y-r2", new_agent="codex",
                    old_task_id="impl-y", effect="换人重派"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            driver = f"""
            state.workflowId = 'wf-1';
            state.workflow = {{workflow:{{workflow_id:'wf-1'}},stages:[],stall:null}};
            state.controllerActionsData = {{
              workflow_id:'wf-1',
              blockers:[{{task_id:'impl-y',status:'failed',stage_verdict:'blocked',
                         stage_verdict_note:'FAIL',agent:'codex',stage:'test'}}],
              actions: {json.dumps(actions, ensure_ascii=False)},
            }};
            state.decisionData = {{workflow_id:'wf-1',
              decisions:{json.dumps([_decision()], ensure_ascii=False)}, advice:[]}};
            state.controllerCockpit=__cockpitFixture;
            state.controllerCockpit.actions=state.controllerActionsData.actions;
            openControllerCockpitModal();
            console.log('MARKUP=' + String(__probe.modal || ''));
            """
            out = self._run_in(tmp, driver)
        markup = out.split("MARKUP=", 1)[1]
        handlers = re.findall(r'onclick="([^"]*)"', markup)
        self.assertGreaterEqual(len(handlers), 5, "expected handlers for every card")
        with tempfile.TemporaryDirectory() as tmp:
            for i, handler in enumerate(handlers):
                f = Path(tmp) / f"h{i}.js"
                f.write_text(f"({html.unescape(handler)});", encoding="utf-8")
                res = subprocess.run([NODE, "--check", str(f)],
                                     capture_output=True, text=True, timeout=30)
                self.assertEqual(
                    res.returncode, 0,
                    f"handler {i} invalid: {handler!r}\n{res.stderr[:300]}",
                )

    def test_js_arg_survives_html_attribute_parsing_and_round_trips(self):
        """jsArg output goes through an HTML attribute parser before JS sees it.

        The browser decodes entities inside the attribute value, so the emitted
        literal is only correct if it is entity-encoded; and whatever the parser
        hands to JS must still evaluate back to the original value.
        """
        import tempfile

        cases = ["wf-1", "a\'b", 'a"b', "a\\b", "a&b", "a;b", ""]
        with tempfile.TemporaryDirectory() as tmp:
            driver = """
            const cases = %s;
            const decode = s => s.replace(/&quot;/g, '"').replace(/&amp;/g, '&');
            let broken = 0, bad = [];
            for (const c of cases) {
              const attr = 'onclick="f(' + jsArg(c) + ')"';
              const m = attr.match(/^onclick="([^"]*)"$/);
              if (!m) { broken++; bad.push('TRUNCATED:' + c); continue; }
              // What the browser would hand to the JS engine.
              const asJs = decode(m[1]);
              let value = null;
              try {
                // `f` is the real handler's callee; define it so we can read
                // the argument the browser would actually pass.
                value = (new Function(
                  'var f=function(x){return x}; return ' + asJs
                ))();
              } catch (e) { broken++; bad.push('SYNTAX:' + c + ' -> ' + asJs); continue; }
              if (String(value) !== c) { broken++; bad.push('MISMATCH:' + c + ' -> ' + value); }
            }
            console.log('BROKEN=' + broken);
            if (bad.length) console.log('DETAIL=' + bad.join(' ; '));
            """ % json.dumps(cases, ensure_ascii=False)
            out = self._run_in(tmp, driver)
        self.assertIn("BROKEN=0", out, out)

    def _run_in(self, tmpdir, driver):
        path = Path(tmpdir) / "cockpit.js"
        path.write_text(PRELUDE + _script() + "\nconst __cockpitFixture=" + json.dumps(_cockpit(), ensure_ascii=False) + ";\n" + textwrap.dedent(driver), encoding="utf-8")
        result = subprocess.run([NODE, str(path)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[:2000])
        return result.stdout


if __name__ == "__main__":
    unittest.main()
