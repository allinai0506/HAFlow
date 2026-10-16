"""Agent execution quality assessment V1.

The assessor copies settled outcome facts and reads the existing trace.
It does not recompute qualified success, write the task database, or publish
experience rules.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import state_db
from herdr.execution_outcome import outcome_id_for
from herdr.quality_assessment import ASSESSOR_VERSION, assess_quality, assess_task_run


ROOT = Path(__file__).resolve().parents[1]
TASK = "task-1"
RUN = "run-1"
OTHER = "run-2"


def _outcome(*, success=True, rework=0, blocked=0, human=0, run_id=RUN, task_id=TASK):
    return {
        "outcome_id": outcome_id_for(task_id, run_id),
        "run_id": run_id,
        "task_id": task_id,
        "qualified_success": success,
        "rework_count": rework,
        "blocked_count": blocked,
        "human_intervention_count": human,
        "verification_passed": success,
    }


def _event(kind, ref, run_id=RUN, **details):
    event = {
        "id": f"trace:{ref}",
        "kind": kind,
        "run_id": run_id,
        "source": "native_session",
        "source_ref": ref,
        "status": "error" if kind == "test_failed" else "success",
        "details": details,
    }
    return event


def _action(ref, command, exit_code, *, retry_of=None, ordinal=0):
    return {
        "source_ref": ref,
        "command": command,
        "exit_code": exit_code,
        "retry_of": retry_of,
        "ordinal": ordinal,
        "timestamp": ordinal,
    }


class QualityJudgementTest(unittest.TestCase):
    def test_clean_success_has_no_process_finding(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("test_passed", "t1", command="pytest", exit_code=0)],
        )
        self.assertEqual(report["assessor_version"], ASSESSOR_VERSION)
        self.assertEqual(report["outcome_id"], outcome_id_for(TASK, RUN))
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["process_quality"], "acceptable")
        self.assertEqual(report["rework_count"], 0)
        self.assertEqual(report["blocked_count"], 0)
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["trace_completeness"], "complete")
        self.assertEqual(report["problem_codes"], [])
        self.assertEqual(report["evidence"], [{
            "kind": "outcome",
            "ref": outcome_id_for(TASK, RUN),
        }])

    def test_success_with_rework_and_blocks_is_an_improvement_opportunity(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(rework=3, blocked=2),
            events=[_event("test_passed", "t1", command="pytest", exit_code=0)],
        )
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["process_quality"], "improvement_opportunity")
        self.assertEqual(report["rework_count"], 3)
        self.assertEqual(report["blocked_count"], 2)
        self.assertEqual(
            report["problem_codes"],
            ["blocked_observed", "rework_observed"],
        )

    def test_business_failure_stays_a_failure(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(success=False),
            events=[_event("test_failed", "t1", command="pytest", exit_code=1)],
        )
        self.assertEqual(report["correctness"], "failure")
        self.assertEqual(report["process_quality"], "improvement_opportunity")
        self.assertIn("business_failure", report["problem_codes"])
        self.assertEqual(report["test_anomalies"], [{
            "code": "test_failed",
            "evidence_ref": "trace:t1",
        }])

    def test_missing_verification_is_unknown(self):
        report = assess_quality(TASK, RUN, outcome=None, events=[])
        self.assertIsNone(report["outcome_id"])
        self.assertEqual(report["correctness"], "unknown")
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIsNone(report["rework_count"])
        self.assertIsNone(report["blocked_count"])
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertEqual(
            report["problem_codes"],
            ["outcome_absent", "trace_missing", "verification_missing"],
        )

    def test_native_test_failure_does_not_replace_a_missing_receipt(self):
        report = assess_quality(
            TASK, RUN,
            outcome=None,
            events=[_event("test_failed", "t1", command="pytest", exit_code=1)],
        )
        self.assertEqual(report["correctness"], "unknown")
        self.assertEqual(report["process_quality"], "improvement_opportunity")
        self.assertIn("verification_missing", report["problem_codes"])
        self.assertIn("test_anomaly", report["problem_codes"])
        self.assertNotIn("business_failure", report["problem_codes"])

    def test_trajectory_verification_is_a_receipt_even_when_it_failed(self):
        event = _event("test_failed", "t1", command="pytest", exit_code=1)
        event["source"] = "trajectory"
        report = assess_quality(TASK, RUN, outcome=None, events=[event])
        self.assertEqual(report["correctness"], "unknown")
        self.assertNotIn("verification_missing", report["problem_codes"])
        self.assertIn("test_anomaly", report["problem_codes"])

    def test_unattributed_native_session_is_not_a_failure(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("task_completed", "status", run_id=RUN)],
            degradation=[{"code": "native_unverified", "message": "未归属"}],
        )
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["process_quality"], "unknown")
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertIn("native_unattributed", report["problem_codes"])
        self.assertNotIn("business_failure", report["problem_codes"])

    def test_truncated_trace_is_partial(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("test_passed", "t1", command="pytest", exit_code=0)],
            degradation=[{"code": "native_truncated", "message": "截断"}],
        )
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["trace_completeness"], "partial")
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("trace_truncated", report["problem_codes"])

    def test_other_run_events_are_dropped(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[
                _event("test_passed", "mine", command="pytest", exit_code=0),
                _event("test_failed", "theirs", run_id=OTHER, command="pytest", exit_code=1),
            ],
        )
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["process_quality"], "acceptable")
        self.assertEqual(report["test_anomalies"], [])
        self.assertIn("foreign_run_dropped", report["problem_codes"])
        refs = [item["ref"] for item in report["evidence"]]
        self.assertNotIn("trace:theirs", refs)

    def test_explicit_retry_does_not_change_rework_count(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(rework=0),
            events=[
                _event("test_failed", "t1", command="pytest tests/a.py", exit_code=1),
                _event("retry", "t2:retry"),
                _event("test_passed", "t2", command="pytest tests/a.py", exit_code=0),
            ],
            actions=[
                _action("t1", "pytest tests/a.py", 1),
                _action("t2", "pytest tests/a.py", 0, retry_of="t1", ordinal=1),
            ],
        )
        self.assertEqual(report["rework_count"], 0)
        self.assertEqual(report["process_quality"], "improvement_opportunity")
        self.assertIn("explicit_retry", report["problem_codes"])
        self.assertNotIn("implicit_retry", report["problem_codes"])

    def test_implicit_retry_needs_the_same_failed_command(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("test_passed", "seen", command="pytest", exit_code=0)],
            actions=[
                _action("a", "pytest tests/a.py", 1),
                _action("b", "pytest tests/a.py", 0, ordinal=1),
                _action("c", "pytest tests/b.py", 1, ordinal=2),
            ],
        )
        self.assertIn("implicit_retry", report["problem_codes"])
        self.assertEqual(report["process_quality"], "improvement_opportunity")
        self.assertEqual(report["rework_count"], 0)
        refs = {item["ref"] for item in report["evidence"]}
        self.assertIn("a", refs)
        self.assertIn("b", refs)
        self.assertNotIn("c", refs)

    def test_missing_test_exit_code_is_unknown(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("tool_call", "t1", command="pytest tests/a.py")],
            actions=[_action("t1", "pytest tests/a.py", None)],
        )
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("exit_code_unknown", report["problem_codes"])
        self.assertNotIn("business_failure", report["problem_codes"])

    def test_workflow_decision_is_not_a_quality_defect(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(human=1),
            events=[_event("test_passed", "t1", command="pytest", exit_code=0)],
            human_signals={
                "steers": [{
                    "ref": "steer-decision",
                    "run_id": RUN,
                    "operator": "human",
                    "decision_id": "approve-release",
                    "kind": "decision",
                }],
            },
        )
        self.assertEqual(report["workflow_decision_count"], 1)
        self.assertEqual(report["corrective_human_count"], 0)
        self.assertEqual(report["process_quality"], "acceptable")
        self.assertNotIn("corrective_human", report["problem_codes"])
        self.assertNotIn("human_intervention_unclassified", report["problem_codes"])

    def test_run_scoped_correction_is_an_improvement_signal(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(human=1),
            events=[_event("test_passed", "t1", command="pytest", exit_code=0)],
            human_signals={
                "steers": [{
                    "ref": "steer-fix",
                    "run_id": RUN,
                    "operator": "human",
                    "decision_id": "",
                    "kind": "",
                }],
            },
        )
        self.assertEqual(report["corrective_human_count"], 1)
        self.assertEqual(report["process_quality"], "improvement_opportunity")
        self.assertIn("corrective_human", report["problem_codes"])

    def test_unscoped_human_count_is_not_treated_as_correction(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(human=2),
            events=[_event("test_passed", "t1", command="pytest", exit_code=0)],
            human_signals={
                "steers": [{
                    "ref": "steer-loose",
                    "run_id": "",
                    "operator": "human",
                    "decision_id": "",
                    "kind": "",
                }],
            },
        )
        self.assertEqual(report["corrective_human_count"], 0)
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("human_intervention_unclassified", report["problem_codes"])
        self.assertNotIn("corrective_human", report["problem_codes"])

    def test_legacy_unscoped_events_stay_unknown_until_the_owning_run_allows_them(self):
        failed = _event("test_failed", "old", run_id=None, command="pytest", exit_code=1)
        hidden = assess_quality(
            TASK, RUN, outcome=_outcome(), events=[failed], allow_legacy_unscoped=False,
        )
        visible = assess_quality(
            TASK, RUN, outcome=_outcome(), events=[failed], allow_legacy_unscoped=True,
        )
        self.assertEqual(hidden["test_anomalies"], [])
        self.assertIn("unscoped_evidence_ignored", hidden["problem_codes"])
        self.assertEqual(visible["test_anomalies"], [{
            "code": "test_failed",
            "evidence_ref": "trace:old",
        }])


class QualityReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-quality-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"
        self.addCleanup(self._restore_env)
        self._old_env = os.environ.get("HERDR_STATE_DB")
        self._old_auto = os.environ.get("HERDR_OUTCOME_AUTOFINALIZE")
        self._old_exp = os.environ.get("HERDR_EXPERIENCE_COLLECTION_DISABLED")
        os.environ["HERDR_STATE_DB"] = str(self.db)
        os.environ["HERDR_OUTCOME_AUTOFINALIZE"] = "0"
        os.environ["HERDR_EXPERIENCE_COLLECTION_DISABLED"] = "1"
        state_db.init_db(self.db)
        self._seed()

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("HERDR_STATE_DB", None)
        else:
            os.environ["HERDR_STATE_DB"] = self._old_env
        if self._old_auto is None:
            os.environ.pop("HERDR_OUTCOME_AUTOFINALIZE", None)
        else:
            os.environ["HERDR_OUTCOME_AUTOFINALIZE"] = self._old_auto
        if self._old_exp is None:
            os.environ.pop("HERDR_EXPERIENCE_COLLECTION_DISABLED", None)
        else:
            os.environ["HERDR_EXPERIENCE_COLLECTION_DISABLED"] = self._old_exp

    def _seed(self):
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO workflows (workflow_id, title, status, created_at, updated_at) "
                "VALUES ('wf-1', 'wf', 'pending', 1, 1)"
            )
            payload = json.dumps({
                "run_id": OTHER,
                "task_type": "dev",
                "status_history": ["pending", "working", "completed"],
            })
            conn.execute(
                "INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, "
                "payload_json, created_at, updated_at, version) "
                "VALUES (?, 'wf-1', 'test', 'test', 'fixture', 'completed', ?, 1, 2, 1)",
                (TASK, payload),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 10, 'trajectory', ?, 1)",
                (TASK, json.dumps({"verification": {"passed": False, "evidence_id": "ev-b"}}), OTHER),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 11, 'trajectory', ?, 2)",
                (TASK, json.dumps({"verification": {"passed": True, "evidence_id": "ev-a"}}), RUN),
            )
            conn.execute(
                "INSERT INTO steering_items (steer_id, task_id, workflow_id, instruction, "
                "operator, status, created_at, payload_json) VALUES "
                "('steer-b', ?, 'wf-1', 'fix the other run', 'human', 'done', 4, ?)",
                (TASK, json.dumps({"run_id": OTHER})),
            )
            conn.commit()
        finally:
            conn.close()
        state_db.insert_execution_outcome({
            "outcome_id": outcome_id_for(TASK, RUN),
            "run_id": RUN,
            "task_id": TASK,
            "workflow_id": "wf-1",
            "agent": "fixture",
            "node": "test",
            "task_type": "dev",
            "final_status": "completed",
            "requirements_satisfied": True,
            "verification_passed": True,
            "qualified_success": True,
            "rework_count": 0,
            "blocked_count": 0,
            "human_intervention_count": 0,
            "recorded_at": 20.0,
            "source_eval_id": "eval-a",
            "source_eval_revision": 1,
            "source_task_version": 1,
            "schema_version": 1,
        }, db_path=self.db)

    def test_historical_receipt_hides_a_superseded_test_failure(self):
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 9, 'trajectory', ?, 3)",
                (TASK, json.dumps({
                    "verification": {"failing_count": 3, "evidence_id": "ev-a"},
                }), RUN),
            )
            conn.commit()
        finally:
            conn.close()
        report = assess_task_run(TASK, RUN, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["test_anomalies"], [])

    def test_historical_failed_receipt_is_counted_once(self):
        run_id = "run-hist"
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 13, 'trajectory', ?, 1)",
                (TASK, json.dumps({
                    "verification": {"failing_count": 2, "evidence_id": "ev-h"},
                }), run_id),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 14, 'trajectory', ?, 2)",
                (TASK, json.dumps({
                    "verification": {"passed": False, "evidence_id": "ev-h"},
                }), run_id),
            )
            conn.commit()
        finally:
            conn.close()
        report = assess_task_run(TASK, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(len(report["test_anomalies"]), 1)
        self.assertEqual(report["test_anomalies"][0]["code"], "test_failed")
        self.assertTrue(report["test_anomalies"][0]["evidence_ref"])

    def test_read_is_isolated_and_does_not_change_the_database(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        report = assess_task_run(TASK, RUN, db_path=self.db, budget_s=0.2)
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["outcome_id"], outcome_id_for(TASK, RUN))
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["corrective_human_count"], 0)
        self.assertNotIn("trace:theirs", [item["ref"] for item in report["evidence"]])
        current = assess_task_run(TASK, OTHER, db_path=self.db, budget_s=0.2)
        self.assertIsNone(current["outcome_id"])
        self.assertEqual(current["test_anomalies"], [{
            "code": "test_failed",
            "evidence_ref": current["test_anomalies"][0]["evidence_ref"],
        }])
        self.assertTrue(current["test_anomalies"][0]["evidence_ref"])
        self.assertNotIn(outcome_id_for(TASK, RUN), [
            item["ref"] for item in current["evidence"]
        ])

    def test_plain_agent_without_a_clone_can_be_acceptable(self):
        task_id = "task-plain"
        run_id = "run-plain"
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, "
                "payload_json, created_at, updated_at, version) "
                "VALUES (?, 'wf-1', 'test', 'test', 'fixture', 'completed', ?, 1, 2, 1)",
                (task_id, json.dumps({"run_id": run_id, "task_type": "dev"})),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 12, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-plain"}}), run_id),
            )
            conn.commit()
        finally:
            conn.close()
        state_db.insert_execution_outcome({
            "outcome_id": outcome_id_for(task_id, run_id),
            "run_id": run_id,
            "task_id": task_id,
            "workflow_id": "wf-1",
            "agent": "fixture",
            "node": "test",
            "task_type": "dev",
            "final_status": "completed",
            "requirements_satisfied": True,
            "verification_passed": True,
            "qualified_success": True,
            "rework_count": 0,
            "blocked_count": 0,
            "human_intervention_count": 0,
            "recorded_at": 30.0,
            "source_eval_id": "eval-plain",
            "source_eval_revision": 1,
            "source_task_version": 1,
            "schema_version": 1,
        }, db_path=self.db)
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["process_quality"], "acceptable")
        self.assertEqual(report["trace_completeness"], "complete")
        self.assertNotIn("native_unattributed", report["problem_codes"])

    def test_missing_database_returns_a_report(self):
        missing = Path(self.tmp.name) / "absent.db"
        report = assess_task_run(TASK, RUN, db_path=missing)
        self.assertEqual(report["correctness"], "unknown")
        self.assertEqual(report["problem_codes"], ["assessment_unavailable"])

    def test_judgment_error_returns_a_report(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with patch(
            "herdr.quality_sources.load_quality_inputs",
            return_value={"outcome": None},
        ), patch(
            "herdr.quality_assessment.assess_quality",
            side_effect=RuntimeError("boom"),
        ):
            report = assess_task_run(TASK, RUN, db_path=self.db)
        self.assertEqual(report["correctness"], "unknown")
        self.assertEqual(report["problem_codes"], ["assessment_unavailable"])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_blank_identity_does_not_open_the_database(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        report = assess_task_run("  ", RUN, db_path=self.db)
        self.assertEqual(report["problem_codes"], ["identity_incomplete"])
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_cli_prints_json_without_writing_the_database(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        env = os.environ.copy()
        env["HERDR_STATE_DB"] = str(self.db)
        completed = subprocess.run(
            [
                str(ROOT / "bin" / "herdr-quality"),
                "assess",
                "--task-id", TASK,
                "--run-id", RUN,
                "--db-path", str(self.db),
            ],
            cwd=self.tmp.name,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["task_id"], TASK)
        self.assertEqual(report["run_id"], RUN)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_settlement_and_experience_paths_do_not_call_the_assessor(self):
        controller = (ROOT / "services" / "herdr-controller.py").read_text(encoding="utf-8")
        outcome = (ROOT / "herdr" / "execution_outcome.py").read_text(encoding="utf-8")
        experience = (ROOT / "herdr" / "experience" / "models.py").read_text(encoding="utf-8")
        self.assertNotIn("quality_assessment", controller)
        self.assertNotIn("quality_assessment", outcome)
        self.assertNotIn("quality_assessment", experience)


class ReadonlyOutcomeTest(unittest.TestCase):
    def test_readonly_reader_does_not_create_a_database(self):
        tmp = tempfile.TemporaryDirectory(prefix="herdr-quality-missing-")
        self.addCleanup(tmp.cleanup)
        missing = Path(tmp.name) / "state.db"
        with self.assertRaises(FileNotFoundError):
            state_db.read_execution_outcome(TASK, RUN, missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
