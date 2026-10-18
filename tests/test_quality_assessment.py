"""Agent execution quality assessment V1.

The assessor copies settled outcome facts and reads the existing trace.
It does not recompute qualified success, write the task database, or publish
experience rules.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import state_db
from herdr.execution_outcome import outcome_id_for
from herdr.quality_assessment import ASSESSOR_VERSION, assess_quality, assess_task_run
from herdr.quality_sources import _HUMAN_CAP


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

    def test_event_cap_preserves_test_failures_beyond_the_cap(self):
        from herdr.quality_assessment import _EVENT_CAP
        events = [_event("task_started", f"filler-{i}") for i in range(_EVENT_CAP + 5)]
        events.insert(_EVENT_CAP, _event("test_failed", "late-fail", command="pytest", exit_code=1))
        report = assess_quality(TASK, RUN, outcome=_outcome(), events=events)
        self.assertIn("trace_truncated", report["problem_codes"])
        self.assertIn("test_anomaly", report["problem_codes"])
        self.assertEqual(report["process_quality"], "improvement_opportunity")

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

    def test_missing_timestamp_keeps_later_retry_in_input_order(self):
        failed = _action("a", "git status", 1, ordinal=1)
        failed["timestamp"] = 10
        later = _action("b", "git status", 0, ordinal=2)
        later["timestamp"] = None
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("test_passed", "seen", command="pytest", exit_code=0)],
            actions=[failed, later],
        )
        self.assertIn("implicit_retry", report["problem_codes"])
        self.assertEqual(report["process_quality"], "improvement_opportunity")

    def test_complete_timestamps_still_follow_time_order(self):
        later = _action("b", "git status", 0, ordinal=2)
        later["timestamp"] = 20
        earlier = _action("a", "git status", 1, ordinal=1)
        earlier["timestamp"] = 10
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("test_passed", "seen", command="pytest", exit_code=0)],
            actions=[later, earlier],
        )
        self.assertIn("implicit_retry", report["problem_codes"])

    def test_missing_timestamps_do_not_pair_retries_across_sessions(self):
        older_failure = _action("a", "git status", 1, ordinal=1)
        older_failure.update({"session_id": "sess-a", "timestamp": None})
        newer_success = _action("b", "git status", 0, ordinal=1)
        newer_success.update({"session_id": "sess-b", "timestamp": None})
        for actions in ([older_failure, newer_success], [newer_success, older_failure]):
            with self.subTest(order=[item["source_ref"] for item in actions]):
                report = assess_quality(
                    TASK, RUN,
                    outcome=_outcome(),
                    events=[_event("test_passed", "seen", command="pytest", exit_code=0)],
                    actions=actions,
                )
                self.assertNotIn("implicit_retry", report["problem_codes"])

    def test_missing_timestamps_still_pair_retries_inside_one_session(self):
        failed = _action("a", "git status", 1, ordinal=1)
        failed.update({"session_id": "sess-a", "timestamp": None})
        later = _action("b", "git status", 0, ordinal=2)
        later.update({"session_id": "sess-a", "timestamp": None})
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("test_passed", "seen", command="pytest", exit_code=0)],
            actions=[failed, later],
        )
        self.assertIn("implicit_retry", report["problem_codes"])
        self.assertEqual(report["process_quality"], "improvement_opportunity")

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

    def test_unknown_exit_code_of_a_non_pytest_runner_is_unknown(self):
        for command in ("npm test", "pnpm test", "yarn test", "go test ./...", "cargo test"):
            with self.subTest(command=command):
                report = assess_quality(
                    TASK, RUN,
                    outcome=_outcome(),
                    events=[_event("test_passed", "receipt", command="pytest", exit_code=0)],
                    actions=[_action("t1", command, None)],
                )
                self.assertEqual(report["correctness"], "success")
                self.assertEqual(report["process_quality"], "unknown")
                self.assertIn("exit_code_unknown", report["problem_codes"])

    def test_corrupt_native_session_is_unknown(self):
        report = assess_quality(
            TASK, RUN,
            outcome=_outcome(),
            events=[_event("test_passed", "t1", command="pytest", exit_code=0)],
            degradation=[{"code": "native_corrupt", "message": "记录损坏"}],
        )
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("native_corrupt", report["problem_codes"])
        self.assertNotIn("business_failure", report["problem_codes"])

    def test_python_runner_forms_stay_recognized(self):
        for command in ("pytest tests/a.py", "py.test tests/a.py", "tox", "python3 -m pytest tests/a.py"):
            with self.subTest(command=command):
                report = assess_quality(
                    TASK, RUN,
                    outcome=_outcome(),
                    events=[_event("test_passed", "receipt", command="pytest", exit_code=0)],
                    actions=[_action("t1", command, None)],
                )
                self.assertIn("exit_code_unknown", report["problem_codes"])

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

    def test_human_reads_stay_on_the_requested_run_and_cap(self):
        task_id = "task-humans"
        run_id = "run-humans"
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
                (task_id, json.dumps({
                    "verification": {"passed": True, "evidence_id": "ev-humans"},
                }), run_id),
            )
            conn.execute(
                "INSERT INTO steering_items (steer_id, task_id, instruction, operator, "
                "created_at, payload_json) VALUES (?, ?, 'fix it', 'human', 1, ?)",
                ("steer-match", task_id, json.dumps({"run_id": run_id})),
            )
            for index in range(_HUMAN_CAP + 1):
                conn.execute(
                    "INSERT INTO steering_items (steer_id, task_id, instruction, operator, "
                    "created_at, payload_json) VALUES (?, ?, 'other', 'human', ?, ?)",
                    (f"steer-other-{index}", task_id, 10 + index, json.dumps({"run_id": "run-other"})),
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
            "human_intervention_count": 1,
            "recorded_at": 40.0,
            "source_eval_id": "eval-humans",
            "source_eval_revision": 1,
            "source_task_version": 1,
            "schema_version": 1,
        }, db_path=self.db)
        matched = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(matched["corrective_human_count"], 1)
        self.assertEqual(matched["process_quality"], "improvement_opportunity")
        self.assertNotIn("human_signals_truncated", matched["problem_codes"])

        conn = state_db.get_db_connection(self.db)
        try:
            for index in range(_HUMAN_CAP + 1):
                conn.execute(
                    "INSERT INTO steering_items (steer_id, task_id, instruction, operator, "
                    "created_at, payload_json) VALUES (?, ?, 'approve', 'human', ?, ?)",
                    (
                        f"steer-cap-{index}",
                        task_id,
                        100 + index,
                        json.dumps({"run_id": run_id, "decision_id": f"d-{index}", "kind": "decision"}),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        capped = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertIn("human_signals_truncated", capped["problem_codes"])
        self.assertLessEqual(capped["workflow_decision_count"], _HUMAN_CAP)
        self.assertNotEqual(capped["process_quality"], "acceptable")

    def test_malformed_steering_payload_does_not_erase_the_assessment(self):
        task_id = "task-bad-json"
        run_id = "run-bad-json"
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
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-bad"}}), run_id),
            )
            conn.execute(
                "INSERT INTO steering_items (steer_id, task_id, instruction, operator, "
                "created_at, payload_json) VALUES ('steer-bad', ?, 'broken', 'human', 5, '{not json')",
                (task_id,),
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
            "recorded_at": 50.0,
            "source_eval_id": "eval-bad-json",
            "source_eval_revision": 1,
            "source_task_version": 1,
            "schema_version": 1,
        }, db_path=self.db)
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertNotIn("assessment_unavailable", report["problem_codes"])
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["process_quality"], "acceptable")
        self.assertEqual(report["corrective_human_count"], 0)

    def _insert_task(self, task_id, run_id, *, clone_path=None, agent="fixture"):
        payload = {"run_id": run_id, "task_type": "dev"}
        if clone_path:
            payload["clone_path"] = clone_path
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, "
                "payload_json, created_at, updated_at, version) "
                "VALUES (?, 'wf-1', 'test', 'test', ?, 'completed', ?, 1, 2, 1)",
                (task_id, agent, json.dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

    def _insert_outcome(self, task_id, run_id, *, human=0, recorded_at=60.0, eval_id="eval-extra"):
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
            "human_intervention_count": human,
            "recorded_at": recorded_at,
            "source_eval_id": eval_id,
            "source_eval_revision": 1,
            "source_task_version": 1,
            "schema_version": 1,
        }, db_path=self.db)

    def _insert_scoped_verification(self, task_id, run_id, *, passed=True, evidence="ev-extra", timestamp=12):
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, ?, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"passed": passed, "evidence_id": evidence}}),
                 timestamp, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_unscoped_pass_cannot_complete_a_persisted_run(self):
        task_id, run_id = "task-scoped", "run-scoped"
        self._insert_task(task_id, run_id)
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 9, 'trajectory', NULL, 1)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-old"}})),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, run_id, eval_id="eval-scoped")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertIn("unscoped_evidence_ignored", report["problem_codes"])
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertNotEqual(report["process_quality"], "acceptable")

    def test_legacy_unscoped_failure_is_not_an_anomaly_for_a_persisted_run(self):
        task_id, run_id = "task-scoped-fail", "run-scoped-fail"
        self._insert_task(task_id, run_id)
        self._insert_scoped_verification(task_id, run_id, evidence="ev-scoped")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 9, 'trajectory', NULL, 1)",
                (task_id, json.dumps({"verification": {"passed": False, "evidence_id": "ev-old"}})),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, run_id, eval_id="eval-scoped-fail")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertIn("unscoped_evidence_ignored", report["problem_codes"])
        self.assertEqual(report["test_anomalies"], [])
        self.assertNotIn("test_anomaly", report["problem_codes"])

    def test_corrupt_sibling_task_does_not_erase_the_assessment(self):
        task_id, run_id = "task-clone", "run-clone"
        self._insert_task(task_id, run_id, clone_path="/tmp/herdr-shared-clone")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, workflow_id, node, stage, agent, status, "
                "payload_json, created_at, updated_at, version) "
                "VALUES ('task-broken', 'wf-1', 'test', 'test', 'fixture', 'completed', ?, 1, 2, 1)",
                ("{not json",),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_scoped_verification(task_id, run_id, evidence="ev-clone")
        self._insert_outcome(task_id, run_id, eval_id="eval-clone")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertNotIn("assessment_unavailable", report["problem_codes"])
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["trace_completeness"], "complete")

    def test_truncated_event_window_keeps_the_authoritative_receipt(self):
        from herdr.execution_trace import STORED_LIMIT

        task_id, run_id = "task-cap", "run-cap"
        self._insert_task(task_id, run_id)
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"failing_count": 3, "evidence_id": "ev-cap"}}), run_id),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_id, 10 + index, run_id, index + 2) for index in range(STORED_LIMIT - 1)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100000, 'trajectory', ?, 5000)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-cap"}}), run_id),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, run_id, eval_id="eval-cap")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["test_anomalies"], [])
        self.assertNotIn("test_anomaly", report["problem_codes"])

    def test_truncated_historical_event_window_keeps_the_authoritative_receipt(self):
        from herdr.execution_trace import STORED_LIMIT

        task_id = "task-hist-cap"
        hist_run = "run-hist-cap"
        current_run = "run-current-cap"
        self._insert_task(task_id, current_run)
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"failing_count": 3, "evidence_id": "ev-hist-cap"}}), hist_run),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_id, 10 + index, hist_run, index + 2) for index in range(STORED_LIMIT - 1)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100000, 'trajectory', ?, 5000)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-hist-cap"}}), hist_run),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, hist_run, eval_id="eval-hist-cap")
        report = assess_task_run(task_id, hist_run, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["test_anomalies"], [])
        self.assertNotIn("test_anomaly", report["problem_codes"])
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("historical_run_trace_limited", report["problem_codes"])
        self.assertIn("trace_truncated", report["problem_codes"])

    def test_historical_receipt_supplement_boundary_2000_and_1999(self):
        from herdr.execution_trace import STORED_LIMIT

        task_1999 = "task-boundary-1999"
        run_1999 = "run-boundary-1999"
        self._insert_task(task_1999, "other-run")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_1999, json.dumps({"verification": {"failing_count": 3, "evidence_id": "ev-1999"}}), run_1999),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_1999, 10 + index, run_1999, index + 2) for index in range(STORED_LIMIT - 3)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 50000, 'trajectory', ?, 2000)",
                (task_1999, json.dumps({"verification": {"passed": True, "evidence_id": "ev-1999"}}), run_1999),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_1999, run_1999, eval_id="eval-1999")
        report_1999 = assess_task_run(task_1999, run_1999, db_path=self.db, budget_s=0.2)
        self.assertEqual(report_1999["test_anomalies"], [])
        self.assertNotIn("test_anomaly", report_1999["problem_codes"])
        self.assertNotIn("trace_truncated", report_1999["problem_codes"])
        self.assertEqual(report_1999["trace_completeness"], "unknown")
        self.assertEqual(report_1999["process_quality"], "unknown")

        task_2000 = "task-boundary-2000"
        run_2000 = "run-boundary-2000"
        self._insert_task(task_2000, "other-run")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_2000, json.dumps({"verification": {"failing_count": 3, "evidence_id": "ev-2000"}}), run_2000),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_2000, 10 + index, run_2000, index + 2) for index in range(STORED_LIMIT - 2)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 50000, 'trajectory', ?, 2000)",
                (task_2000, json.dumps({"verification": {"passed": True, "evidence_id": "ev-2000"}}), run_2000),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_2000, run_2000, eval_id="eval-2000")
        report_2000 = assess_task_run(task_2000, run_2000, db_path=self.db, budget_s=0.2)
        self.assertEqual(report_2000["test_anomalies"], [])
        self.assertNotIn("test_anomaly", report_2000["problem_codes"])
        self.assertIn("trace_truncated", report_2000["problem_codes"])
        self.assertEqual(report_2000["process_quality"], "unknown")

    def test_supplemented_failed_receipt_survives_event_cap(self):
        """Codex PR#267 P1: a paired failed receipt beyond the capped window
        must still report the failure, not silently disappear."""
        from herdr.execution_trace import STORED_LIMIT
        task_cap = "task-cap-fail"
        run_cap = "run-cap-fail"
        self._insert_task(task_cap, "other-run")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_cap, json.dumps({"verification": {"failing_count": 1, "evidence_id": "ev-cap"}}), run_cap),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_cap, 10 + index, run_cap, index + 2) for index in range(STORED_LIMIT - 1)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 50000, 'trajectory', ?, 5000)",
                (task_cap, json.dumps({"verification": {"passed": True, "evidence_id": "ev-unrelated"}}), run_cap),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 50001, 'trajectory', ?, 5001)",
                (task_cap, json.dumps({"verification": {"passed": False, "evidence_id": "ev-cap"}}), run_cap),
            )
            conn.commit()
        finally:
            conn.close()
        report_cap = assess_task_run(task_cap, run_cap, db_path=self.db, budget_s=0.2)
        self.assertIn("trace_truncated", report_cap["problem_codes"])
        self.assertIn("test_anomaly", report_cap["problem_codes"])
        self.assertEqual(report_cap["process_quality"], "improvement_opportunity")

    def test_historical_receipt_does_not_leak_across_runs(self):
        from herdr.execution_trace import STORED_LIMIT

        task_id = "task-cross-leak"
        hist_run = "run-hist-leak"
        other_run = "run-other-leak"
        self._insert_task(task_id, "current-run")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"failing_count": 3, "evidence_id": "ev-cross"}}), hist_run),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_id, 10 + index, hist_run, index + 2) for index in range(STORED_LIMIT - 1)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100000, 'trajectory', ?, 5000)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-cross"}}), other_run),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100001, 'trajectory', NULL, 5001)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-cross"}})),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, hist_run, eval_id="eval-cross-leak")
        report = assess_task_run(task_id, hist_run, db_path=self.db, budget_s=0.2)
        self.assertEqual(len(report["test_anomalies"]), 1)
        self.assertEqual(report["test_anomalies"][0]["code"], "test_failed")
        self.assertIn("test_anomaly", report["problem_codes"])
        self.assertEqual(report["process_quality"], "improvement_opportunity")

    def test_historical_multiple_pairings_and_unmatched_receipts(self):
        from herdr.execution_trace import STORED_LIMIT

        task_id = "task-multi-pair"
        hist_run = "run-multi-pair"
        self._insert_task(task_id, "current-run")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"failing_count": 2, "evidence_id": "ev-pass"}}), hist_run),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 2, 'trajectory', ?, 2)",
                (task_id, json.dumps({"verification": {"failing_count": 2, "evidence_id": "ev-fail"}}), hist_run),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_id, 10 + index, hist_run, index + 3) for index in range(STORED_LIMIT - 2)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100000, 'trajectory', ?, 5000)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-pass"}}), hist_run),
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100001, 'trajectory', ?, 5001)",
                (task_id, json.dumps({"verification": {"passed": False, "evidence_id": "ev-fail"}}), hist_run),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, hist_run, eval_id="eval-multi-pair")
        report = assess_task_run(task_id, hist_run, db_path=self.db, budget_s=0.2)
        self.assertEqual(len(report["test_anomalies"]), 1)
        self.assertEqual(report["test_anomalies"][0]["code"], "test_failed")
        self.assertIn("test_anomaly", report["problem_codes"])
        self.assertEqual(report["process_quality"], "improvement_opportunity")

    def test_historical_unmatched_receipt_does_not_suppress_failure(self):
        from herdr.execution_trace import STORED_LIMIT

        task_id = "task-unmatched"
        hist_run = "run-unmatched"
        self._insert_task(task_id, "current-run")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"failing_count": 3, "evidence_id": "ev-target"}}), hist_run),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_id, 10 + index, hist_run, index + 2) for index in range(STORED_LIMIT - 1)],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100000, 'trajectory', ?, 5000)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-different"}}), hist_run),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, hist_run, eval_id="eval-unmatched")
        report = assess_task_run(task_id, hist_run, db_path=self.db, budget_s=0.2)
        self.assertEqual(len(report["test_anomalies"]), 1)
        self.assertIn("test_anomaly", report["problem_codes"])
        self.assertEqual(report["process_quality"], "improvement_opportunity")

    def test_historical_receipt_supplement_saturation_keeps_window_evidence(self):
        from herdr.execution_trace import STORED_LIMIT

        task_id = "task-saturation"
        hist_run = "run-saturation"
        self._insert_task(task_id, "current-run")
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'tests_completed', ?, 1, 'trajectory', ?, 1)",
                (task_id, json.dumps({"verification": {"failing_count": 3, "evidence_id": "ev-target"}}), hist_run),
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'task_started', '{}', ?, 'trajectory', ?, ?)",
                [(task_id, 10 + index, hist_run, index + 2) for index in range(STORED_LIMIT - 1)],
            )
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, ?, 'trajectory', ?, ?)",
                [
                    (task_id, json.dumps({"verification": {"passed": True, "evidence_id": f"dummy-{i}"}}),
                     1000 + i, hist_run, 3000 + i)
                    for i in range(STORED_LIMIT)
                ],
            )
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, 'verification_completed', ?, 100000, 'trajectory', ?, 90000)",
                (task_id, json.dumps({"verification": {"passed": True, "evidence_id": "ev-target"}}), hist_run),
            )
            conn.commit()
        finally:
            conn.close()
        self._insert_outcome(task_id, hist_run, eval_id="eval-saturation")
        report = assess_task_run(task_id, hist_run, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["test_anomalies"], [])
        self.assertNotIn("test_anomaly", report["problem_codes"])
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("trace_truncated", report["problem_codes"])

    def _saturated_history(self, task_id, *, pairs=21, corrupt=False, paired=True):
        hist_run = "historical-r3"
        self._insert_task(task_id, "current-r3")
        self._insert_outcome(task_id, hist_run, eval_id=task_id)
        rows = []
        for i in range(pairs):
            rows.append(("tests_completed", json.dumps({"verification": {
                "failing_count": 1, "evidence_id": f"z{i:03}",
            }}), i + 1, hist_run))
        rows.extend(("task_started", "{}", 100 + i, hist_run) for i in range(2000 - pairs))
        rows.extend(("verification_completed", json.dumps({"verification": {
            "passed": True, "evidence_id": f"a{i}",
        }}), 5000 + i, hist_run) for i in range(2000))
        if paired:
            rows.extend(("verification_completed", json.dumps({"verification": {
                "passed": True, "evidence_id": f"z{i:03}",
            }}), 10000 + i, hist_run) for i in range(pairs))
        if corrupt:
            rows.append(("verification_completed", "{broken", 20000, hist_run))
        # These receipts must never resolve this run's evidence.
        rows.extend(("verification_completed", json.dumps({"verification": {
            "passed": True, "evidence_id": "z000",
        }}), 30000, foreign) for foreign in ("other-r3", None))
        conn = state_db.get_db_connection(self.db)
        try:
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id) VALUES ('wf-1', ?, ?, ?, ?, 'trajectory', ?)",
                [(task_id, *row) for row in rows],
            )
            conn.commit()
        finally:
            conn.close()
        return hist_run

    def test_r3_saturated_21_pairs_do_not_confirm_unread_failure(self):
        for pairs in (20, 21):
            with self.subTest(pairs=pairs):
                task_id = f"r3-pairs-{pairs}"
                run = self._saturated_history(task_id, pairs=pairs)
                report = assess_task_run(task_id, run, db_path=self.db)
                self.assertEqual(report["test_anomalies"], [])
                self.assertEqual(report["process_quality"], "unknown")
                self.assertNotIn("stored_payload_corrupt", report["problem_codes"])

    def test_r3_saturated_budget_exhaustion_is_explicitly_unresolved(self):
        from herdr.quality_sources import load_quality_inputs

        task_id = "r3-unresolved"
        run = self._saturated_history(task_id, paired=False)
        inputs = load_quality_inputs(task_id, run, db_path=self.db, roots={}, budget_s=0.2)
        notes = [n for n in inputs["degradation"] if n["code"] == "verification_receipts_unresolved"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["details"]["unresolved_count"], 1)
        report = assess_task_run(task_id, run, db_path=self.db)
        # Twenty exact absence checks still confirm their real failures.
        self.assertEqual(len(report["test_anomalies"]), 20)
        self.assertEqual(report["process_quality"], "improvement_opportunity")

    def test_r3_saturated_corrupt_receipt_preserves_legal_events(self):
        from herdr.quality_sources import load_quality_inputs

        task_id = "r3-corrupt"
        run = self._saturated_history(task_id, pairs=1, corrupt=True, paired=False)
        inputs = load_quality_inputs(task_id, run, db_path=self.db, roots={}, budget_s=0.2)
        self.assertTrue(any(e["kind"] == "test_failed" for e in inputs["events"]))
        self.assertTrue(any(e["kind"] == "agent_started" for e in inputs["events"]))
        notes = [n for n in inputs["degradation"] if n["code"] == "stored_payload_corrupt"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["details"]["corrupt_count"], 1)
        self.assertNotIn("trajectory_unavailable", [n["code"] for n in inputs["degradation"]])
        report = assess_task_run(task_id, run, db_path=self.db)
        self.assertIn("stored_payload_corrupt", report["problem_codes"])
        self.assertEqual(len(report["test_anomalies"]), 1)
        self.assertEqual(report["trace_completeness"], "unknown")

    def test_r3_corrupt_saturated_receipt_with_successful_pair_stays_unknown(self):
        from herdr.quality_sources import load_quality_inputs

        task_id = "r3-corrupt-paired"
        run = self._saturated_history(task_id, pairs=1, corrupt=True)
        report = assess_task_run(task_id, run, db_path=self.db)
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("stored_payload_corrupt", report["problem_codes"])
        conn = state_db.get_db_connection(self.db)
        try:
            # Valid JSON non-objects share the decoder's corrupt semantics.
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id) VALUES ('wf-1', ?, 'verification_completed', ?, 40000, 'trajectory', ?)",
                [(task_id, payload, run) for payload in ('[]', 'null', '"str"')]
                + [(task_id, '{broken', foreign) for foreign in ('other-r3', None)],
            )
            conn.commit()
        finally:
            conn.close()
        inputs = load_quality_inputs(task_id, run, db_path=self.db, roots={}, budget_s=0.2)
        notes = [n for n in inputs["degradation"] if n["code"] == "stored_payload_corrupt"]
        self.assertEqual(notes[0]["details"]["corrupt_count"], 4)
        self.assertLessEqual(len(notes[0]["details"]["sample_ids"]), 8)
        self.assertTrue(any(e["kind"] == "test_passed" for e in inputs["events"]))

    def test_r3_per_evidence_receipt_cap_does_not_confirm_failure(self):
        from herdr.quality_sources import load_quality_inputs

        task_id = "r3-receipt-cap"
        run = self._saturated_history(task_id, pairs=1, paired=False)
        conn = state_db.get_db_connection(self.db)
        try:
            conn.executemany(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id) VALUES ('wf-1', ?, 'verification_completed', ?, ?, 'trajectory', ?)",
                [(task_id, json.dumps({"evidence_id": "z000"}), 10000 + i, run) for i in range(10)]
                + [(task_id, json.dumps({"verification": {"evidence_id": "z000", "passed": True}}), 20000, run)],
            )
            conn.commit()
        finally:
            conn.close()
        inputs = load_quality_inputs(task_id, run, db_path=self.db, roots={}, budget_s=0.2)
        self.assertIn("verification_receipts_unresolved", [n["code"] for n in inputs["degradation"]])
        report = assess_task_run(task_id, run, db_path=self.db)
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["process_quality"], "unknown")

    def test_r3_schema_only_receipt_is_not_a_verified_pair(self):
        task_id = "r3-schema-pair"
        run = self._saturated_history(task_id, pairs=1)
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id) VALUES ('wf-1', ?, 'verification_completed', ?, 4000, 'trajectory', ?)",
                (task_id, json.dumps({"evidence_id": "z000"}), run),
            )
            conn.commit()
        finally:
            conn.close()
        report = assess_task_run(task_id, run, db_path=self.db)
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["process_quality"], "unknown")

    def test_historical_receipt_query_sqlite_error_degrades_gracefully(self):
        task_id = "task-sqlite-err"
        hist_run = "run-sqlite-err"
        self._insert_task(task_id, "current-run")
        self._insert_outcome(task_id, hist_run, eval_id="eval-sqlite-err")

        with (
            patch("herdr.quality_sources._receipts_for_scope", side_effect=sqlite3.OperationalError("disk error")),
            patch("herdr.quality_sources.STORED_LIMIT", 0),
        ):
            report = assess_task_run(task_id, hist_run, db_path=self.db, budget_s=0.2)
            self.assertIn("trace_unavailable", report["problem_codes"])
            self.assertEqual(report["trace_completeness"], "unknown")
            self.assertEqual(report["process_quality"], "unknown")

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

    def _insert_ledger_event(self, task_id, run_id, *, event_type="verification_completed",
                             payload_json="{oops", timestamp=70, sequence=1):
        conn = state_db.get_db_connection(self.db)
        try:
            conn.execute(
                "INSERT INTO events (workflow_id, task_id, event_type, payload_json, "
                "timestamp, source, run_id, sequence) VALUES "
                "('wf-1', ?, ?, ?, ?, 'trajectory', ?, ?)",
                (task_id, event_type, payload_json, timestamp, run_id, sequence),
            )
            conn.commit()
        finally:
            conn.close()

    def test_corrupt_stored_ledger_payload_is_unknown(self):
        task_id, run_id = "task-corrupt", "run-corrupt"
        self._insert_task(task_id, run_id)
        self._insert_ledger_event(task_id, run_id)
        self._insert_outcome(task_id, run_id, eval_id="eval-corrupt")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("stored_payload_corrupt", report["problem_codes"])
        self.assertEqual(report["test_anomalies"], [])
        self.assertEqual(report["rework_count"], 0)
        self.assertEqual(report["blocked_count"], 0)

    def test_corrupt_stored_ledger_payload_is_unknown_for_historical_run(self):
        task_id = "task-histcorrupt"
        owning_run = "run-histcorrupt-own"
        historical_run = "run-histcorrupt-old"
        self._insert_task(task_id, owning_run)
        self._insert_ledger_event(task_id, historical_run, timestamp=71)
        report = assess_task_run(task_id, historical_run, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("stored_payload_corrupt", report["problem_codes"])
        self.assertIn("historical_run_trace_limited", report["problem_codes"])
        self.assertEqual(report["test_anomalies"], [])

    def test_corrupt_ledger_payload_is_recorded_for_every_projectable_type(self):
        from herdr.execution_trace import _PROJECTABLE_EVENT_TYPES

        task_id, run_id = "task-corrupt-all", "run-corrupt-all"
        self._insert_task(task_id, run_id)
        for index, event_type in enumerate(_PROJECTABLE_EVENT_TYPES):
            self._insert_ledger_event(
                task_id, run_id, event_type=event_type,
                timestamp=80 + index, sequence=index + 1,
            )
        self._insert_outcome(task_id, run_id, eval_id="eval-corrupt-all")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertEqual(report["process_quality"], "unknown")
        self.assertIn("stored_payload_corrupt", report["problem_codes"])
        self.assertEqual(report["test_anomalies"], [])

    def test_non_object_json_ledger_payload_counts_as_corrupt(self):
        task_id, run_id = "task-corrupt-forms", "run-corrupt-forms"
        self._insert_task(task_id, run_id)
        for index, payload_json in enumerate(("[1,2]", '"str"', "null")):
            self._insert_ledger_event(
                task_id, run_id, payload_json=payload_json,
                timestamp=100 + index, sequence=index + 1,
            )
        self._insert_outcome(task_id, run_id, eval_id="eval-corrupt-forms")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertIn("stored_payload_corrupt", report["problem_codes"])

    def test_blank_ledger_payload_is_not_corrupt(self):
        task_id, run_id = "task-blank-payload", "run-blank-payload"
        self._insert_task(task_id, run_id)
        self._insert_ledger_event(task_id, run_id, payload_json=None, timestamp=110, sequence=1)
        self._insert_ledger_event(task_id, run_id, payload_json="", timestamp=111, sequence=2)
        self._insert_ledger_event(task_id, run_id, payload_json="{}", timestamp=112, sequence=3)
        self._insert_outcome(task_id, run_id, eval_id="eval-blank-payload")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertNotIn("stored_payload_corrupt", report["problem_codes"])
        self.assertEqual(report["trace_completeness"], "complete")
        self.assertEqual(report["process_quality"], "acceptable")

    def test_valid_object_missing_keys_is_not_corrupt(self):
        task_id, run_id = "task-schema-gaps", "run-schema-gaps"
        self._insert_task(task_id, run_id)
        self._insert_ledger_event(
            task_id, run_id, payload_json=json.dumps({"verification": {}}),
            timestamp=120, sequence=1,
        )
        self._insert_ledger_event(
            task_id, run_id, payload_json=json.dumps({"evidence_id": "ev-loose"}),
            timestamp=121, sequence=2,
        )
        self._insert_outcome(task_id, run_id, eval_id="eval-schema-gaps")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertNotIn("stored_payload_corrupt", report["problem_codes"])

    def test_corrupt_payload_keeps_the_real_anomaly_signal(self):
        task_id, run_id = "task-corrupt-anomaly", "run-corrupt-anomaly"
        self._insert_task(task_id, run_id)
        self._insert_scoped_verification(
            task_id, run_id, passed=False, evidence="ev-a6", timestamp=130,
        )
        self._insert_ledger_event(
            task_id, run_id, event_type="task_artifact_checkpoint",
            timestamp=131, sequence=2,
        )
        self._insert_outcome(task_id, run_id, eval_id="eval-corrupt-anomaly")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["correctness"], "success")
        self.assertEqual(report["trace_completeness"], "unknown")
        self.assertEqual(report["process_quality"], "improvement_opportunity")
        self.assertIn("stored_payload_corrupt", report["problem_codes"])
        self.assertIn("test_anomaly", report["problem_codes"])
        self.assertEqual(len(report["test_anomalies"]), 1)
        self.assertEqual(report["rework_count"], 0)
        self.assertEqual(report["blocked_count"], 0)

    def test_clean_ledger_run_stays_acceptable(self):
        task_id, run_id = "task-clean-ctrl", "run-clean-ctrl"
        self._insert_task(task_id, run_id)
        self._insert_scoped_verification(
            task_id, run_id, passed=True, evidence="ev-clean", timestamp=140,
        )
        self._insert_outcome(task_id, run_id, eval_id="eval-clean-ctrl")
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(report["trace_completeness"], "complete")
        self.assertEqual(report["process_quality"], "acceptable")
        self.assertNotIn("stored_payload_corrupt", report["problem_codes"])

    def test_corrupt_payload_assessment_does_not_change_the_database(self):
        task_id, run_id = "task-corrupt-readonly", "run-corrupt-readonly"
        self._insert_task(task_id, run_id)
        self._insert_ledger_event(task_id, run_id, timestamp=150)
        self._insert_outcome(task_id, run_id, eval_id="eval-corrupt-readonly")
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        report = assess_task_run(task_id, run_id, db_path=self.db, budget_s=0.2)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertNotIn("assessment_unavailable", report["problem_codes"])


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
