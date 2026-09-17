"""Tests for herdr-task finalize / close-workflow teardown lifecycle."""

import importlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))


def _import_herdr_task():
    task_bin = HERDR_ROOT / "bin" / "herdr-task"
    spec = importlib.util.spec_from_loader(
        "herdr_task_finalize_test",
        importlib.machinery.SourceFileLoader(
            "herdr_task_finalize_test",
            str(task_bin),
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ht = _import_herdr_task()


def _resp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FinalizeTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-finalize-")
        root = Path(self.tmp.name)
        self.tasks_file = str(root / "tasks.json")
        self.workflows_file = str(root / "workflows.json")
        self.stage_state_file = str(root / "stage-state.json")
        self.evidence_root = str(root / "evidence")
        self.clone_root = os.path.realpath(str(root / "clones"))

        _ht.TASKS_FILE = self.tasks_file
        _ht.WORKFLOWS_FILE = self.workflows_file
        _ht.STAGE_STATE_FILE = self.stage_state_file
        _ht.EVIDENCE_ROOT = self.evidence_root
        _ht.CLONE_ROOT = self.clone_root

        self.herdr_calls = []
        self.pane_exists = True
        self.pane_session = {"agent": "opencode", "value": "ses_test"}
        self.live_panes = [
            {"pane_id": "wA:pT", "tab_id": "wA:t8"},
            {"pane_id": "wA:pX", "tab_id": "wA:t8"},
            {"pane_id": "wA:pV", "tab_id": "wA:t9"},
        ]

    def tearDown(self):
        try:
            store = _ht._get_store()
            for t in store.list_tasks():
                store.delete_task(t["task_id"])
            for w in store.list_workflows():
                store.delete_workflow(w["workflow_id"])
        except Exception:
            pass
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _write_tasks(self, tasks):
        with open(self.tasks_file, "w", encoding="utf-8") as f:
            json.dump({"tasks": tasks}, f)
        store = _ht._get_store()
        for t in store.list_tasks():
            store.delete_task(t["task_id"])
        for t in tasks:
            store.save_task(t)

    def _read_tasks(self):
        store = _ht._get_store()
        tasks = store.list_tasks()
        if tasks:
            return tasks
        with open(self.tasks_file, "r", encoding="utf-8") as f:
            return json.load(f)["tasks"]

    def _write_workflows(self, entries):
        for wid, entry in entries.items():
            entry.setdefault("workflow_id", wid)
            entry.setdefault("status", "running")
        with open(self.workflows_file, "w", encoding="utf-8") as f:
            json.dump({"workflows": entries}, f)
        store = _ht._get_store()
        for wid, entry in entries.items():
            store.save_workflow(entry)

    def _read_workflows(self):
        store = _ht._get_store()
        wfs = {w["workflow_id"]: w for w in store.list_workflows()}
        if wfs:
            return wfs
        with open(self.workflows_file, "r", encoding="utf-8") as f:
            return json.load(f)["workflows"]

    def _mk_clone(self, task_id):
        clone = os.path.join(self.clone_root, task_id)
        os.makedirs(os.path.join(clone, ".git"))
        with open(os.path.join(clone, "file.txt"), "w") as f:
            f.write("x")
        return clone

    def _mk_task(self, task_id="t-1", status="cleaned", stage="implementation",
                 integration_mode="git", pane_id="wA:pX", clone=True,
                 integration_ref=None, workflow_id="wf-test"):
        task = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "status": status,
            "stage": stage,
            "agent": "opencode",
            "pane_id": pane_id,
            "clone_path": self._mk_clone(task_id) if clone else None,
            "integration_mode": integration_mode,
            "branch": f"agent/opencode/test-{task_id}",
        }
        if integration_ref:
            task["integration_ref"] = integration_ref
            task["integration_branch"] = f"herdr/integration-{task_id}"
        return task

    def _fake_herdr(self, *args):
        self.herdr_calls.append(args)
        head = tuple(args[:2])
        if head == ("pane", "read"):
            if not self.pane_exists:
                return _resp(1, "", "no pane")
            return _resp(0, "TRANSCRIPT LINE 1\nTRANSCRIPT LINE 2\n")
        if head == ("pane", "get"):
            if not self.pane_exists:
                return _resp(1, "", "no pane")
            payload = {"result": {"pane": {
                "pane_id": args[2], "agent_session": self.pane_session}}}
            return _resp(0, json.dumps(payload))
        if head == ("pane", "list"):
            payload = {"result": {"panes": self.live_panes}}
            return _resp(0, json.dumps(payload))
        if head in {("pane", "close"), ("tab", "close")}:
            if not self.pane_exists and head == ("pane", "close"):
                return _resp(1, "", "no pane")
            return _resp(0, "")
        if head == ("agent", "get"):
            if not self.pane_exists:
                return _resp(1, "", "no pane")
            return _resp(0, json.dumps({"result": {"agent": {
                "agent": "opencode", "agent_status": "idle"}}}))
        return _resp(0, "")

    def _patch_herdr(self):
        return patch.object(
            _ht, "_herdr", side_effect=lambda *a: self._fake_herdr(*a)
        )

    def _assert_no_herdr_calls(self):
        self.assertEqual(self.herdr_calls, [])


class TestFinalizeTask(FinalizeTestBase):
    def test_refuses_active_task(self):
        self._write_tasks([self._mk_task(status="working")])
        with self._patch_herdr():
            with self.assertRaises(SystemExit) as ctx:
                _ht.finalize_task("t-1")
        self.assertEqual(ctx.exception.code, 2)
        self._assert_no_herdr_calls()
        self.assertEqual(self._read_tasks()[0]["status"], "working")

    def test_failed_requires_force(self):
        self._write_tasks([self._mk_task(status="failed")])
        with self._patch_herdr():
            with self.assertRaises(SystemExit) as ctx:
                _ht.finalize_task("t-1")
        self.assertEqual(ctx.exception.code, 2)
        self._assert_no_herdr_calls()

    def test_force_allows_failed(self):
        self._write_tasks([self._mk_task(status="failed")])
        with self._patch_herdr():
            report = _ht.finalize_task("t-1", force=True)
        self.assertTrue(report["pane_closed"])
        self.assertTrue(os.path.exists(report["evidence"]))
        # failed 状态本身不推进,资源已清
        self.assertEqual(self._read_tasks()[0]["status"], "failed")
        self.assertFalse(self._read_tasks()[0]["pane_retained"])

    def test_dumps_transcript_and_closes_pane(self):
        self._write_tasks([self._mk_task(status="cleaned")])
        with self._patch_herdr():
            report = _ht.finalize_task("t-1")
        evidence = report["evidence"]
        self.assertTrue(evidence and os.path.exists(evidence))
        with open(evidence, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("TRANSCRIPT LINE 1", content)
        meta_path = os.path.join(os.path.dirname(evidence), "meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["agent_session"], self.pane_session)
        self.assertIn(("pane", "close", "wA:pX"), self.herdr_calls)
        self.assertTrue(report["pane_closed"])

    def test_integrated_clone_deleted_docs_clone_retained(self):
        git_task = self._mk_task("t-git", status="cleaned",
                                 integration_ref="refs/herdr/tasks/t-git")
        docs_task = self._mk_task("t-docs", status="cleaned",
                                  integration_mode="none")
        self._write_tasks([git_task, docs_task])
        with self._patch_herdr():
            r1 = _ht.finalize_task("t-git")
            r2 = _ht.finalize_task("t-docs")
        self.assertFalse(os.path.exists(r1["clone_path"]))
        self.assertTrue(r2["clone_deleted"] is False)
        self.assertTrue(r2["clone_retained_reason"])
        self.assertTrue(os.path.exists(r2["clone_path"]))

    def test_purge_clones_flag_removes_docs_clone(self):
        self._write_tasks([self._mk_task("t-docs", status="cleaned",
                                         integration_mode="none")])
        with self._patch_herdr():
            report = _ht.finalize_task("t-docs", purge_clones=True)
        self.assertTrue(report["clone_deleted"])
        self.assertFalse(os.path.exists(report["clone_path"]))

    def test_superseded_deletes_clone_and_keeps_status(self):
        self._write_tasks([self._mk_task("t-sup", status="superseded",
                                         integration_mode="none")])
        with self._patch_herdr():
            report = _ht.finalize_task("t-sup")
        self.assertTrue(report["clone_deleted"])
        self.assertEqual(report["status"], "superseded")
        self.assertEqual(self._read_tasks()[0]["status"], "superseded")

    def test_completed_advances_to_cleaned(self):
        self._write_tasks([self._mk_task("t-c", status="completed",
                                         integration_mode="none")])
        with self._patch_herdr():
            report = _ht.finalize_task("t-c")
        self.assertEqual(report["status"], "cleaned")
        self.assertEqual(self._read_tasks()[0]["status"], "cleaned")

    def test_committed_not_integrated_keeps_clone(self):
        self._write_tasks([self._mk_task("t-cm", status="committed",
                                         integration_mode="git")])
        with self._patch_herdr():
            report = _ht.finalize_task("t-cm")
        self.assertEqual(report["status"], "committed")
        self.assertTrue(os.path.exists(report["clone_path"]))
        self.assertIn("integrate", report["clone_retained_reason"])

    def test_idempotent_when_pane_already_gone(self):
        self._write_tasks([self._mk_task("t-idem", status="cleaned",
                                         integration_ref="refs/x")])
        self.pane_exists = False
        with self._patch_herdr():
            first = _ht.finalize_task("t-idem")
            second = _ht.finalize_task("t-idem")
        self.assertFalse(first["pane_closed"])
        self.assertFalse(second["pane_closed"])
        self.assertFalse(os.path.exists(first["clone_path"]))

    def test_refuses_clone_outside_clone_root(self):
        outside = os.path.join(self.tmp.name, "outside-repo")
        os.makedirs(outside)
        task = self._mk_task("t-out", status="cleaned",
                             integration_ref="refs/x")
        task["clone_path"] = outside
        self._write_tasks([task])
        with self._patch_herdr():
            report = _ht.finalize_task("t-out")
        self.assertFalse(report["clone_deleted"])
        self.assertTrue(os.path.exists(outside))

    def test_missing_task_exits(self):
        self._write_tasks([])
        with self.assertRaises(SystemExit) as ctx:
            _ht.finalize_task("nope")
        self.assertEqual(ctx.exception.code, 1)


class TestPurgeGate(FinalizeTestBase):
    def test_purge_accepts_superseded(self):
        self._write_tasks([self._mk_task("t-sup", status="superseded",
                                         integration_mode="none")])
        with self._patch_herdr():
            _ht.purge_task("t-sup")
        tasks = self._read_tasks()
        self.assertEqual(tasks[0]["resource_retention"], "purged")

    def test_purge_still_refuses_active(self):
        self._write_tasks([self._mk_task("t-w", status="working")])
        with self._patch_herdr():
            with self.assertRaises(SystemExit) as ctx:
                _ht.purge_task("t-w")
        self.assertEqual(ctx.exception.code, 2)


class TestCloseWorkflow(FinalizeTestBase):
    def _write_workflow_config(self):
        cfg = {
            "workspace_id": "wA",
            "coordinator": {"tab_id": "wA:t1", "pane_id": "wA:p1"},
            "stages": [
                {"key": "requirements", "tab_id": "wA:t8",
                 "anchor_pane_id": "wA:pT"},
                {"key": "plan", "tab_id": "wA:t9",
                 "anchor_pane_id": "wA:pV"},
            ],
        }
        cfg_file = os.path.join(self.tmp.name, "workflow.json")
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return cfg_file

    def setUp(self):
        super().setUp()
        self.cfg_file = self._write_workflow_config()
        self._write_workflows({
            "wf-test": {"workflow_id": "wf-test", "project_id": "p-1"},
        })
        with open(self.stage_state_file, "w", encoding="utf-8") as f:
            json.dump({"wf-test:requirements": "notified",
                       "other:requirements": "notified"}, f)
        patcher = patch.object(
            _ht, "project_for_workflow",
            return_value={"workflow_file": self.cfg_file},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_blocks_on_active_tasks(self):
        tasks = [
            self._mk_task("t-ok", status="cleaned",
                          integration_ref="refs/x"),
            self._mk_task("t-active", status="working"),
        ]
        self._write_tasks(tasks)
        with self._patch_herdr():
            with self.assertRaises(SystemExit) as ctx:
                _ht.close_workflow("wf-test")
        self.assertEqual(ctx.exception.code, 2)
        self._assert_no_herdr_calls()
        self.assertEqual(
            self._read_workflows()["wf-test"].get("status"), "running")

    def test_blocks_on_unsettled_git_finalize(self):
        # completed/committed 的 git 任务尚未走完 commit/integrate:
        # close 抢先清领会撞 'cleaned -> committed' 非法转移。
        tasks = [
            self._mk_task("t-done", status="completed",
                          integration_mode="git"),
            self._mk_task("t-cm", status="committed",
                          integration_mode="git"),
        ]
        self._write_tasks(tasks)
        with self._patch_herdr():
            with self.assertRaises(SystemExit) as ctx:
                _ht.close_workflow("wf-test")
        self.assertEqual(ctx.exception.code, 2)
        self._assert_no_herdr_calls()
        self.assertEqual(
            self._read_workflows()["wf-test"].get("status"), "running")

    def test_unsettled_git_check_ignores_non_git_and_settled(self):
        # 不误伤:completed+none 仍按既有路径推进 cleaned;
        # cleaned+git 不受闸门影响。
        tasks = [
            self._mk_task("t-c", status="completed",
                          integration_mode="none"),
            self._mk_task("t-git-ok", status="cleaned",
                          integration_mode="git",
                          integration_ref="refs/herdr/tasks/t-git-ok"),
        ]
        self._write_tasks(tasks)
        with self._patch_herdr():
            report = _ht.close_workflow("wf-test")
        by_id = {r["task_id"]: r for r in report["tasks"]}
        self.assertEqual(by_id["t-c"]["status"], "cleaned")
        self.assertEqual(by_id["t-git-ok"]["status"], "cleaned")

    def test_full_close_retains_failed_and_never_touches_coordinator(self):
        tasks = [
            self._mk_task("t-git", status="cleaned",
                          integration_ref="refs/herdr/tasks/t-git"),
            self._mk_task("t-docs", status="cleaned",
                          integration_mode="none"),
            self._mk_task("t-sup", status="superseded",
                          integration_mode="none"),
            self._mk_task("t-c", status="completed",
                          integration_mode="none"),
            self._mk_task("t-fail", status="failed",
                          integration_mode="none"),
        ]
        self._write_tasks(tasks)
        with self._patch_herdr():
            report = _ht.close_workflow("wf-test")

        by_id = {r["task_id"]: r for r in report["tasks"]}
        self.assertEqual(by_id["t-fail"]["action"], "retained-failed")
        self.assertEqual(by_id["t-git"]["action"], "finalized")
        self.assertEqual(by_id["t-c"]["status"], "cleaned")
        self.assertFalse(os.path.exists(by_id["t-git"]["clone_path"]))
        self.assertTrue(os.path.exists(by_id["t-docs"]["clone_path"]))
        self.assertFalse(os.path.exists(by_id["t-sup"]["clone_path"]))

        # 批量路径同样必须落记录字段(与单任务 finalize 一致)
        records = {t["task_id"]: t for t in self._read_tasks()}
        self.assertFalse(records["t-git"]["pane_retained"])
        self.assertEqual(records["t-git"]["resource_retention"], "purged")
        self.assertTrue(records["t-git"].get("evidence"))

        closed_tabs = [
            args[2] for args in self.herdr_calls
            if args[:2] == ("tab", "close")
        ]
        self.assertEqual(sorted(closed_tabs), ["wA:t8", "wA:t9"])

        entry = self._read_workflows()["wf-test"]
        self.assertEqual(entry["status"], "completed")
        self.assertTrue(entry.get("completed_at"))

        with open(self.stage_state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertNotIn("wf-test:requirements", state)
        self.assertIn("other:requirements", state)

        self.assertNotIn(("pane", "close", "wA:p1"), self.herdr_calls)
        self.assertIn("include_coordinator", report)

    def test_dry_run_touches_nothing(self):
        tasks = [
            self._mk_task("t-git", status="cleaned",
                          integration_ref="refs/herdr/tasks/t-git"),
        ]
        self._write_tasks(tasks)
        with self._patch_herdr():
            report = _ht.close_workflow("wf-test", dry_run=True)
        self.assertEqual(report["tasks"][0]["action"], "dry-run")
        self.assertTrue(os.path.exists(report["tasks"][0]["clone_path"]))
        self.assertEqual(self._read_tasks()[0]["status"], "cleaned")
        self.assertEqual(
            [a for a in self.herdr_calls if a[:2] in
             {("pane", "close"), ("tab", "close")}],
            [],
        )
        self.assertEqual(
            self._read_workflows()["wf-test"].get("status"), "running")
        with open(self.stage_state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertIn("wf-test:requirements", state)

    def test_shared_tab_with_foreign_pane_is_skipped(self):
        self.live_panes.append(
            {"pane_id": "wA:pFOREIGN", "tab_id": "wA:t9"})
        self._write_tasks([
            self._mk_task("t-ok", status="cleaned",
                          integration_ref="refs/x"),
        ])
        with self._patch_herdr():
            report = _ht.close_workflow("wf-test")
        self.assertEqual(report["tabs_closed"], ["wA:t8"])
        self.assertIn("wA:t9", report["tabs_skipped"])
        self.assertIn("wA:pFOREIGN", report["tabs_skipped"]["wA:t9"])
        self.assertNotIn(("tab", "close", "wA:t9"), self.herdr_calls)

    def test_unlistable_panes_skip_all_tabs(self):
        # pane list 失败时宁可保留 tab,不做盲目销毁
        self.herdr_calls = []
        def broken(*args):
            self.herdr_calls.append(args)
            if args[:2] == ("pane", "list"):
                return _resp(1, "", "boom")
            return _resp(0, "")
        self._write_tasks([
            self._mk_task("t-ok", status="cleaned",
                          integration_ref="refs/x"),
        ])
        with patch.object(_ht, "_herdr", side_effect=broken):
            report = _ht.close_workflow("wf-test")
        self.assertEqual(report["tabs_closed"], [])
        self.assertEqual(
            set(report["tabs_skipped"]), {"wA:t8", "wA:t9"})

    def test_dry_run_reflects_ownership_check(self):
        # dry-run 必须与真实执行同一套归属校验,不能高估将关闭的 tab
        self.live_panes.append(
            {"pane_id": "wA:pFOREIGN", "tab_id": "wA:t9"})
        self._write_tasks([
            self._mk_task("t-ok", status="cleaned",
                          integration_ref="refs/x"),
        ])
        with self._patch_herdr():
            report = _ht.close_workflow("wf-test", dry_run=True)
        self.assertEqual(report["tabs_closed"], ["wA:t8"])
        self.assertIn("wA:t9", report["tabs_skipped"])
        self.assertIn("wA:pFOREIGN", report["tabs_skipped"]["wA:t9"])
        # dry-run 仍不得产生任何关闭动作
        closes = [
            a for a in self.herdr_calls
            if (a[0], a[1]) in {("pane", "close"), ("tab", "close")}
        ]
        self.assertEqual(closes, [])

    def test_include_coordinator_closes_coordinator_pane(self):
        self._write_tasks([self._mk_task("t-ok", status="cleaned",
                                         integration_ref="refs/x")])
        with self._patch_herdr():
            report = _ht.close_workflow(
                "wf-test", include_coordinator=True)
        self.assertTrue(report["coordinator_closed"])
        self.assertIn(("pane", "close", "wA:p1"), self.herdr_calls)

    def test_unknown_workflow_exits(self):
        self._write_tasks([])
        with self.assertRaises(SystemExit) as ctx:
            _ht.close_workflow("wf-unknown")
        self.assertEqual(ctx.exception.code, 1)

    def test_taskless_registered_workflow_marks_completed(self):
        self._write_tasks([])
        with self._patch_herdr():
            report = _ht.close_workflow("wf-test")
        self.assertEqual(report["tasks"], [])
        self.assertEqual(
            self._read_workflows()["wf-test"].get("status"), "completed")
        with open(self.stage_state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        self.assertNotIn("wf-test:requirements", state)


if __name__ == "__main__":
    unittest.main()
