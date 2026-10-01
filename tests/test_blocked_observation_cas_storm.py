"""A stale blocked-observation sample must stop the CAS retry storm.

Observed on ``wf-project-0929-01`` / ``impl-t6-mock-retire``: Sentinel recorded
a ``blocked_marker_observed`` sample at version 3, ``herdr-task set-status``
bumped the task to version 5, and the Controller then re-attempted the same
unwinnable compare-and-set 238 times over 25 minutes — 238 identical
``blocked_observation_cas_rejected`` rows in the events ledger, zero progress.

The pre-check itself is pure and lives in ``herdr.completion``; these tests
cover both the predicate and the Controller wiring.
"""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
if str(HERDR_ROOT) not in sys.path:
    sys.path.insert(0, str(HERDR_ROOT))

from herdr.completion import observation_is_current
from herdr import kernel


class ObservationIsCurrentTest(unittest.TestCase):
    """The pure pre-check: can this sample still win its CAS?"""

    def test_matching_version_and_status_is_current(self):
        self.assertTrue(
            observation_is_current(
                {"observed_version": 5, "observed_status": "working"},
                authoritative_status="working",
                authoritative_version=5,
            )
        )

    def test_version_bumped_makes_sample_stale(self):
        """The regression: sample predates a set-status / reopen / other write."""
        self.assertFalse(
            observation_is_current(
                {"observed_version": 3, "observed_status": "working"},
                authoritative_status="working",
                authoritative_version=5,
            )
        )

    def test_status_moved_makes_sample_stale(self):
        self.assertFalse(
            observation_is_current(
                {"observed_version": 5, "observed_status": "working"},
                authoritative_status="rework",
                authoritative_version=5,
            )
        )

    def test_version_only_mismatch_is_enough(self):
        self.assertFalse(
            observation_is_current(
                {"observed_version": 3},
                authoritative_status="working",
                authoritative_version=5,
            )
        )

    def test_missing_observation_is_never_current(self):
        for bad in (None, "not-a-dict", 42, []):
            self.assertFalse(
                observation_is_current(
                    bad,
                    authoritative_status="working",
                    authoritative_version=5,
                ),
                bad,
            )

    def test_unbindable_sample_defers_to_the_authoritative_cas(self):
        """No version on either side must not silently veto a fresh sample."""
        self.assertTrue(
            observation_is_current(
                {"observed_status": "working"},
                authoritative_status="working",
                authoritative_version=5,
            )
        )
        self.assertTrue(
            observation_is_current(
                {"observed_version": 5},
                authoritative_status=None,
                authoritative_version=None,
            )
        )

    def test_absent_observed_status_defers_to_the_authoritative_cas(self):
        self.assertTrue(
            observation_is_current(
                {"observed_version": 5},
                authoritative_status="working",
                authoritative_version=5,
            )
        )

    def test_garbage_version_fails_closed(self):
        for bad in ("v5", "", object()):
            self.assertFalse(
                observation_is_current(
                    {"observed_version": bad},
                    authoritative_status="working",
                    authoritative_version=5,
                ),
                bad,
            )

    def test_numeric_string_version_still_binds(self):
        self.assertTrue(
            observation_is_current(
                {"observed_version": "5"},
                authoritative_status="working",
                authoritative_version=5,
            )
        )


class _FakeStore:
    """Records the ledger writes so storm growth is observable."""

    def __init__(self, samples):
        self.samples = samples
        self.recorded = []

    def list_events(self, task_id=None, event_type=None, limit=None, desc=False):
        if event_type != "blocked_marker_observed":
            return []
        sample = self.samples.get(task_id)
        if not sample:
            return []
        return [{"payload": dict(sample)}]

    def record_event(self, event_type, payload, **kwargs):
        self.recorded.append((event_type, dict(payload)))
        return True


