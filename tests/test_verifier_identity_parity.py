"""Protections keyed on verifier identity must fire for template-declared gates.

Regression for wf-haflow-1021-01: code-craft-v1 names its gates cleanroom_test /
cleanroom_review. Every check that identified a verifier by the literal ids
{'test', 'review'} silently skipped them, so the cleanroom stations were treated
as ordinary producers: they could be marked writable, their receipt-v1 business
gate could be force-passed on a stage verdict alone, and wrapup's business gate
never listed them as dependencies.
"""

import unittest
from pathlib import Path

from herdr.business_gate import business_gate_blockers
from herdr.task_checkpoint import checkpoint_instruction_block, reject_verdict_only_override
from herdr.task_delivery import writable_task
from herdr.workflow import load_template

TEMPLATES = Path(__file__).resolve().parents[1] / "workflow_templates"


def template(name):
    return load_template(str(TEMPLATES / f"{name}.yaml"))


def cleanroom_task(**overrides):
    task = {
        "task_id": "wf-cleanroom_test-auto",
        "workflow_id": "wf",
        "node": "cleanroom_test",
        "task_type": "test",
        "integration_mode": "none",
        "completion_protocol": "receipt-v1",
        "status": "working",
    }
    task.update(overrides)
    return task


class WritableTaskTest(unittest.TestCase):
    def test_verifier_is_never_a_writable_task(self):
        # integration_mode='git' is what makes the verifier clause observable:
        # the real cleanroom tasks use 'none' and fail the first condition anyway.
        self.assertFalse(writable_task(cleanroom_task(integration_mode="git")))
        self.assertFalse(writable_task(cleanroom_task(
            integration_mode="git", node="cleanroom_review")))

    def test_legacy_verifier_ids_stay_read_only(self):
        for node_id in ("test", "review"):
            with self.subTest(node_id=node_id):
                self.assertFalse(writable_task(
                    {"node": node_id, "integration_mode": "git", "task_type": "test"}))

    def test_producer_task_stays_writable(self):
        self.assertTrue(writable_task({
            "node": "craft", "task_type": "feat", "integration_mode": "git"}))


class VerdictOnlyOverrideTest(unittest.TestCase):
    def test_verifier_receipt_gate_cannot_be_force_passed(self):
        with self.assertRaises(ValueError):
            reject_verdict_only_override(cleanroom_task())
        with self.assertRaises(ValueError):
            reject_verdict_only_override(cleanroom_task(node="cleanroom_review"))

    def test_legacy_verifier_ids_still_reject_the_override(self):
        for node_id in ("test", "review"):
            with self.subTest(node_id=node_id):
                with self.assertRaises(ValueError):
                    reject_verdict_only_override(
                        {"node": node_id, "completion_protocol": "receipt-v1"})

    def test_producer_and_legacy_tasks_are_unaffected(self):
        reject_verdict_only_override(
            {"node": "craft", "task_type": "feat", "completion_protocol": "receipt-v1"})
        reject_verdict_only_override(cleanroom_task(completion_protocol=None))


class BusinessGateDependencyTest(unittest.TestCase):
    def _wrapup_pending(self, config):
        workflow = {"workflow_id": "wf", "candidate_sha": "a" * 40}
        tasks = [{"task_id": "wrap-1", "workflow_id": "wf", "node": "wrapup",
                  "status": "working", "completion_protocol": "receipt-v1"}]
        return business_gate_blockers(None, workflow, config, tasks, node_id="wrapup")

    def test_declared_verifiers_are_gate_dependencies(self):
        self.assertEqual(self._wrapup_pending(template("code-craft-v1")),
                         ["<missing:cleanroom_review>", "<missing:cleanroom_test>"])

    def test_legacy_template_keeps_its_gate_dependencies(self):
        self.assertEqual(self._wrapup_pending(template("software-development-v1")),
                         ["<missing:review>", "<missing:test>"])

    def test_present_verifiers_join_the_gate_cohort(self):
        config = template("code-craft-v1")
        workflow = {"workflow_id": "wf", "candidate_sha": "a" * 40}
        tasks = [
            {"task_id": "wrap-1", "workflow_id": "wf", "node": "wrapup",
             "status": "working", "completion_protocol": "receipt-v1"},
            cleanroom_task(),
            cleanroom_task(task_id="wf-cleanroom_review-auto", node="cleanroom_review"),
        ]
        # No store means the receipt cohort cannot be re-read, so every receipt-v1
        # gate is reported. Before the fix the cohort was empty and this was [];
        # the point is that the cleanroom gates are now in the cohort at all.
        self.assertEqual(
            business_gate_blockers(None, workflow, config, tasks, node_id="wrapup"),
            ["wf-cleanroom_review-auto", "wf-cleanroom_test-auto"],
        )


class CheckpointInstructionTest(unittest.TestCase):
    """A verifier must be told to bind its acceptance record to the candidate."""

    def test_declared_verifier_gets_the_acceptance_instruction(self):
        block = checkpoint_instruction_block(
            cleanroom_task(run_id="run-1", candidate_sha="a" * 40), "1")
        self.assertIn("acceptance-record", block)

    def test_legacy_verifier_ids_keep_the_instruction(self):
        for node_id in ("test", "review"):
            with self.subTest(node_id=node_id):
                block = checkpoint_instruction_block(
                    {"task_id": "t", "run_id": "run-1", "node": node_id,
                     "candidate_sha": "a" * 40}, "1")
                self.assertIn("acceptance-record", block)

    def test_producers_and_candidate_less_tasks_get_no_acceptance_instruction(self):
        for task in (
            {"task_id": "t", "run_id": "run-1", "node": "craft",
             "task_type": "feat", "candidate_sha": "a" * 40},
            cleanroom_task(run_id="run-1", candidate_sha=None),
        ):
            with self.subTest(task=task.get("node")):
                self.assertNotIn("acceptance-record",
                                 checkpoint_instruction_block(task, "1"))


if __name__ == "__main__":
    unittest.main()
