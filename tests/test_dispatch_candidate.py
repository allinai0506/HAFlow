"""Dispatch-candidate recovery tests (P1).

r6 tested main instead of the feature branch (vacant blocked verdict),
and fix outputs never landed on any branch. Covers:
- planner carries a sanitized onto_branch into specs (pure);
- controller passes --onto to launch, and refuses vacuous candidates
  (fail-open on git errors);
- supersede auto-saves clone WIP to the task branch (best-effort).
"""

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

from herdr import direct_dispatch as planner


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name,
        importlib.machinery.SourceFileLoader(name, str(path)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctl = _load_module(
    "herdr_controller_dispatch_candidate_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)
_ht = _load_module(
    "herdr_task_dispatch_candidate_test",
    HERDR_ROOT / "bin" / "herdr-task",
)


def _node(node_id="test", **overrides):
    node = {
        "id": node_id,
        "label": node_id,
        "purpose": "验证实现结果是否满足需求与验收标准。",
        "default_task_type": "test",
        "default_integration_mode": "none",
        "required_outputs": ["测试结论"],
        "rules": [],
        "depends_on": ["implementation"],
    }
    node.update(overrides)
    return node


def _task(task_id, status, node, **extra):
    task = {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "node": node,
        "stage": node,
        "status": status,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    task.update(extra)
    return task


class PlannerOntoTest(unittest.TestCase):
    def test_redispatch_carries_onto_branch(self):
        old = _task(
            "wf-1-test-auto", "superseded", "test",
            goal="g", acceptance_criteria=["a"],
            integration_mode="none",
        )
        plan = planner.plan_stage_dispatch(
            "wf-1", _node(), [old], "req",
            context_branch="agent/opencode/feat-x",
        )
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(
            plan["specs"][0]["onto_branch"], "agent/opencode/feat-x"
        )

    def test_initial_dispatch_carries_onto_branch(self):
        plan = planner.plan_stage_dispatch(
            "wf-1", _node(), [], "req",
            context_branch="agent/opencode/feat-x",
        )
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(
            plan["specs"][0]["onto_branch"], "agent/opencode/feat-x"
        )

    def test_invalid_branches_filtered(self):
        for bad in ["", None, "  ", "../evil", "has space",
                    "-rf", "/abs/path", "a~b", "a^b", "a:b"]:
            plan = planner.plan_stage_dispatch(
                "wf-1", _node(), [], "req", context_branch=bad,
            )
            self.assertEqual(plan["mode"], "dispatch")
            self.assertIsNone(plan["specs"][0].get("onto_branch"))

    def test_candidate_branch_prefers_dependencies(self):
        tasks = [
            _task("impl-1", "cleaned", "implementation",
                  branch="agent/opencode/feat-new", updated_at=200.0),
            _task("t-old", "cleaned", "test",
                  branch="agent/codex/stale", updated_at=300.0),
        ]
        self.assertEqual(
            planner.candidate_branch_for_node(
                tasks, "wf-1", "test", ["implementation"]
            ),
            "agent/opencode/feat-new",
        )

    def test_candidate_branch_falls_back_to_own_node(self):
        tasks = [
            _task("impl-1", "cleaned", "implementation", updated_at=200.0),
        ]
        self.assertIsNone(
            planner.candidate_branch_for_node(
                tasks, "wf-1", "test", ["implementation"]
            )
        )


class ControllerOntoWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-cand-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "t@t"],
            check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "t"],
            check=True)
        (self.repo / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "base",
             "--no-gpg-sign"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "branch", "-M", "main"], check=True)
        self.commands = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            self.commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "Task dispatched: x", "")

        patchers = [
            patch.object(_ctl, "project_for_workflow", return_value={
                "startup_ready": True,
                "project_root": str(self.repo),
                "base_branch": "main",
                "coordinator_pane_id": "wA:p1",
                "requirement": "req text here",
            }),
            patch.object(_ctl, "load_tasks", return_value=[
                _task("impl-1", "cleaned", "implementation",
                      branch="agent/opencode/feat-new", updated_at=200.0),
            ]),
            patch.object(_ctl, "get_stage_policy", return_value={}),
            patch.object(_ctl, "node_is_gate", return_value=False),
            patch.object(_ctl, "shared_docs_block", return_value=""),
            patch.object(
                _ctl, "mark_stage_advance_notified", return_value=True),
            patch.object(
                _ctl, "maybe_compact_coordinator", return_value=False),
            patch("subprocess.run", side_effect=fake_run),
        ]
        for started in patchers:
            started.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patchers)])

    def tearDown(self):
        self.tmp.cleanup()

    def _item(self):
        return {
            "kind": "stage_advance",
            "workflow_id": "wf-1",
            "stage": "implementation",
            "node_id": "test",
            "next_stage": "test",
            "node": _node(),
        }

    def test_launch_receives_onto_from_dependency_branch(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-qb", "agent/opencode/feat-new"],
            check=True)
        (self.repo / "g.txt").write_text("y")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "feat",
             "--no-gpg-sign"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-q", "main"],
            check=True)
        self.assertTrue(_ctl.try_direct_stage_advance(self._item()))
        launch = [c for c in self.commands if "launch" in c]
        self.assertEqual(len(launch), 1)
        self.assertIn("--onto", launch[0])
        self.assertIn("agent/opencode/feat-new",
                      launch[0][launch[0].index("--onto") + 1])

    def test_vacuous_candidate_falls_back_without_launch(self):
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-qb", "agent/opencode/feat-empty"],
            check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "checkout", "-q", "main"],
            check=True)
        with patch.object(
            _ctl, "load_tasks", return_value=[
                _task("impl-1", "cleaned", "implementation",
                      branch="agent/opencode/feat-empty",
                      updated_at=200.0),
            ]):
            self.assertFalse(_ctl.try_direct_stage_advance(self._item()))
        self.assertEqual(
            [c for c in self.commands if "launch" in c], [])

    def test_git_failure_fails_open_to_launch(self):
        with patch.object(
            _ctl, "project_for_workflow", return_value={
                "startup_ready": True,
                "project_root": str(self.root / "no-such-repo"),
                "base_branch": "main",
                "coordinator_pane_id": "wA:p1",
                "requirement": "req text here",
            }):
            self.assertTrue(_ctl.try_direct_stage_advance(self._item()))
        self.assertEqual(len([c for c in self.commands if "launch" in c]), 1)


class SupersedeAutosaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-wip-")
        self.clone = Path(self.tmp.name) / "clone"
        self.clone.mkdir()
        subprocess.run(["git", "init", "-q", str(self.clone)], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "config", "user.email", "t@t"],
            check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "config", "user.name", "t"],
            check=True)
        (self.clone / "a.py").write_text("v1")
        subprocess.run(["git", "-C", str(self.clone), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "commit", "-qm", "base",
             "--no-gpg-sign"], check=True)
        subprocess.run(
            ["git", "-C", str(self.clone), "checkout", "-qb", "agent/task"],
            check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _task(self):
        return {
            "task_id": "t-fix",
            "branch": "agent/task",
            "clone_path": str(self.clone),
        }

    def test_wip_committed_without_internals(self):
        (self.clone / "a.py").write_text("v2")
        (self.clone / ".herdr-loop").mkdir()
        (self.clone / ".herdr-loop" / "STATE.md").write_text("x")
        result = _ht._autosave_clone_wip(self._task())
        self.assertTrue(result)
        log = subprocess.run(
            ["git", "-C", str(self.clone), "log", "--oneline", "-1"],
            text=True, capture_output=True, check=True)
        self.assertIn("t-fix", log.stdout)
        show = subprocess.run(
            ["git", "-C", str(self.clone), "show", "--name-only",
             "--format=", "HEAD"],
            text=True, capture_output=True, check=True)
        self.assertIn("a.py", show.stdout)
        self.assertNotIn(".herdr-loop", show.stdout)

    def test_clean_tree_no_commit(self):
        self.assertFalse(_ht._autosave_clone_wip(self._task()))

    def test_missing_clone_is_silent_noop(self):
        self.assertFalse(_ht._autosave_clone_wip({"task_id": "t"}))
        self.assertFalse(_ht._autosave_clone_wip(
            {"task_id": "t", "branch": "b",
             "clone_path": str(self.clone) + "-missing"}))


if __name__ == "__main__":
    unittest.main()
