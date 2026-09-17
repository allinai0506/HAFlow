"""Fix-loop PR-2 tests: verdicts, gate resolution, atomic invalidation,
fix-loop triggers, close-workflow gate, and console bypass guards."""

import importlib.machinery
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _load_module("herdr_task_gates_test", HERDR_ROOT / "bin" / "herdr-task")
_ctl = _load_module(
    "herdr_controller_gates_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)
_con = _load_module(
    "herdr_console_gates_test",
    HERDR_ROOT / "console" / "herdr_factory_console.py",
)


def _resp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _task(task_id="t1", status="agent_done", stage="review",
          workflow_id="wf-1", **extra):
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "node": stage,
        "stage": stage,
        "status": status,
        "status_history": [],
        "agent": "codex",
        "pane_id": "wA:pZ",
    }
    task.update(extra)
    return task


class SetVerdictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-verdict-")
        _ht.TASKS_FILE = str(Path(self.tmp.name) / "tasks.json")

    def _registry(self):
        return json.loads(Path(_ht.TASKS_FILE).read_text())["tasks"][0]

    def _write(self, task):
        Path(_ht.TASKS_FILE).write_text(
            json.dumps({"tasks": [task]}, ensure_ascii=False)
        )

    def test_pass_verdict_persisted(self):
        self._write(_task())
        _ht.set_status("t1", "completed", verdict="pass")
        self.assertEqual(self._registry()["stage_verdict"], "pass")

    def test_blocked_with_note_persisted(self):
        self._write(_task())
        _ht.set_status(
            "t1", "completed",
            verdict="blocked",
            note="B1: fail-strict 补偿链断裂",
        )
        row = self._registry()
        self.assertEqual(row["stage_verdict"], "blocked")
        self.assertEqual(row["stage_verdict_note"], "B1: fail-strict 补偿链断裂")

    def test_blocked_requires_note(self):
        self._write(_task())
        with self.assertRaises(SystemExit):
            _ht.set_status("t1", "completed", verdict="blocked")

    def test_verdict_rejected_on_non_completed(self):
        self._write(_task(status="pending"))
        with self.assertRaises(SystemExit):
            _ht.set_status("t1", "dispatched", verdict="pass")

    def test_same_status_completed_still_persists_verdict(self):
        # 已 completed 的任务补落 verdict(同状态 early-return 不得吞掉)。
        self._write(_task())
        _ht.set_status("t1", "completed")
        _ht.set_status(
            "t1", "completed",
            verdict="blocked",
            note="B1 阻断",
        )
        row = self._registry()
        self.assertEqual(row["stage_verdict"], "blocked")
        self.assertEqual(row["stage_verdict_note"], "B1 阻断")


class BuildFixLoopMessageTest(unittest.TestCase):
    def _item(self, **overrides):
        item = {
            "workflow_id": "wf-1",
            "gate_stage": "review",
            "retry_node": "implementation",
            "blockers": [{"task_id": "rev1", "note": "B1 阻断"}],
            "invalidated": ["rev1"],
            "loop_count": 1,
            "max_loops": 3,
            "suggested_branch": "agent/x/pr",
        }
        item.update(overrides)
        return item

    def test_contains_onto_blockers_and_skeleton(self):
        message = _ctl.build_fix_loop_message(self._item(), "nexusarchive")

        self.assertIn("HERDR_CONTROLLER_FIX_LOOP_EVENT", message)
        self.assertIn("--onto agent/x/pr", message)
        self.assertIn("rev1: B1 阻断", message)
        self.assertIn("--task-type fix", message)
        self.assertNotIn("注意:已达 fix-loop 上限", message)

    def test_missing_branch_omits_onto_flag(self):
        message = _ctl.build_fix_loop_message(
            self._item(suggested_branch=None), "nexusarchive"
        )

        self.assertNotIn("--onto", message)
        self.assertIn("(未找到", message)

    def test_escalation_at_loop_limit(self):
        message = _ctl.build_fix_loop_message(
            self._item(loop_count=3, max_loops=3), "nexusarchive"
        )

        self.assertIn("注意:已达 fix-loop 上限(3/3)", message)
        self.assertIn("先向用户请示", message)

    def test_contains_efficiency_discipline(self):
        # 2026-09-17 效率优化:fix-loop 事件同样携带效率纪律。
        message = _ctl.build_fix_loop_message(self._item(), "nexusarchive")

        self.assertIn("效率纪律", message)
        self.assertIn("立即结束本回合", message)
        self.assertIn("/compact", message)


class GateVerdictTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-gateverdict-")
        _ctl.TASKS_FILE = str(Path(self.tmp.name) / "tasks.json")

    def _write(self, tasks):
        Path(_ctl.TASKS_FILE).write_text(
            json.dumps({"tasks": tasks}, ensure_ascii=False)
        )

    def test_no_verdict_returns_none(self):
        self._write([_task()])
        self.assertIsNone(_ctl.gate_verdict("wf-1", "review"))

    def test_pass_returned(self):
        self._write([_task(stage_verdict="pass")])
        self.assertEqual(_ctl.gate_verdict("wf-1", "review"), "pass")

    def test_blocked_wins_over_pass(self):
        self._write(
            [
                _task("t1", stage_verdict="pass"),
                _task("t2", stage_verdict="blocked"),
            ]
        )
        self.assertEqual(_ctl.gate_verdict("wf-1", "review"), "blocked")

    def test_superseded_blocked_ignored(self):
        self._write(
            [
                _task("t1", status="superseded", stage_verdict="blocked"),
                _task("t2", stage_verdict="pass"),
            ]
        )
        self.assertEqual(_ctl.gate_verdict("wf-1", "review"), "pass")


class ResolveGateConfigTest(unittest.TestCase):
    def test_node_gate_wins(self):
        cfg = _ctl.resolve_gate_config(
            {"gate": {"retry_node": "wrapup", "max_loops": 2}}, "review"
        )
        self.assertEqual(cfg, {"retry_node": "wrapup", "max_loops": 2})

    def test_builtin_default_for_known_gate_stage(self):
        cfg = _ctl.resolve_gate_config({}, "review")
        self.assertEqual(cfg["retry_node"], "implementation")
        self.assertEqual(cfg["max_loops"], _ctl.FIX_LOOP_MAX)

    def test_unknown_stage_without_config_is_not_gate(self):
        self.assertIsNone(_ctl.resolve_gate_config({}, "requirements"))


