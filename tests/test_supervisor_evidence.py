"""SupervisorState execution evidence.

The supervisor must judge from real HAFlow facts, not vibes:

- HERDR loop report / test result -> bounded ``tests`` facts;
- git status/diff --stat -> bounded ``diff_summary`` facts;
- Agent done report (verdict/blocker/status history + report tail) ->
  bounded ``output_summary``;
- ``collect_facts`` derives retry counts from real status history;
- the serialized state stays inside its budget and never carries raw
  stdout, full diffs or credentials.
"""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from herdr.supervisor.config import load_config
from herdr.supervisor.engine import SemanticSupervisor
from herdr.supervisor.evidence import (
    summarize_git,
    summarize_loop,
    summarize_report,
)
from herdr.supervisor.harness import collect_facts, run_checkpoint
from herdr.supervisor.state import build_supervisor_state

from tests.test_semantic_supervisor import (  # reuse doubles
    FakeStore,
    StubProvider,
    _task,
)

LONG_LINE = "Z" * 5000
SECRET = "sk-" + "a" * 24


def _write_loop_report(clone: Path) -> None:
    loop = clone / ".herdr-loop"
    loop.mkdir(parents=True, exist_ok=True)
    (loop / "STATE.md").write_text(
        "# 循环执行状态 (Loop State)\n\n"
        "- **iteration**: 2\n"
        "- **max_iterations**: 5\n"
        "- **status**: converged\n"
        "- **converged**: true\n",
        encoding="utf-8",
    )
    (loop / "METRICS.json").write_text(json.dumps({
        "correctness": 100.0,
        "quality": 100.0,
        "scope": 100.0,
        "repro": 100.0,
        "composite_score": 97.5,
        "passed_tests": 41,
        "total_tests": 43,
        "failing_tests": ["tests/test_a.py::test_x", "tests/test_b.py::test_y"],
        "lint_errors": 1,
        "type_errors": 0,
        "has_repro_test": False,
        "details": {"test_exit_code": 1},
    }), encoding="utf-8")


def _git(clone: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(clone)] + list(args),
        check=True, capture_output=True, text=True,
    )


def _init_git_workspace(clone: Path) -> None:
    _git(clone, "init", "-q")
    _git(clone, "config", "user.email", "test@example.com")
    _git(clone, "config", "user.name", "HAFlow Test")
    (clone / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-qm", "baseline")
    (clone / "README.md").write_text("baseline\nchanged\n", encoding="utf-8")
    (clone / "new_module.py").write_text("print('new')\n", encoding="utf-8")


class LoopEvidenceTests(unittest.TestCase):
    def test_loop_report_and_test_counts_are_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp)
            _write_loop_report(clone)
            facts = summarize_loop(str(clone))
        self.assertIsNotNone(facts)
        self.assertEqual(facts["loop_status"], "converged")
        self.assertEqual(facts["iteration"], 2)
        self.assertEqual(facts["total_tests"], 43)
        self.assertEqual(facts["passed_tests"], 41)
        self.assertEqual(facts["failing_count"], 2)
        self.assertEqual(facts["lint_errors"], 1)
        self.assertEqual(facts["composite_score"], 97.5)
        self.assertTrue(facts["converged"])

    def test_missing_loop_dir_is_absent_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(summarize_loop(tmp))
        self.assertIsNone(summarize_loop(None))


class GitEvidenceTests(unittest.TestCase):
    def test_git_summary_is_bounded_and_counts_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp)
            _init_git_workspace(clone)
            summary = summarize_git(str(clone))
        self.assertIsNotNone(summary)
        self.assertGreaterEqual(summary["files_changed"], 2)
        self.assertLessEqual(len(summary["sample_files"]), 8)
        self.assertIn("stat", summary)
        self.assertNotIn("changed\n+", json.dumps(summary), "no diff bodies")

    def test_git_failure_is_fail_safe(self):
        self.assertIsNone(summarize_git("/nonexistent-clone-path"))


