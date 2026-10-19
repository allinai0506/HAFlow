from tests.support.console_source import console_source
#!/usr/bin/env python3
"""Frontend contracts for the Controller decision + pipeline surfaces.

Guards the reported gaps:
- the Controller cockpit rendered "no blockers" while a committed task
  still needed `integrate`, so pipeline actions had no section at all;
- open decisions (Barrier-0 rulings such as DU-10) never reached the UI;
- a paused workflow had no resume button in the cockpit.
"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONSOLE_SRC = console_source(ROOT)


def _js() -> str:
    match = re.search(r"<script>(.*?)</script>", CONSOLE_SRC, re.DOTALL)
    assert match, "<script> block not found in console HTML_TEMPLATE"
    return match.group(1)


class TestConsoleDecisionAndPipelineUI(unittest.TestCase):
    def test_controller_cockpit_has_a_pipeline_section(self):
        js = _js()
        self.assertIn("renderPipelineActions", js)
        self.assertIn("继续推进", js)
        self.assertIn("a.group==='pipeline'", js)

    def test_pipeline_actions_are_not_hidden_when_there_are_no_blockers(self):
        """Regression guard: the old code only rendered cards when blockers existed.

        The pipeline section must be derived from the full action list, never
        from the blocker-conditioned branch.
        """
        js = _js()
        self.assertIn("function renderPipelineActions(acts)", js)
        self.assertIn("const pipelineActs=acts.filter(a=>a.group==='pipeline')", js)
        # The blocker branch must not be the only source of cards any more.
        self.assertIn("const pipelineSection=renderPipelineActions(allActs)", js)
        self.assertIn("${decisionSection}${unblockSection}${pipelineSection}", js)
        # The empty-blocker message must point at the pipeline section instead
        # of claiming there is nothing to do.
        self.assertIn("继续推进”一节", js)

    def test_cockpit_renders_decision_cards_with_resolve_buttons(self):
        js = _js()
        for token in ("待你裁决", "decision_id", "openDecisionPanel", "submitDecision"):
            self.assertIn(token, js, f"missing decision UI token: {token}")

    def test_cockpit_renders_paused_workflow_resume(self):
        """A paused workflow is the reason nothing dispatches; it needs a button."""
        js = _js()
        self.assertIn(":resume_workflow", js)
        self.assertIn("const resumeAct=pipelineActs.find", js)

    def test_cockpit_renders_coordinator_advice_timeline(self):
        js = _js()
        self.assertIn("总指挥最新建议", js)
        self.assertIn("/api/workflow/decisions", js)

    def test_decision_api_endpoints_are_wired(self):
        self.assertIn("/api/workflow/decision/raise", CONSOLE_SRC)
        self.assertIn("if p=='/api/workflow/decisions'", CONSOLE_SRC)
        self.assertIn("if p=='/api/workflow/decision'", CONSOLE_SRC)

    def test_dashboard_renders_decision_section(self):
        js = _js()
        self.assertIn("待你裁决", js)
        self.assertIn("d.decisions", js)

    def test_dashboard_decision_buttons_open_the_decisions_own_workflow(self):
        """A ruling is workflow-scoped; the button must not use the dropdown."""
        js = _js()
        self.assertIn("function dashOpenWorkflowOf(wid)", js)
        self.assertIn("dashOpenWorkflowOf(", js)
        self.assertIn("openControllerCockpitModal();", js)
        # No decision button may fall back to the bare selector-based opener.
        for line in js.splitlines():
            if "去拍板" in line or "进入工作流裁决" in line:
                self.assertIn("dashOpenWorkflowOf(", line, line)

    def test_decision_css_tokens_exist(self):
        self.assertIn(".ctl-decision", CONSOLE_SRC)
        self.assertIn(".ctl-dec-opt", CONSOLE_SRC)

    def test_workflow_load_fetches_decisions(self):
        js = _js()
        # Execute the production loader rather than accepting a dead if(false) string.
        import subprocess
        block = js[js.index("const workflowExtraRequests=new Map();"):js.index("function clearWorkflow()")]
        harness = r"""
const assert=require('node:assert/strict');
const state={workflowLoadSequence:1,workflow:{workflow:{workflow_id:'wf-decisions'}}};
const calls=[];
function api(path){
 calls.push(path);
 return Promise.resolve(path.startsWith('/api/workflow/decisions?')?
  {decisions:[{decision_id:'d-real',workflow_id:'wf-decisions'}]}:{});
}
const renderTasks=()=>{},renderFlowWorkbench=()=>{},renderDeliverables=()=>{},renderNodeInspector=()=>{};
""" + block + r"""
loadWorkflowExtras('wf-decisions',1);
setImmediate(()=>{
 assert(calls.includes('/api/workflow/decisions?workflow_id=wf-decisions'));
 assert.equal(state.decisionData.decisions[0].decision_id,'d-real');
 assert.equal(state.decisionData.decisions[0].workflow_id,'wf-decisions');
});
"""
        result=subprocess.run(['node','-e',harness],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__ == "__main__":
    unittest.main()
