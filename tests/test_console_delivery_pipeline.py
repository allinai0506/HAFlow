#!/usr/bin/env python3
"""Console delivery-pipeline projection (issue #253).

Two layers, both required:

1. The console server must attach `herdr.delivery_pipeline` projections to the
   workflow's tasks and read the candidate publication time from the event
   ledger — no second source of truth.
2. The rendered badge must say「等待集成」plus the gate it is waiting on, and
   the pipeline row must read `commit → integration branch → candidate
   published`, instead of the terminal-looking「已提交」.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CODE_CRAFT_NODES = [
    {"id": "craft", "label": "1代码工坊", "depends_on": []},
    {"id": "cleanroom_test", "label": "2洁净室测试", "depends_on": ["craft"],
     "gate": {"retry_node": "craft"}},
    {"id": "cleanroom_review", "label": "3洁净室评审", "depends_on": ["craft"],
     "gate": {"retry_node": "craft"}},
    {"id": "wrapup", "label": "4收尾",
     "depends_on": ["cleanroom_test", "cleanroom_review"]},
]
COMMIT = "6e5825883b2d9629ce71a7ea6d4d88d33f1df530"
CANDIDATE = "85c3a98eb1973f0fd1656e6557aa78268a067da2"
PUBLISHED_AT = 1792280515.7452


def _load_console():
    path = ROOT / "console" / "herdr_factory_console.py"
    spec = importlib.util.spec_from_loader(
        "herdr_console_delivery_test",
        importlib.machinery.SourceFileLoader("herdr_console_delivery_test", str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract_fn(html, name):
    start = html.find(f"function {name}(")
    if start < 0:
        return None
    i = html.find("{", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start:j + 1]
    return None


def _craft_task(status="committed", **kw):
    task = {
        "task_id": "haflow-simple-workbench-v1-r2",
        "workflow_id": "wf-haflow-1017-01",
        "node": "craft",
        "stage": "craft",
        "status": status,
        "agent": "codex",
        "integration_mode": "git",
        "commit": COMMIT,
        "integration_branch": "herdr/integration-haflow-simple-workbench-v1-r2",
        "candidate_sha": CANDIDATE,
        "pane_id": "w19:p2G",
    }
    task.update(kw)
    return task


def _gate_task(node, verdict):
    return {"task_id": f"wf-haflow-1017-01-{node}-r2", "workflow_id": "wf-haflow-1017-01",
            "node": node, "stage": node, "status": "cleaned",
            "stage_verdict": verdict, "integration_mode": "none"}


class TestConsoleDeliveryProjection(unittest.TestCase):
    """Layer 1: the server attaches the core projection to each task."""

    def setUp(self):
        self.console = _load_console()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"
        for key in ("HERDR_STATE_DB", "TASKS_FILE", "WORKFLOWS_FILE"):
            self.addCleanup(os.environ.pop, key, None)
        os.environ["HERDR_STATE_DB"] = str(self.db)
        os.environ.pop("TASKS_FILE", None)
        os.environ.pop("WORKFLOWS_FILE", None)
        from herdr.state_store import SQLiteStateStore
        self.store = SQLiteStateStore(self.db)

    def _seed(self, wf_status="running", gates=("", "")):
        self.store.save_workflow({
            "workflow_id": "wf-haflow-1017-01", "status": wf_status,
            "config": {"nodes": CODE_CRAFT_NODES},
            "candidate_sha": CANDIDATE,
        })
        craft = _craft_task()
        self.store.save_task(craft)
        for node, verdict in zip(("cleanroom_test", "cleanroom_review"), gates):
            self.store.save_task(_gate_task(node, verdict))
        from herdr.state_db import record_event
        record_event({
            "event_type": "candidate_published", "workflow_id": "wf-haflow-1017-01",
            "task_id": craft["task_id"], "source": "herdr-task", "timestamp": PUBLISHED_AT,
            "payload": {"candidate_sha": COMMIT, "execution_id": "wf-haflow-1017-01"},
        }, db_path=self.db)
        return craft

    def _workflow_tasks(self):
        return self.store.list_tasks(workflow_id="wf-haflow-1017-01")

    def _project(self, craft):
        out = self.console.attach_delivery(
            self._workflow_tasks(), self.store.get_workflow("wf-haflow-1017-01"),
        )
        return next(t for t in out if t["task_id"] == craft["task_id"])["delivery"]

    def test_committed_gate_waiting_task_gets_the_pipeline_projection(self):
        craft = self._seed()
        out = self._project(craft)
        self.assertEqual(out["stage"], "awaiting_integration")
        self.assertEqual(out["stage_label"], "等待集成")
        self.assertTrue(out["scheduler_owned"])
        self.assertEqual([g["id"] for g in out["gates"]],
                         ["cleanroom_test", "cleanroom_review"])
        self.assertEqual(out["commit_short"], "6e582588")
        self.assertEqual(out["integration_mode"], "git")
        self.assertEqual(out["integration_branch"],
                         "herdr/integration-haflow-simple-workbench-v1-r2")
        self.assertEqual(out["candidate_published_at"], PUBLISHED_AT)

    def test_gate_wait_disappears_once_both_gates_passed(self):
        craft = self._seed(gates=("pass", "pass"))
        out = self._project(craft)
        self.assertEqual(out["gates"], [])
        self.assertEqual(out["wait_kind"], "owner")
        self.assertIn("自动集成", out["owner_note"])

    def test_paused_workflow_releases_the_task_to_a_human(self):
        craft = self._seed(wf_status="paused")
        out = self._project(craft)
        self.assertFalse(out["scheduler_owned"])
        self.assertEqual(out["owner"], "human")

    def test_tasks_outside_the_pipeline_are_left_untouched(self):
        craft = _craft_task(status="working")
        self.assertEqual(
            self.console.attach_delivery([craft], {"workflow_id": "wf-haflow-1017-01"}),
            [craft],
        )

    def test_missing_ledger_keeps_the_publication_time_unknown(self):
        self.assertEqual(self.console.candidate_publication_times("wf-absent"), {})


class TestConsoleDeliveryApi(unittest.TestCase):
    """The real read chain: console API → core projection → state.db + ledger."""

    def setUp(self):
        self.console = _load_console()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"
        for key in ("HERDR_STATE_DB", "TASKS_FILE", "WORKFLOWS_FILE"):
            self.addCleanup(os.environ.pop, key, None)
        os.environ["HERDR_STATE_DB"] = str(self.db)
        os.environ.pop("TASKS_FILE", None)
        os.environ.pop("WORKFLOWS_FILE", None)
        from herdr.state_store import SQLiteStateStore
        from herdr.state_db import record_event
        store = SQLiteStateStore(self.db)
        store.save_workflow({
            "workflow_id": "wf-haflow-1017-01", "status": "running",
            "config": {"nodes": CODE_CRAFT_NODES}, "candidate_sha": CANDIDATE,
        })
        craft = _craft_task()
        store.save_task(craft)
        store.save_task(_gate_task("cleanroom_test", ""))
        record_event({
            "event_type": "candidate_published", "workflow_id": "wf-haflow-1017-01",
            "task_id": craft["task_id"], "source": "herdr-task", "timestamp": PUBLISHED_AT,
            "payload": {"candidate_sha": COMMIT, "execution_id": "wf-haflow-1017-01"},
        }, db_path=self.db)

    def test_workflow_detail_ships_the_projection_with_the_tasks(self):
        detail = self.console.workflow_detail("wf-haflow-1017-01", lightweight=True)
        craft = next(t for t in detail["tasks"]
                     if t["task_id"] == "haflow-simple-workbench-v1-r2")
        self.assertEqual(craft["delivery"]["stage_label"], "等待集成")
        self.assertEqual([g["id"] for g in craft["delivery"]["gates"]],
                         ["cleanroom_test", "cleanroom_review"])

    def test_controller_actions_no_longer_offer_a_manual_integrate(self):
        payload = self.console.api_workflow_controller_actions("wf-haflow-1017-01")
        ids = [a["action_id"] for a in payload["actions"]]
        self.assertNotIn("haflow-simple-workbench-v1-r2:integrate", ids, ids)


class TestDeliveryBadgeRendering(unittest.TestCase):
    """Layer 2: the rendered badge no longer reads as a terminal state."""

    @classmethod
    def setUpClass(cls):
        cls.console = _load_console()
        cls.html = getattr(cls.console, "HTML_TEMPLATE", "")

    def _run(self, expr, extra="", extra_fns=()):
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not found; skipping JS execution test")
        fns = ["esc", "humanStatus", "cleanStageLabel", "taskDrawerNumTs",
               "deliveryOf", "deliveryGates", "deliveryReason",
               "deliveryOverridesStatus", "taskStatusLabel", "taskStatusClass",
               "taskBadge", "deliveryWaitHtml", "deliveryClock",
               "deliveryPipelineLine", *extra_fns]
        parts = []
        for fn in fns:
            src = _extract_fn(self.html, fn)
            self.assertIsNotNone(src, f"{fn} not found")
            parts.append(src)
        harness = "\n".join(parts) + f"\n{extra}\nconsole.log(String({expr}));"
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(harness)
            p = f.name
        try:
            res = subprocess.run([node_bin, p], capture_output=True, text=True, timeout=15)
        finally:
            Path(p).unlink(missing_ok=True)
        self.assertEqual(res.returncode, 0, f"node failed: {res.stderr}")
        return res.stdout.strip()

    @staticmethod
    def _delivery(**kw):
        base = {"task_id": "craft-r2", "status": "committed", "stage": "awaiting_integration",
                "stage_label": "等待集成", "scheduler_owned": True,
                "owner_note": "由调度器自动集成，无需人工操作", "wait_kind": "gate",
                "gates": [{"id": "cleanroom_test", "label": "2洁净室测试"},
                          {"id": "cleanroom_review", "label": "3洁净室评审"}],
                "commit_short": "6e582588",
                "integration_branch": "herdr/integration-craft-r2",
                "integration_mode": "git",
                "candidate_short": "85c3a98e", "candidate_published_at": PUBLISHED_AT}
        base.update(kw)
        return base

    @staticmethod
    def _delivery_js(**kw):
        return json.dumps(TestDeliveryBadgeRendering._delivery(**kw), ensure_ascii=False)

    def _task(self, delivery=None, **kw):
        task = {"task_id": "craft-r2", "status": "committed", **kw}
        body = json.dumps(task, ensure_ascii=False)
        if delivery is None:
            return body
        return body[:-1] + ',"delivery":' + delivery + '}'

    def test_badge_says_waiting_for_integration_not_committed(self):
        badge = self._run("taskBadge(T)", f"var T={self._task(self._delivery_js())};")
        self.assertIn("等待集成", badge)
        self.assertNotIn("已提交", badge)

    def test_badge_reason_names_the_pending_gates(self):
        badge = self._run("taskBadge(T)", f"var T={self._task(self._delivery_js())};")
        self.assertIn('title="等待门禁: 洁净室测试、洁净室评审"', badge)

    def test_wait_text_is_visible_next_to_the_badge(self):
        wait = self._run("deliveryWaitHtml(T)",
                         f"var T={self._task(self._delivery_js())};")
        self.assertEqual(wait, '<span class="task-wait" title="等待门禁: 洁净室测试、洁净室评审">'
                               '等待门禁: 洁净室测试、洁净室评审</span>')

    def test_reason_falls_back_to_the_owner_note_once_gates_passed(self):
        wait = self._run("deliveryWaitHtml(T)",
                         f"var T={self._task(self._delivery_js(gates=[], wait_kind='owner'))};")
        self.assertIn("由调度器自动集成，无需人工操作", wait)

    def test_human_owned_task_keeps_its_existing_label(self):
        delivery = self._delivery_js(scheduler_owned=False, owner="human",
                                     wait_kind="", gates=[],
                                     owner_note="由人工决定下一步")
        badge = self._run("taskBadge(T)", f"var T={self._task(delivery)};")
        self.assertIn("已提交", badge)
        self.assertNotIn("等待集成", badge)

    def test_task_without_a_projection_renders_the_raw_status(self):
        badge = self._run("taskBadge(T)", f"var T={self._task(status='working')};")
        self.assertIn("运行中", badge)

    def test_pipeline_line_reads_commit_branch_and_publication_time(self):
        line = self._run("deliveryPipelineLine(T)",
                         f"var T={self._task(self._delivery_js())};")
        expected_time = datetime.fromtimestamp(PUBLISHED_AT).strftime("%m-%d %H:%M")
        self.assertEqual(
            line,
            f"commit=6e582588 → herdr/integration-craft-r2 → 候选发布 {expected_time}",
        )

    def test_pipeline_line_states_the_missing_publication_instead_of_guessing(self):
        line = self._run("deliveryPipelineLine(T)",
                         f"var T={self._task(self._delivery_js(candidate_published_at=None))};")
        self.assertIn("候选发布 时间未记录", line)

    def test_pipeline_line_is_omitted_before_anything_was_committed(self):
        delivery = self._delivery_js(commit_short="", integration_branch="",
                                     candidate_published_at=None)
        line = self._run("deliveryPipelineLine(T)",
                         f"var T={self._task(delivery)};")
        self.assertEqual(line, "")

    def test_pipeline_line_is_omitted_for_a_non_git_task(self):
        """A gate/report task is not part of the git delivery chain."""
        delivery = self._delivery_js(integration_mode="none", integration_branch="")
        line = self._run("deliveryPipelineLine(T)",
                         f"var T={self._task(delivery)};")
        self.assertEqual(line, "")

    def test_inspector_runtime_card_shows_the_delivery_state_and_row(self):
        """The node card is the other place that used to read「已提交」."""
        extra = (
            "var elements={flowInspectorBody:{},flowInspTitle:{},flowInspMeta:{}};\n"
            "var document={getElementById:id=>elements[id]||(elements[id]={}),"
            "querySelectorAll:()=>[],querySelector:()=>null};\n"
            f"var T={self._task(self._delivery_js(), node='craft')};\n"
            "var state={workflow:{tasks:[T],context:{required:[],optional:[]}},"
            "workflowId:'wf-253',flowSelectedNodeId:'craft',flowInspectorTab:'runtime'};\n"
            "function flowNodeById(){return {id:'craft',label:'1代码工坊',node_type:'agent',status:'waiting'};}\n"
            "function humanNodeStatus(s){return s;}\n"
            "function flowNodeStatus(n,s){return s;}\n"
            "renderNodeInspector();\n"
        )
        html = self._run("elements.flowInspectorBody.innerHTML", extra,
                         extra_fns=("flowTasksForNode", "renderNodeInspector"))
        self.assertIn("等待集成", html)
        self.assertIn("等待门禁: 洁净室测试、洁净室评审", html)
        self.assertIn("commit=6e582588 → herdr/integration-craft-r2", html)
        self.assertNotIn("已提交", html)

    def test_cockpit_shows_the_scheduler_attribution_instead_of_a_button(self):
        """No action exists for the step, so the panel must explain who owns it."""
        extra = ("var state={workflow:{tasks:[{task_id:'craft-r2',status:'committed',"
                 "delivery:" + self._delivery_js() + "}]}};")
        html = self._run("renderPipelineActions([])", extra,
                         extra_fns=("taskSequence", "taskKind", "taskDisplayName",
                                    "renderSchedulerAttribution", "renderPipelineActions"))
        self.assertIn("由调度器自动集成，无需人工操作", html)
        self.assertIn("等待门禁: 洁净室测试、洁净室评审", html)
        self.assertNotIn("一键执行", html)

    def test_cockpit_stays_empty_when_nobody_is_waiting(self):
        extra = ("var state={workflow:{tasks:[{task_id:'craft-r2',status:'working'}]}};")
        html = self._run("renderPipelineActions([])", extra,
                         extra_fns=("taskSequence", "taskKind", "taskDisplayName",
                                    "renderSchedulerAttribution", "renderPipelineActions"))
        self.assertEqual(html, "")

    def test_cockpit_does_not_repeat_the_owner_note_once_gates_passed(self):
        delivery = self._delivery_js(gates=[], wait_kind="owner")
        extra = ("var state={workflow:{tasks:[{task_id:'craft-r2',status:'committed',"
                 "delivery:" + delivery + "}]}};")
        html = self._run("renderPipelineActions([])", extra,
                         extra_fns=("taskSequence", "taskKind", "taskDisplayName",
                                    "renderSchedulerAttribution", "renderPipelineActions"))
        self.assertEqual(html.count("由调度器自动集成，无需人工操作"), 1)


if __name__ == "__main__":
    unittest.main()