class InvalidateForFixLoopTest(unittest.TestCase):
    def setUp(self):
        self.finalized = []
        self.superseded = []

        def fake_run(cmd, **kwargs):
            if "finalize" in cmd:
                self.finalized.append(cmd[cmd.index("finalize") + 1])
            if "supersede" in cmd:
                self.superseded.append(cmd[cmd.index("supersede") + 1])
            return _resp(0)

        self.patchers = [
            patch.object(
                _ctl, "load_tasks",
                return_value=[
                    _task("t-done", status="completed"),
                    _task("t-cleanup", status="cleanup_ready"),
                    _task("t-cleaned", status="cleaned"),
                    _task("t-agentdone", status="agent_done"),
                    _task("t-pending", status="pending"),
                    _task("t-committed", status="committed"),
                    _task("t-superseded", status="superseded"),
                    _task("t-other", status="cleaned", stage="wrapup2"),
                ],
            ),
            patch.object(
                _ctl, "get_task",
                side_effect=lambda tid: {"task_id": tid, "status": "cleaned"},
            ),
            patch.object(_ctl.subprocess, "run", side_effect=fake_run),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_downstream_invalidation_with_finalize_first(self):
        invalidated = _ctl.invalidate_for_fix_loop(
            "wf-1", "review", {"nodes": []}
        )

        # completed/cleanup_ready 先 finalize 规范化,再进入可作废集合
        self.assertEqual(sorted(self.finalized), ["t-cleanup", "t-done"])
        # cleaned/agent_done 直接 supersede;pending/committed/superseded/无关跳过
        self.assertEqual(
            sorted(self.superseded),
            ["t-agentdone", "t-cleaned", "t-cleanup", "t-done"],
        )
        self.assertEqual(
            sorted(invalidated),
            ["t-agentdone", "t-cleaned", "t-cleanup", "t-done"],
        )

    def test_idempotent_when_all_superseded(self):
        with patch.object(_ctl, "load_tasks", return_value=[]):
            invalidated = _ctl.invalidate_for_fix_loop(
                "wf-1", "review", {"nodes": []}
            )
        self.assertEqual(invalidated, [])


class HandleFixLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-fixloop-")
        _ctl.STAGE_STATE_FILE = str(Path(self.tmp.name) / "stage-state.json")
        self.fixloop_calls = []
        self.registry = [
            _task(
                "t1",
                status="completed",
                stage_verdict="blocked",
                stage_verdict_note="B1 阻断",
                branch=None,
            ),
            _task("t2", status="cleaned", stage="wrapup", branch="agent/x/pr"),
            _task(
                "impl1", status="committed", stage="implementation",
                branch="agent/x/feat-pr",
            ),
        ]

        patchers = [
            patch.object(_ctl, "load_tasks", return_value=self.registry),
            patch.object(_ctl, "get_task",
                         side_effect=lambda tid: {"task_id": tid, "status": "cleaned"}),
            patch.object(_ctl.subprocess, "run", return_value=_resp(0)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _drain(self):
        items = []
        while True:
            try:
                items.append(_ctl.coordinator_queue.get_nowait())
            except queue.Empty:
                return items

    def test_single_event_with_blockers_and_counter(self):
        _ctl.handle_fix_loop(
            "wf-1", "review",
            {"retry_node": "implementation", "max_loops": 3},
            {"nodes": []},
        )

        items = self._drain()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["kind"], "fix_loop")
        self.assertEqual(item["gate_stage"], "review")
        self.assertEqual(item["retry_node"], "implementation")
        self.assertEqual(item["loop_count"], 1)
        self.assertEqual(
            item["blockers"],
            [{"task_id": "t1", "note": "B1 阻断"}],
        )
        self.assertEqual(item["suggested_branch"], "agent/x/feat-pr")

        state = json.loads(Path(_ctl.STAGE_STATE_FILE).read_text())
        self.assertEqual(state["wf-1|fixloop|implementation"], 1)

    def test_no_invalidation_no_event(self):
        with patch.object(_ctl, "load_tasks", return_value=[]):
            _ctl.handle_fix_loop(
                "wf-1", "review",
                {"retry_node": "implementation", "max_loops": 3},
                {"nodes": []},
            )
        self.assertEqual(self._drain(), [])


class StageAdvanceGateTest(unittest.TestCase):
    def _workflow_cfg(self):
        return {
            "nodes": [
                {"id": "requirements", "depends_on": []},
                {"id": "implementation", "depends_on": ["requirements"]},
                {"id": "review", "depends_on": ["implementation"]},
                {"id": "wrapup", "depends_on": ["review"]},
            ]
        }

    def _patch_env(self, registry, fixloop_calls, advance_marks,
                   workflow_entry=None):
        tmp = tempfile.TemporaryDirectory(prefix="herdr-gatesweep-")
        self.addCleanup(tmp.cleanup)
        workflows_file = Path(tmp.name) / "workflows.json"
        workflows_file.write_text(json.dumps({
            "workflows": {
                "wf-1": workflow_entry or {"status": "in_progress"}
            }
        }))
        _ctl.WORKFLOWS_FILE = str(workflows_file)
        self.addCleanup(setattr, _ctl, "WORKFLOWS_FILE", _ctl.WORKFLOWS_FILE)

        patchers = [
            patch.object(_ctl, "workflow_config_for",
                         return_value=self._workflow_cfg()),
            patch.object(_ctl, "project_for_workflow",
                         return_value={"startup_ready": True}),
            patch.object(_ctl, "coordinator_pane_for_workflow",
                         return_value="wA:p1"),
            patch.object(_ctl, "load_stage_state", return_value={}),
            patch.object(_ctl, "save_stage_state", side_effect=lambda s: None),
            patch.object(_ctl, "load_tasks", return_value=registry),
            patch.object(
                _ctl, "mark_stage_advance_queued",
                side_effect=lambda wf, node: advance_marks.append(node) or True,
            ),
            patch.object(
                _ctl, "handle_fix_loop",
                side_effect=lambda wf, gate, cfg, wcfg:
                    fixloop_calls.append(gate),
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_blocked_gate_dependency_routes_to_fix_loop(self):
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("req1", status="cleaned", stage="requirements"),
                _task("impl1", status="cleaned", stage="implementation"),
                _task("rev1", status="cleaned", stage="review",
                      stage_verdict="blocked"),
            ],
            fixloop_calls,
            advance_marks,
        )

        _ctl.check_workflow_stage_advance("wf-1")

        self.assertEqual(fixloop_calls, ["review"])
        self.assertEqual(advance_marks, [])

    def test_passing_gate_advances_normally(self):
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("req1", status="cleaned", stage="requirements"),
                _task("impl1", status="cleaned", stage="implementation"),
                _task("rev1", status="cleaned", stage="review",
                      stage_verdict="pass"),
            ],
            fixloop_calls,
            advance_marks,
        )

        _ctl.check_workflow_stage_advance("wf-1")

        self.assertEqual(fixloop_calls, [])
        self.assertEqual(advance_marks, ["wrapup"])

    def test_completed_workflow_with_blocked_gate_never_closes(self):
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("req1", status="cleaned", stage="requirements"),
                _task("impl1", status="cleaned", stage="implementation"),
                _task("rev1", status="cleaned", stage="review"),
                _task("wrap1", status="cleaned", stage="wrapup",
                      stage_verdict="blocked"),
            ],
            fixloop_calls,
            advance_marks,
        )

        with patch.object(
            _ctl, "maybe_close_completed_workflow"
        ) as close_mock:
            _ctl.check_workflow_stage_advance("wf-1")

        self.assertEqual(fixloop_calls, ["wrapup"])
        close_mock.assert_not_called()
        self.assertEqual(advance_marks, [])

    def test_completed_workflow_with_pass_closes(self):
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("req1", status="cleaned", stage="requirements"),
                _task("impl1", status="cleaned", stage="implementation"),
                _task("rev1", status="cleaned", stage="review"),
                _task("wrap1", status="cleaned", stage="wrapup",
                      stage_verdict="pass"),
            ],
            fixloop_calls,
            advance_marks,
        )

        with patch.object(
            _ctl, "maybe_close_completed_workflow"
        ) as close_mock:
            _ctl.check_workflow_stage_advance("wf-1")

        close_mock.assert_called_once_with("wf-1")
        self.assertEqual(fixloop_calls, [])

    def test_completed_status_workflow_never_reflows(self):
        # abandoned/completed 的 workflow 残留 blocked verdict 也不得被
        # 周期 sweep 回流(否则已关闭工作流会被反复作废任务)。
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("rev1", status="cleaned", stage="review",
                      stage_verdict="blocked"),
            ],
            fixloop_calls,
            advance_marks,
            workflow_entry={"status": "completed", "outcome": "abandoned"},
        )

        _ctl.check_workflow_stage_advance("wf-1")

        self.assertEqual(fixloop_calls, [])
        self.assertEqual(advance_marks, [])

    def test_latched_reopen_sweep_is_silent(self):
        # reopen 闩存在时:全节点完成也不打 [WORKFLOW COMPLETE]、
        # 不 close、不回流。
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("req1", status="cleaned", stage="requirements"),
                _task("impl1", status="cleaned", stage="implementation"),
                _task("rev1", status="cleaned", stage="review"),
                _task("wrap1", status="cleaned", stage="wrapup"),
            ],
            fixloop_calls,
            advance_marks,
            workflow_entry={
                "status": "in_progress",
                "suppress_auto_close": True,
            },
        )

        with patch.object(
            _ctl, "maybe_close_completed_workflow"
        ) as close_mock:
            _ctl.check_workflow_stage_advance("wf-1")

        close_mock.assert_not_called()
        self.assertEqual(fixloop_calls, [])

    def test_reflow_does_not_advance_gate_while_fix_active(self):
        # 拒绝"给 gate 打 notified"的依据:fix task ACTIVE 期间,
        # implementation 节点未完成 → review 不 ready,无任何推进;
        # 无需依赖 notified 残留,也不存在与 fix_loop 竞争的推进。
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("req1", status="cleaned", stage="requirements"),
                _task("impl1", status="committed", stage="implementation"),
                _task("fix1", status="working", stage="implementation"),
                _task("rev1", status="superseded", stage="review",
                      stage_verdict="blocked"),
            ],
            fixloop_calls,
            advance_marks,
        )

        _ctl.check_workflow_stage_advance("wf-1")

        self.assertEqual(fixloop_calls, [])
        # implementation 节点未完成且依赖已满足 → 既有 ready-node 语义会
        # 重新提示派发(总指挥看到活动 fix task 即空转结束,属既有噪音,
        # 非本机制引入);关键是 review 不 ready、无第二次 fix_loop。
        self.assertEqual(advance_marks, ["implementation"])

    def test_reflow_resumes_gate_after_fix_completes(self):
        # fix 完成后:implementation 恢复完成,review 节点(全 superseded)
        # 重新 ready → 正常 stage_advance,重流由此发生。
        fixloop_calls, advance_marks = [], []
        self._patch_env(
            [
                _task("req1", status="cleaned", stage="requirements"),
                _task("impl1", status="committed", stage="implementation"),
                _task("fix1", status="completed", stage="implementation"),
                _task("rev1", status="superseded", stage="review",
                      stage_verdict="blocked"),
            ],
            fixloop_calls,
            advance_marks,
        )

        _ctl.check_workflow_stage_advance("wf-1")

        self.assertEqual(fixloop_calls, [])
        self.assertEqual(advance_marks, ["review"])


class CloseWorkflowGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-closegate-")
        root = Path(self.tmp.name)
        self.tasks_file = root / "tasks.json"
        self.workflows_file = root / "workflows.json"
        self.stage_state_file = root / "stage-state.json"
        self.clone_root = os.path.realpath(str(root / "clones"))

        _ht.TASKS_FILE = str(self.tasks_file)
        _ht.WORKFLOWS_FILE = str(self.workflows_file)
        _ht.STAGE_STATE_FILE = str(self.stage_state_file)
        _ht.EVIDENCE_ROOT = str(root / "evidence")
        _ht.CLONE_ROOT = self.clone_root

        self.herdr_calls = []

        def fake_herdr(*args):
            self.herdr_calls.append(args)
            head = tuple(args[:2])
            if head == ("pane", "read"):
                return _resp(0, "TRANSCRIPT\n")
            if head == ("pane", "get"):
                payload = {"result": {"pane": {
                    "pane_id": args[2],
                    "agent_session": {"agent": "codex", "value": "s"},
                }}}
                return _resp(0, json.dumps(payload))
            return _resp(0, "")

        self.herdr_patcher = patch.object(
            _ht, "_herdr", side_effect=fake_herdr
        )
        self.herdr_patcher.start()
        self.addCleanup(self.herdr_patcher.stop)

        clone = os.path.join(self.clone_root, "t1")
        os.makedirs(os.path.join(clone, ".git"))
        with open(os.path.join(clone, "f.txt"), "w") as f:
            f.write("x")

        self.tasks_file.write_text(json.dumps({"tasks": [
            {
                "task_id": "t1",
                "workflow_id": "wf-1",
                "status": "cleaned",
                "stage": "review",
                "node": "review",
                "agent": "codex",
                "pane_id": "wA:pX",
                "clone_path": clone,
                "integration_mode": "none",
                "branch": "agent/codex/t1",
                "stage_verdict": "blocked",
                "stage_verdict_note": "B1",
            }
        ]}))
        self.workflows_file.write_text(json.dumps(
            {"workflows": {"wf-1": {"status": "in_progress"}}}
        ))

    def _entry(self):
        return json.loads(self.workflows_file.read_text())["workflows"]["wf-1"]

    def test_close_refuses_blocked_verdict(self):
        with self.assertRaises(SystemExit) as ctx:
            _ht.close_workflow("wf-1")
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self._entry().get("status"), "in_progress")

    def test_close_abandon_records_outcome(self):
        _ht.close_workflow("wf-1", abandon=True)
        entry = self._entry()
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["outcome"], "abandoned")

    def test_close_without_verdict_records_delivered(self):
        tasks = json.loads(self.tasks_file.read_text())
        tasks["tasks"][0].pop("stage_verdict")
        tasks["tasks"][0].pop("stage_verdict_note")
        self.tasks_file.write_text(json.dumps(tasks))

        _ht.close_workflow("wf-1")
        entry = self._entry()
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["outcome"], "delivered")