class ControllerBlockedObservationTests(unittest.TestCase):
    """process_blocked_observations must not retry an unwinnable CAS."""

    def setUp(self):
        self.controller = importlib.import_module("services.herdr-controller")
        for name in (
            "_blocked_observation_stale",
            "_blocked_observation_rejected",
        ):
            getattr(self.controller, name).clear()
            self.addCleanup(getattr(self.controller, name).clear)
        os.environ["HERDR_CONTROLLER_TEST"] = "1"
        self.addCleanup(os.environ.pop, "HERDR_CONTROLLER_TEST", None)

    def _task(self, version=5, status="working"):
        return {
            "task_id": "impl-t6-mock-retire",
            "workflow_id": "wf-project-0929-01",
            "node": "implementation",
            "stage": "implementation",
            "status": status,
            "version": version,
            "pane_id": "w13:p1",
        }

    def _sweep(self, task, store, transition_result):
        # ``kernel`` is imported inside the function body, so patch the real
        # module attribute rather than a controller-level alias.
        with patch.object(self.controller, "load_tasks", return_value=[task]), \
             patch.object(self.controller, "_get_store", return_value=store), \
             patch.object(
                 kernel, "transition_task",
                 return_value=transition_result,
             ) as transition, \
             patch.object(self.controller, "enqueue_coordinator_event") as enqueue:
            processed = self.controller.process_blocked_observations()
        return processed, transition, enqueue

    def test_stale_sample_is_never_attempted(self):
        task = self._task(version=5)
        store = _FakeStore({"impl-t6-mock-retire": {
            "observed_version": 3, "observed_status": "working",
        }})
        for _ in range(50):
            processed, transition, enqueue = self._sweep(
                task, store, {"accepted": False, "reason": "cas_mismatch"}
            )
            self.assertEqual(processed, 0)
            transition.assert_not_called()
            enqueue.assert_not_called()
        self.assertEqual(store.recorded, [], "a stale sample must not touch the ledger")

    def test_same_stale_sample_logs_once_until_sample_changes(self):
        task = self._task(version=5)
        store = _FakeStore({"impl-t6-mock-retire": {
            "observed_version": 3, "observed_status": "working",
        }})
        with patch("builtins.print") as output:
            for _ in range(50):
                self._sweep(task, store, {"accepted": False})
            self.assertEqual(output.call_count, 1)
            store.samples[task["task_id"]]["observed_version"] = 4
            for _ in range(50):
                self._sweep(task, store, {"accepted": False})
            self.assertEqual(output.call_count, 2)
        self.assertEqual(store.recorded, [])

    def test_fresh_sample_transitions_to_blocked(self):
        task = self._task(version=5)
        store = _FakeStore({"impl-t6-mock-retire": {
            "observed_version": 5, "observed_status": "working",
        }})
        processed, transition, enqueue = self._sweep(
            task, store, {"accepted": True, "task": dict(task, status="blocked")}
        )
        self.assertEqual(processed, 1)
        transition.assert_called_once()
        self.assertEqual(transition.call_args.kwargs["to_status"], "blocked")
        self.assertEqual(transition.call_args.kwargs["expected_version"], 5)
        enqueue.assert_called_once()

    def test_unexpected_rejection_is_recorded_once_per_sample(self):
        task = self._task(version=5)
        store = _FakeStore({"impl-t6-mock-retire": {
            "observed_version": 5, "observed_status": "working",
        }})
        for _ in range(50):
            self._sweep(task, store, {"accepted": False, "reason": "worker_busy"})
        rejects = [e for e in store.recorded if e[0] == "blocked_observation_cas_rejected"]
        self.assertEqual(len(rejects), 1, "one sample must produce one ledger row")
        self.assertEqual(rejects[0][1]["reason"], "worker_busy")
        self.assertEqual(rejects[0][1]["authoritative_version"], 5)

    def test_a_new_sample_retries_and_is_recorded_again(self):
        """A fresh sample after a rejection must not be muted by the dedup."""
        task = self._task(version=6)
        store = _FakeStore({"impl-t6-mock-retire": {
            "observed_version": 6, "observed_status": "working",
        }})
        for _ in range(5):
            self._sweep(task, store, {"accepted": False, "reason": "worker_busy"})
        rejects = [e for e in store.recorded if e[0] == "blocked_observation_cas_rejected"]
        self.assertEqual(len(rejects), 1)
        self.assertEqual(rejects[0][1]["expected_version"], 6)

    def test_recovered_task_releases_its_dedup_state(self):
        task = self._task(version=5)
        store = _FakeStore({"impl-t6-mock-retire": {
            "observed_version": 5, "observed_status": "working",
        }})
        self._sweep(task, store, {"accepted": False, "reason": "worker_busy"})
        self.assertIn("impl-t6-mock-retire", self.controller._blocked_observation_rejected)
        done_task = self._task(version=9, status="completed")
        self._sweep(done_task, store, {"accepted": False, "reason": "x"})
        self.assertNotIn(
            "impl-t6-mock-retire", self.controller._blocked_observation_rejected
        )
        self.assertNotIn(
            "impl-t6-mock-retire", self.controller._blocked_observation_stale
        )

    def test_stale_to_fresh_transition_is_announced_once(self):
        """Reaching a fresh sample must clear the stale latch, not re-log it."""
        task = self._task(version=5)
        store = _FakeStore({"impl-t6-mock-retire": {
            "observed_version": 5, "observed_status": "working",
        }})
        self._sweep(task, store, {"accepted": False, "reason": "worker_busy"})
        store.samples["impl-t6-mock-retire"] = {
            "observed_version": 6, "observed_status": "working",
        }
        moved = self._task(version=6)
        for _ in range(5):
            self._sweep(moved, store, {"accepted": True, "task": moved})
        self.assertNotIn("impl-t6-mock-retire", self.controller._blocked_observation_stale)


if __name__ == "__main__":
    unittest.main()