class ReportEvidenceTests(unittest.TestCase):
    def test_report_merges_task_facts_and_bounded_tail(self):
        report = (
            "step 1 done\n"
            "HERDR_TASK_DONE:t-1\n"
            f"api_key={SECRET}\n"
            f"{LONG_LINE}\n"
        )
        task = _task(
            stage_verdict="pass",
            stage_verdict_note="all acceptance checks reviewed",
            status_history=[
                {"from": "working", "to": "rework", "reason": "tests failed"},
                {"from": "rework", "to": "working"},
                {"from": "working", "to": "agent_done"},
            ],
        )
        summary = summarize_report(task, report_text=report)
        self.assertIn("verdict_note=", summary)
        self.assertIn("recent_transitions=working->rework", summary)
        self.assertIn("agent_tail=", summary)
        self.assertNotIn(SECRET, summary)
        self.assertNotIn(LONG_LINE, summary)
        self.assertLessEqual(len(summary), 400)

    def test_agent_tail_survives_busy_failure_fields(self):
        task = _task(
            stage_verdict_note="N" * 400,
            failure_reason="F" * 400,
            error="E" * 400,
            message="M" * 400,
            last_result="L" * 400,
        )
        summary = summarize_report(task, report_text="agent final summary line")
        self.assertIn("agent_tail=agent final summary line", summary)
        self.assertLessEqual(len(summary), 400)

    def test_collect_facts_derives_attempt_count_from_status_history(self):
        task = _task(status_history=[
            {"from": "working", "to": "rework"},
            {"from": "rework", "to": "working"},
            {"from": "working", "to": "rework"},
            {"from": "rework", "to": "working"},
        ])
        facts = collect_facts(task)
        self.assertEqual(facts["attempt_count"], 2)


class SupervisorStateEvidenceTests(unittest.TestCase):
    def test_checkpoint_state_carries_bounded_real_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp)
            _write_loop_report(clone)
            _init_git_workspace(clone)
            report = (
                "HERDR_TASK_DONE:t-1\n"
                f"api_key={SECRET}\n"
                f"{LONG_LINE}\n"
            )
            provider = StubProvider()
            config = load_config(path="/nonexistent-supervisor.json")
            config["interval"] = 0
            config["cooldown"] = 0
            result = run_checkpoint(
                task=_task(clone_path=str(clone),
                           acceptance_criteria=["tests pass", "bounded evidence"]),
                trigger="agent_done",
                store=FakeStore(),
                config=config,
                supervisor=SemanticSupervisor(config, provider),
                report_reader=lambda: report,
                log=lambda _m: None,
            )
        self.assertIsNotNone(result)
        state = provider.states[0]
        self.assertEqual(state["tests"]["total_tests"], 43)
        self.assertEqual(state["tests"]["passed_tests"], 41)
        self.assertGreaterEqual(state["diff_summary"]["files_changed"], 2)
        self.assertIn("stat", state["diff_summary"])
        self.assertIn("recent_output_summary", state)
        self.assertIn("acceptance_criteria", state)

        blob = json.dumps(state, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(LONG_LINE, blob)
        self.assertLessEqual(len(blob), config["max_context_size"],
                             "evidence must stay inside the context budget")

    def test_state_builder_caps_evidence_dicts(self):
        facts = {
            "tests": {f"k{i}": i for i in range(30)},
            "diff_summary": {f"d{i}": "x" * 500 for i in range(30)},
            "output_summary": LONG_LINE,
        }
        state = build_supervisor_state(
            _task(acceptance_criteria=[f"AC-{i}" for i in range(30)]),
            now=time.time(),
            facts=facts,
            max_context_size=2000,
        )
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False)), 2000)

    def test_budget_is_absolute_even_for_identity_fields(self):
        task = _task(task_id="t" * 5000, node="n" * 5000, agent="a" * 5000)
        task["runtime"] = {"status": "running", "agent_name": "x" * 5000}
        state = build_supervisor_state(task, now=time.time(), max_context_size=500)
        self.assertLessEqual(len(json.dumps(state, ensure_ascii=False)), 500)
        self.assertIn("task_id", state)


if __name__ == "__main__":
    unittest.main()