class ConsoleGateTest(unittest.TestCase):
    def test_create_candidate_refuses_blocked_verdict(self):
        patchers = [
            patch.object(
                _con, "load_json",
                side_effect=lambda path, default: {
                    "workflows": {"wf-1": {"status": "in_progress"}}
                },
            ),
            patch.object(
                _con, "tasks_for_workflow",
                return_value=[
                    {"task_id": "rev1", "status": "cleaned",
                     "stage_verdict": "blocked"}
                ],
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        with patch.object(_con, "run") as run_mock:
            with self.assertRaises(RuntimeError) as ctx:
                _con.create_candidate("wf-1")

        run_mock.assert_not_called()
        self.assertIn("blocked", str(ctx.exception))
        self.assertIn("rev1", str(ctx.exception))

    def test_manual_advance_refuses_blocked_verdict(self):
        patchers = [
            patch.object(
                _con, "workflow_detail",
                return_value={"stages": [], "workflow": {}, "project": {}},
            ),
            patch.object(
                _con, "tasks_for_workflow",
                return_value=[
                    {"task_id": "rev1", "status": "cleaned",
                     "stage_verdict": "blocked"}
                ],
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        with patch.object(_con, "run") as run_mock:
            with self.assertRaises(RuntimeError) as ctx:
                _con.manual_advance("wf-1")

        run_mock.assert_not_called()
        self.assertIn("rev1", str(ctx.exception))


class InvalidateFixLoopSubsetTest(unittest.TestCase):
    """返工只重跑受影响子集:门禁节点内 verdict=pass 的任务保留。"""

    def setUp(self):
        self.finalized = []
        self.superseded = []

        def fake_run(cmd, **kwargs):
            if "finalize" in cmd:
                self.finalized.append(cmd[cmd.index("finalize") + 1])
            if "supersede" in cmd:
                self.superseded.append(cmd[cmd.index("supersede") + 1])
            return _resp(0)

        self.patchers = [
            patch.object(
                _ctl,
                "load_tasks",
                return_value=[
                    _task(
                        "test-pass", status="completed", stage="test",
                        stage_verdict="pass",
                    ),
                    _task(
                        "test-blocked", status="agent_done", stage="test",
                        stage_verdict="blocked",
                        stage_verdict_note="B1 回填缺失",
                    ),
                    _task(
                        "review-1", status="cleaned", stage="review",
                        stage_verdict="pass",
                    ),
                ],
            ),
            patch.object(
                _ctl, "get_task",
                side_effect=lambda tid: {"task_id": tid, "status": "cleaned"},
            ),
            patch.object(_ctl.subprocess, "run", side_effect=fake_run),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_gate_pass_preserved_only_blocked_and_downstream_invalidated(self):
        workflow_cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["test"]},
            ]
        }
        invalidated = _ctl.invalidate_for_fix_loop(
            "wf-1", "test", workflow_cfg
        )

        self.assertNotIn("test-pass", self.superseded)
        self.assertNotIn("test-pass", self.finalized)
        self.assertEqual(
            sorted(self.superseded),
            ["review-1", "test-blocked"],
        )
        self.assertEqual(
            sorted(invalidated),
            ["review-1", "test-blocked"],
        )

    def test_completed_git_pass_task_still_invalidated(self):
        # Git 集成的 pass 任务尚未落定,保留会滞留未提交 → 仍走 finalize+作废。
        with patch.object(
            _ctl,
            "load_tasks",
            return_value=[
                _task(
                    "test-pass-git", status="completed", stage="test",
                    stage_verdict="pass", integration_mode="git",
                ),
            ],
        ):
            invalidated = _ctl.invalidate_for_fix_loop(
                "wf-1", "test", {"nodes": []}
            )

        self.assertEqual(self.finalized, ["test-pass-git"])
        self.assertEqual(self.superseded, ["test-pass-git"])
        self.assertEqual(invalidated, ["test-pass-git"])

    def test_intermediate_nodes_invalidated_when_gate_retries_upstream_node(self):
        # review 门禁回流到更上游 implementation 时,
        # 中间节点(test)与门禁节点(review)任务必须全部作废,implementation 本身不作废
        workflow_cfg = {
            "nodes": [
                {"id": "implementation", "depends_on": []},
                {"id": "test", "depends_on": ["implementation"]},
                {"id": "review", "depends_on": ["test"]},
            ]
        }
        with patch.object(
            _ctl,
            "load_tasks",
            return_value=[
                _task("impl-1", status="cleaned", stage="implementation", stage_verdict="pass"),
                _task("test-1", status="cleaned", stage="test", stage_verdict="pass"),
                _task("review-1", status="cleaned", stage="review", stage_verdict="blocked"),
            ],
        ):
            invalidated = _ctl.invalidate_for_fix_loop(
                "wf-1", "review", workflow_cfg, retry_node="implementation"
            )

        self.assertNotIn("impl-1", self.superseded)
        self.assertIn("test-1", self.superseded)
        self.assertIn("review-1", self.superseded)
        self.assertEqual(sorted(invalidated), ["review-1", "test-1"])


class FinalizeAlreadyCommittedTaskTest(unittest.TestCase):
    def test_finalize_skips_commit_if_already_committed(self):
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            return _resp(0)

        task = _task(
            "task-committed",
            status="committed",
            integration_mode="git",
        )

        def mock_get_task(tid):
            if any("integrate" in cmd for cmd in commands):
                return _task(tid, status="integrated", integration_mode="git")
            return task

        with patch.object(_ctl, "get_task", side_effect=mock_get_task), \
             patch.object(_ctl, "set_task_status", return_value=True), \
             patch.object(_ctl, "enqueue_stage_advance"), \
             patch.object(_ctl.subprocess, "run", side_effect=fake_run):
            _ctl.finalize_completed_task("task-committed")

        subcmds = [cmd[1] for cmd in commands]
        self.assertNotIn("commit", subcmds)
        self.assertIn("integrate", subcmds)
        self.assertIn("cleanup", subcmds)


class AutoCloseGitFinalizeDeferralTest(unittest.TestCase):
    """自动 close 不得与 git 终化(commit/integrate)抢跑。

    回归背景:2026-09-17 wf-nexusarchive-0917-01 wrapup 在 `herdr-task
    commit` 子进程在途时被 close 抢先推进到 cleaned,commit 随后撞
    'cleaned -> committed' 非法转移,交付分支落不进集成链路。
    """

    def setUp(self):
        _ctl._workflow_close_inflight.discard("wf-1")
        _ctl._close_deferred_logged.discard("wf-1")

    def _invoke(self, tasks):
        with patch.object(_ctl, "load_tasks", return_value=tasks), \
             patch.object(
                 _ctl, "_workflow_entry",
                 return_value={"status": "in_progress"},
             ), \
             patch.object(
                 _ctl.liveness, "workflow_is_foreign",
                 return_value=False,
             ), \
             patch.object(
                 _ctl.subprocess, "run",
                 side_effect=lambda *a, **k: _resp(0),
             ) as run_mock:
            _ctl.maybe_close_completed_workflow("wf-1")
            time.sleep(0.2)
        return run_mock

    def test_close_deferred_while_completed_git_task_pending(self):
        run_mock = self._invoke([
            _task("wrap", status="completed", stage="wrapup",
                  integration_mode="git"),
        ])
        run_mock.assert_not_called()
        self.assertNotIn("wf-1", _ctl._workflow_close_inflight)
        self.assertIn("wf-1", _ctl._close_deferred_logged)

    def test_close_deferred_while_committed_git_task_pending(self):
        run_mock = self._invoke([
            _task("impl", status="committed", stage="implementation",
                  integration_mode="git"),
        ])
        run_mock.assert_not_called()

    def test_close_proceeds_when_git_finalize_settled(self):
        run_mock = self._invoke([
            _task("impl", status="cleaned", stage="implementation",
                  integration_mode="git"),
        ])
        self.assertTrue(run_mock.called)

    def test_close_proceeds_for_completed_docs_task(self):
        # 非 git 的 completed 任务仍按既有路径物理收尾,不被闸门误伤。
        run_mock = self._invoke([
            _task("docs", status="completed", stage="wrapup",
                  integration_mode="none"),
        ])
        self.assertTrue(run_mock.called)


if __name__ == "__main__":
    unittest.main()
