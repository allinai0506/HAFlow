"""A predecessor's replacement must be visible from either pointer.

Regression for wf-haflow-1021-01. The craft node livelocked for 17.5 minutes:

    op 211810 predecessors = [craft-auto]        (frozen before craft-auto-r2 existed)
    planner lineage leaf    = craft-auto-r2      (so it dispatches r3 superseding r2)
    launch validator        = supersedes must be in op.predecessors -> permanent reject

`lineage_redispatch_candidates` links a lineage from BOTH pointers, so the
planner correctly moved on to r2. `predecessors_current` only read the back
pointer `superseded_by`, and r2's launch had been refused fail-fast
(baseline_stale) long before the backfill at the end of a successful launch, so
`craft-auto.superseded_by` stayed None. The staleness check therefore reported
the predecessor as still current, the operation was never superseded, and the
self-heal (`dispatch_predecessor_changed`) could not fire.
"""

import unittest

from herdr.node_dispatch import predecessors_current

WORKFLOW_ID = "wf-haflow-1021-01"
EXECUTION_ID = "wf-haflow-1021-01"
ROOT = "wf-haflow-1021-01-craft-auto"
ROOT_RUN = "run_26c412e05b694524915cfbd2a2e35156"
R2 = "wf-haflow-1021-01-craft-auto-r2"
R2_RUN = "run_eec9cd6d76e346e29921a8f0e2380866"

WORKFLOW = {"workflow_id": WORKFLOW_ID, "execution_id": EXECUTION_ID, "status": "running"}


def operation(*, predecessors, op_id=211810):
    return {
        "id": op_id,
        "workflow_id": WORKFLOW_ID,
        "status": "waiting_human",
        "started": 0,
        "attempts": 3,
        "detail": {},
        "payload": {"node_id": "craft", "predecessors": predecessors},
    }


def task(task_id, *, run_id, status="superseded", supersedes=None, superseded_by=None,
         replacement_pending=True, dispatch_operation_id=None, launch_intent_id=None):
    return {
        "task_id": task_id,
        "workflow_id": WORKFLOW_ID,
        "node": "craft",
        "stage": "craft",
        "run_id": run_id,
        "execution_id": EXECUTION_ID,
        "status": status,
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "replacement_pending": replacement_pending,
        "dispatch_operation_id": dispatch_operation_id,
        "launch_intent_id": launch_intent_id,
    }


ROOT_TASK = task(ROOT, run_id=ROOT_RUN)
R2_TASK = task(R2, run_id=R2_RUN, supersedes=ROOT)
PREDECESSORS = [{"task_id": ROOT, "run_id": ROOT_RUN}]


class PredecessorLineageConsistencyTest(unittest.TestCase):
    def test_forward_pointer_alone_makes_the_predecessor_stale(self):
        """The production shape: superseded_by was never backfilled."""
        self.assertIsNone(ROOT_TASK["superseded_by"])
        self.assertFalse(predecessors_current(
            operation(predecessors=PREDECESSORS), WORKFLOW, [ROOT_TASK, R2_TASK]))

    def test_back_pointer_alone_still_makes_the_predecessor_stale(self):
        linked = dict(ROOT_TASK, superseded_by=R2)
        self.assertFalse(predecessors_current(
            operation(predecessors=PREDECESSORS), WORKFLOW, [linked, R2_TASK]))

    def test_predecessor_awaiting_its_first_replacement_stays_current(self):
        """Nothing has claimed it yet: this operation is still the responsible one."""
        self.assertTrue(predecessors_current(
            operation(predecessors=PREDECESSORS), WORKFLOW, [ROOT_TASK]))

    def test_replacement_registered_by_this_operation_keeps_it_current(self):
        live = task(R2, run_id=R2_RUN, supersedes=ROOT, status="working",
                    replacement_pending=None, dispatch_operation_id=211810,
                    launch_intent_id="intent-1")
        self.assertTrue(predecessors_current(
            operation(predecessors=PREDECESSORS), WORKFLOW, [ROOT_TASK, live]))

    def test_a_claim_from_another_node_or_workflow_is_not_evidence(self):
        for foreign in (
            dict(R2_TASK, node="cleanroom_test", stage="cleanroom_test"),
            dict(R2_TASK, workflow_id="wf-other"),
            dict(R2_TASK, execution_id="execution-other"),
        ):
            with self.subTest(foreign=foreign.get("node") or foreign.get("workflow_id")
                              or foreign.get("execution_id")):
                self.assertTrue(predecessors_current(
                    operation(predecessors=PREDECESSORS), WORKFLOW, [ROOT_TASK, foreign]))

    def test_run_identity_mismatch_is_unchanged(self):
        stale_run = [{"task_id": ROOT, "run_id": "run_something_else"}]
        self.assertFalse(predecessors_current(
            operation(predecessors=stale_run), WORKFLOW, [ROOT_TASK, R2_TASK]))

    def test_no_predecessors_recorded_is_vacuously_current(self):
        self.assertTrue(predecessors_current(
            operation(predecessors=None), WORKFLOW, [ROOT_TASK, R2_TASK]))


if __name__ == "__main__":
    unittest.main()
