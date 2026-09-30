"""
Regression tests for the workflow deadlock fix.

Covers:
  1. TRANSITIONS: superseded is reachable from failed/cleaned/in-progress.
  2. is_node_complete: superseded tasks excluded; node unblocks correctly.
  3. reconcile_stage_advance_states: notified lock revoked on predecessor regress.
  4. supersede_task: marks task superseded with correct metadata.
  5. stage_reset / force_advance: clear stage-state.json keys correctly.
"""

import importlib
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _import_herdr_task():
    task_bin = HERDR_ROOT / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader(
        "herdr_task_bin",
        importlib.machinery.SourceFileLoader("herdr_task_bin", str(task_bin)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _import_herdr_task()
TRANSITIONS = _ht.TRANSITIONS


def _stub_herdr_modules():
    for name in ("herdr.projects", "herdr.workflow", "herdr_projects", "herdr_workflow"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.project_for_workflow = lambda wf: {}
            stub.workflow_config_for = lambda wf: None
            stub.find_node = lambda cfg, n: None
            stub.get_ready_nodes = lambda cfg, done: []
            stub.is_workflow_completed = lambda cfg, done: False
            stub.normalize_workflow = lambda cfg: cfg
            sys.modules[name] = stub


def _load_controller(unique_name):
    _stub_herdr_modules()
    ctrl_path = HERDR_ROOT / "services" / "herdr-controller.py"
    spec = importlib.util.spec_from_loader(
        unique_name,
        importlib.machinery.SourceFileLoader(unique_name, str(ctrl_path)),
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        pass
    return mod


# ---------------------------------------------------------------------------
# 1. TRANSITIONS
# ---------------------------------------------------------------------------

class TestTransitions(unittest.TestCase):

    def test_superseded_from_failed(self):
        self.assertIn("superseded", TRANSITIONS["failed"])

    def test_superseded_from_cleaned(self):
        self.assertIn("superseded", TRANSITIONS["cleaned"])

    def test_superseded_from_in_progress(self):
        for state in ("dispatched", "working", "blocked", "agent_done", "rework"):
            with self.subTest(state=state):
                self.assertIn("superseded", TRANSITIONS[state])

    def test_superseded_is_terminal(self):
        self.assertEqual(TRANSITIONS["superseded"], set())

    def test_pending_cannot_supersede(self):
        self.assertNotIn("superseded", TRANSITIONS.get("pending", set()))

    def test_completed_chain_unaffected(self):
        for state in ("completed", "committed", "integrated", "cleanup_ready"):
            with self.subTest(state=state):
                self.assertNotIn("superseded", TRANSITIONS.get(state, set()))


# ---------------------------------------------------------------------------
# 2. is_node_complete
# ---------------------------------------------------------------------------

class TestIsNodeComplete(unittest.TestCase):

    def setUp(self):
        self.ctrl = _load_controller("ctrl_is_node_complete")

    def _set_tasks(self, tasks):
        self.ctrl.load_tasks = lambda: tasks

    def test_failed_blocks_node(self):
        self._set_tasks([
            {"task_id": "t1", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "failed"},
        ])
        self.assertFalse(self.ctrl.is_node_complete("wf1", "fix"))

    def test_superseded_plus_cleaned_replacement_completes_node(self):
        self._set_tasks([
            {"task_id": "t1", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "superseded", "superseded_by": "t2"},
            {"task_id": "t2", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "cleaned"},
        ])
        self.assertTrue(self.ctrl.is_node_complete("wf1", "fix"))

    def test_all_superseded_no_replacement_is_incomplete(self):
        self._set_tasks([
            {"task_id": "t1", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "superseded"},
        ])
        self.assertFalse(self.ctrl.is_node_complete("wf1", "fix"))

    def test_cleaned_plus_superseded_completes_node(self):
        self._set_tasks([
            {"task_id": "t1", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "cleaned"},
            {"task_id": "t2", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "superseded"},
        ])
        self.assertTrue(self.ctrl.is_node_complete("wf1", "fix"))


# ---------------------------------------------------------------------------
# 3. reconcile_stage_advance_states
# ---------------------------------------------------------------------------

class TestReconcile(unittest.TestCase):

    def setUp(self):
        self.ctrl = _load_controller("ctrl_reconcile")

    def test_notified_revoked_when_predecessor_regresses(self):
        wf = "wf-reconcile-1"
        workflow_cfg = {
            "nodes": [
                {"id": "fix", "depends_on": []},
                {"id": "test", "depends_on": ["fix"]},
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({f"{wf}:test": "notified"}, f)
            tmp = f.name
        try:
            self.ctrl.STAGE_STATE_FILE = tmp
            self.ctrl.load_tasks = lambda: [
                {"task_id": "t1", "workflow_id": wf, "node": "fix",
                 "stage": "fix", "status": "failed"},
            ]
            self.ctrl.reconcile_stage_advance_states(wf, workflow_cfg)
            with open(tmp) as f:
                state = json.load(f)
            self.assertNotIn(f"{wf}:test", state)
        finally:
            os.unlink(tmp)

    def test_notified_preserved_when_predecessor_complete(self):
        wf = "wf-reconcile-2"
        workflow_cfg = {
            "nodes": [
                {"id": "fix", "depends_on": []},
                {"id": "test", "depends_on": ["fix"]},
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({f"{wf}:test": "notified"}, f)
            tmp = f.name
        try:
            self.ctrl.STAGE_STATE_FILE = tmp
            self.ctrl.load_tasks = lambda: [
                {"task_id": "t1", "workflow_id": wf, "node": "fix",
                 "stage": "fix", "status": "cleaned"},
            ]
            self.ctrl.reconcile_stage_advance_states(wf, workflow_cfg)
            with open(tmp) as f:
                state = json.load(f)
            self.assertEqual(state.get(f"{wf}:test"), "notified")
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# 4. supersede_task
# ---------------------------------------------------------------------------

class TestSupersedeTask(unittest.TestCase):

    def _store(self, tasks):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"tasks": tasks}, f)
            return f.name

    def test_supersede_failed_task(self):
        path = self._store([{"task_id": "old-1", "status": "failed"}])
        try:
            _ht.TASKS_FILE = path
            _ht.supersede_task("old-1", new_task_id="new-1", reason="retry")
            with open(path) as f:
                data = json.load(f)
            t = next(x for x in data["tasks"] if x["task_id"] == "old-1")
            self.assertEqual(t["status"], "superseded")
            self.assertEqual(t["superseded_by"], "new-1")
            self.assertEqual(t["supersede_reason"], "retry")
        finally:
            os.unlink(path)

    def test_supersede_cleaned_task(self):
        path = self._store([{"task_id": "old-2", "status": "cleaned"}])
        try:
            _ht.TASKS_FILE = path
            _ht.supersede_task("old-2")
            with open(path) as f:
                data = json.load(f)
            t = next(x for x in data["tasks"] if x["task_id"] == "old-2")
            self.assertEqual(t["status"], "superseded")
        finally:
            os.unlink(path)

    def test_supersede_pending_rejected(self):
        path = self._store([{"task_id": "p-1", "status": "pending"}])
        try:
            _ht.TASKS_FILE = path
            with self.assertRaises(SystemExit) as cm:
                _ht.supersede_task("p-1")
            self.assertEqual(cm.exception.code, 2)
        finally:
            os.unlink(path)

    # -- 补挂：auto-recover 两阶段留下的断链 ---------------------------------
    #
    # auto-recover (`services/herdr-controller.py`) 先把基础设施失败的任务
    # 标成 superseded（此刻还不知道替代者是谁），direct_dispatch 之后才按谱系
    # 补派 -rN。旧任务的 superseded_by 因此永远为空，而 required_task_ids 的
    # 链式解析（scheduler.node_is_complete）断在第一跳 → 节点永久判不出完成。
    #
    # 修法不是放宽状态机（superseded 必须保持终态，见
    # test_superseded_is_terminal），而是让 supersede 能在「已经是 superseded」
    # 时幂等地补挂替代者：只写 superseded_by，不改 status。

    def test_link_replacement_onto_already_superseded_task(self):
        path = self._store([
            {"task_id": "impl-t7", "status": "superseded",
             "workflow_id": "wf-1", "supersede_reason": "auto-recover"},
        ])
        try:
            _ht.TASKS_FILE = path
            _ht.supersede_task("impl-t7", new_task_id="impl-t7-r2")
            with open(path) as f:
                data = json.load(f)
            t = next(x for x in data["tasks"] if x["task_id"] == "impl-t7")
            self.assertEqual(t["superseded_by"], "impl-t7-r2",
                             "补派替代者后必须回填 superseded_by")
            self.assertEqual(t["status"], "superseded",
                             "补挂不得改动已达终态的 status")
            self.assertEqual(t["supersede_reason"], "auto-recover",
                             "补挂不得覆盖首次作废的原因")
        finally:
            os.unlink(path)

    def test_link_replacement_does_not_overwrite_existing_pointer(self):
        """已有替代者时必须拒绝，否则谱系会被静默改写。"""
        path = self._store([
            {"task_id": "impl-x", "status": "superseded",
             "superseded_by": "impl-x-r2"},
        ])
        try:
            _ht.TASKS_FILE = path
            with self.assertRaises(SystemExit) as cm:
                _ht.supersede_task("impl-x", new_task_id="impl-x-r9")
            self.assertEqual(cm.exception.code, 2)
            with open(path) as f:
                data = json.load(f)
            t = next(x for x in data["tasks"] if x["task_id"] == "impl-x")
            self.assertEqual(t["superseded_by"], "impl-x-r2")
        finally:
            os.unlink(path)

    def test_link_replacement_rejects_cross_workflow(self):
        path = self._store([
            {"task_id": "impl-y", "status": "superseded", "workflow_id": "wf-1"},
            {"task_id": "impl-y-r2", "status": "pending", "workflow_id": "wf-2"},
        ])
        try:
            _ht.TASKS_FILE = path
            with self.assertRaises(SystemExit) as cm:
                _ht.supersede_task("impl-y", new_task_id="impl-y-r2")
            self.assertEqual(cm.exception.code, 2)
        finally:
            os.unlink(path)

    def test_link_replacement_allows_distinct_run_id(self):
        """补派按设计就是新执行：新旧 run_id 不同，不是身份串号。

        controller 构造 launch argv 时明确保留
        ``--execution-id <workflow_id>`` 并注明 "sibling launches keep
        distinct run_ids but share one execution_id"。实测事故
        (wf-project-0929-01)：impl-t7 run_955df1bd… 被补派成
        impl-t7-r2 run_c5a26395…，run 天然不同 —— 若补挂沿用 run 相等
        校验，存量断链将永远补不上。

        跨 workflow 仍然必须拒绝（见上一个用例）：run 可不同，
        归属的 workflow 不能不同。
        """
        path = self._store([
            {"task_id": "impl-t7", "status": "superseded",
             "workflow_id": "wf-1", "run_id": "run_aaa"},
            {"task_id": "impl-t7-r2", "status": "cleaned",
             "workflow_id": "wf-1", "run_id": "run_bbb"},
        ])
        try:
            _ht.TASKS_FILE = path
            _ht.supersede_task("impl-t7", new_task_id="impl-t7-r2")
            with open(path) as f:
                data = json.load(f)
            t = next(x for x in data["tasks"] if x["task_id"] == "impl-t7")
            self.assertEqual(t["superseded_by"], "impl-t7-r2")
            self.assertEqual(t["run_id"], "run_aaa", "补挂不得改写旧任务 run 归属")
        finally:
            os.unlink(path)

    def test_superseded_task_without_replacement_still_rejected(self):
        """没有替代者时维持原语义：superseded 不能再「转」一次。"""
        path = self._store([{"task_id": "impl-z", "status": "superseded"}])
        try:
            _ht.TASKS_FILE = path
            with self.assertRaises(SystemExit) as cm:
                _ht.supersede_task("impl-z")
            self.assertEqual(cm.exception.code, 2)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# 5. 补派链路：dispatch spec 的 redispatch_of 必须落到 --supersedes
# ---------------------------------------------------------------------------

class TestRedispatchLinksSupersedes(unittest.TestCase):
    """direct_dispatch 已经算出 redispatch_of，controller 却没传给 launch。

    实测事故（wf-project-0929-01）：impl-t7-integration-gates 被 auto-recover
    作废后补派成 -r2，但 launch 命令缺 --supersedes，required_task_ids 判定
    永久卡死，日志刷 [STAGE ADVANCE WAIT] coordinator=working。
    """

    def test_controller_passes_supersedes_for_redispatch_specs(self):
        ctrl = _load_controller("herdr_controller_supersedes_probe")
        src = HERDR_ROOT / "services" / "herdr-controller.py"
        text = src.read_text(encoding="utf-8")
        # launch argv 的构造块：redispatch_of 必须被翻译成 --supersedes
        self.assertIn("spec.get(\"redispatch_of\")", text,
                      "补派 spec 的 redispatch_of 未被消费")
        self.assertIn('"--supersedes"', text,
                      "direct dispatch 的 launch argv 缺 --supersedes")
        self.assertTrue(hasattr(ctrl, "TASK_MANAGER"))


class TestNodeCompleteAfterRedispatchLink(unittest.TestCase):
    """端到端：补挂 superseded_by 后，required_task_ids 判定必须从卡死变通过。"""

    def _task(self, tid, status, **kw):
        t = {"task_id": tid, "status": status, "workflow_id": "wf-1",
             "node": "implementation", "integration_mode": "git"}
        t.update(kw)
        return t

    def test_required_task_ids_resolves_through_redispatch_lineage(self):
        from herdr.scheduler import node_is_complete

        required = ["impl-a", "impl-t7"]
        broken = [
            self._task("impl-a", "cleaned"),
            self._task("impl-t7", "superseded", superseded_by=None),
        ]
        self.assertFalse(
            node_is_complete(broken, required),
            "断链时应当判未完成（fail-closed），这是当前的卡死态",
        )

        linked = [
            self._task("impl-a", "cleaned"),
            self._task("impl-t7", "superseded", superseded_by="impl-t7-r2"),
            self._task("impl-t7-r2", "cleaned"),
        ]
        self.assertTrue(
            node_is_complete(linked, required),
            "回填 superseded_by 后应能沿谱系走到 -r2 并判完成",
        )


# ---------------------------------------------------------------------------
# 5. stage_reset / force_advance
# ---------------------------------------------------------------------------

class TestStageReset(unittest.TestCase):

    def _store(self, data):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(data, f)
            return f.name

    def test_stage_reset_clears_all_for_workflow(self):
        path = self._store({
            "wf-a:test": "notified",
            "wf-a:review": "queued",
            "wf-b:test": "notified",
        })
        try:
            with patch.object(_ht, "STAGE_STATE_FILE", path):
                _ht.stage_reset("wf-a")
            with open(path) as f:
                state = json.load(f)
            self.assertNotIn("wf-a:test", state)
            self.assertNotIn("wf-a:review", state)
            self.assertIn("wf-b:test", state)
        finally:
            os.unlink(path)

    def test_stage_reset_single_stage(self):
        path = self._store({
            "wf-a:test": "notified",
            "wf-a:review": "queued",
        })
        try:
            with patch.object(_ht, "STAGE_STATE_FILE", path):
                _ht.stage_reset("wf-a", "test")
            with open(path) as f:
                state = json.load(f)
            self.assertNotIn("wf-a:test", state)
            self.assertIn("wf-a:review", state)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# 6. ops-center cards vs controller parity
# ---------------------------------------------------------------------------

class TestOpsCardParity(unittest.TestCase):
    """Ops-center node cards must agree with is_node_complete on superseded
    tasks — regression for the 2026-09-12 stats drift (cards counted
    superseded tasks in the denominator while is_node_complete excluded them).
    """

    def test_card_counts_match_is_node_complete(self):
        ctrl = _load_controller("ctrl_ops_parity")
        tasks = [
            {"task_id": "t1", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "superseded", "superseded_by": "t2"},
            {"task_id": "t2", "workflow_id": "wf1", "node": "fix", "stage": "fix",
             "status": "cleaned"},
        ]
        ctrl.load_tasks = lambda: tasks

        self.assertTrue(ctrl.is_node_complete("wf1", "fix"))
        counts = _ht._node_task_status_counts(tasks)
        self.assertEqual(counts["total"], counts["completed"])
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["superseded"], 1)


if __name__ == "__main__":
    unittest.main()
