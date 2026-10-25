"""Orphan-pane janitor regressions (§197 leftover ②).

wf-haflow-1024-02: the watchdog killed a task whose agent process kept
running (a DB transition is not process termination), and the pane was only
*marked* ``pane_lifecycle=orphaned`` — nobody closes it, so an orphaned agent
can grind on unbounded tokens with nobody consuming its output.

INV-SENTINEL-06: an archived task's orphaned pane gets a bounded janitor
lifecycle — after a grace window a still-working agent is interrupted once
(cooperative ctrl-c); after a close grace the pane goes through the existing
guarded reap (identity proven or missing; anything else is retained), and the
clone's unclaimed commits are recorded as a durable evidence event first.
Never writes task status, never force-kills.
"""

from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

from herdr.liveness import select_orphaned_pane_tasks
from herdr.state_store import SQLiteStateStore

NOW = 100_000.0


def _task(task_id="wf-x-impl-auto", *, status="failed", pane="w1:p1",
          lifecycle="orphaned", updated=NOW - 10_000):
    return {
        "task_id": task_id,
        "workflow_id": "wf-x",
        "node": "implementation",
        "stage": "implementation",
        "status": status,
        "pane_id": pane,
        "pane_lifecycle": lifecycle,
        "clone_path": "/tmp/does-not-matter",
        "updated_at": updated,
        "version": 3,
    }


class SelectOrphanedPaneTasksTest(unittest.TestCase):
    def test_selects_archived_orphaned_pane_past_grace(self):
        chosen = select_orphaned_pane_tasks([_task()], NOW, grace_seconds=1800)
        self.assertEqual([t["task_id"] for t in chosen], ["wf-x-impl-auto"])

    def test_within_grace_not_selected(self):
        recent = _task(updated=NOW - 600)
        self.assertEqual(select_orphaned_pane_tasks([recent], NOW, grace_seconds=1800), [])

    def test_non_archived_or_released_or_paneless_not_selected(self):
        self.assertEqual(select_orphaned_pane_tasks(
            [_task(status="working")], NOW, grace_seconds=0), [])
        self.assertEqual(select_orphaned_pane_tasks(
            [_task(lifecycle="released")], NOW, grace_seconds=0), [])
        self.assertEqual(select_orphaned_pane_tasks(
            [_task(pane="")], NOW, grace_seconds=0), [])


class OrphanPaneJanitorTest(unittest.TestCase):
    def setUp(self):
        self.sentinel = importlib.import_module("services.herdr-sentinel")
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(self.tmp.name + "/state.db")
        self.store.save_workflow({"workflow_id": "wf-x", "status": "running"})
        self.store.save_task({
            "task_id": "wf-x-impl-auto",
            "workflow_id": "wf-x",
            "node": "implementation",
            "stage": "implementation",
            "agent": "agy",
            "status": "failed",
            "pane_id": "w1:p1",
            "pane_source": "dynamic",
            "clone_path": str(self.tmp.name) + "/clone",
            "created_at": 1.0,
        })
        # Mark the orphan exactly like kernel.transition_task does.
        task = self.store.get_task("wf-x-impl-auto")
        task = {**task, "pane_lifecycle": "orphaned", "updated_at": NOW - 10_000}
        self.task = task

        self.events = []
        self.calls = {"interrupt": 0, "probe": 0, "close": 0, "evidence": 0}

        def fake_interrupt(pane_id):
            self.calls["interrupt"] += 1
            return True

        def fake_probe(scope):
            self.calls["probe"] += 1
            return self.probe_verdict

        def fake_close(pane_id):
            self.calls["close"] += 1
            return True

        def fake_evidence(task):
            self.calls["evidence"] += 1
            return {"branch": "agent/agy/feat-x", "commits": ["abc123 fix things"]}

        self.probe_verdict = {"status": "available", "reason": "identity_match"}
        self.agent_status_value = "working"
        self.state = {"orphan_panes": {}}

        patches = [
            patch.object(self.sentinel, "_get_store", lambda: self.store),
            patch.object(self.sentinel, "agent_status", lambda pane: self.agent_status_value),
            patch.object(self.sentinel, "_orphan_interrupt", fake_interrupt),
            patch.object(self.sentinel, "_orphan_probe", fake_probe),
            patch.object(self.sentinel, "_orphan_close", fake_close),
            patch.object(self.sentinel, "_unclaimed_commit_evidence", fake_evidence),
            patch.object(
                self.sentinel, "_record_sentinel_event",
                lambda store, task, etype, payload: self.events.append((etype, payload)),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _sweep(self, now, tasks=None):
        return self.sentinel.check_orphan_panes(
            tasks if tasks is not None else [self.task], self.state, now=now
        )

    def test_grace_window_noop(self):
        self._sweep(NOW - 9_000)  # orphan age 1000s < 1800s grace
        self.assertEqual(self.calls, {"interrupt": 0, "probe": 0, "close": 0, "evidence": 0})

    def test_interrupt_once_after_grace(self):
        self._sweep(NOW)
        self.assertEqual(self.calls["interrupt"], 1)
        self.assertEqual([e[0] for e in self.events], ["orphan_pane_interrupted"])
        self._sweep(NOW + 60)
        self.assertEqual(self.calls["interrupt"], 1, "must not re-interrupt")

    def test_reap_after_close_grace_with_evidence_first(self):
        self._sweep(NOW)
        self.probe_verdict = {"status": "available", "reason": "identity_match"}
        self._sweep(NOW + 10)  # inside close grace -> no reap yet
        self.assertEqual(self.calls["close"], 0)
        self._sweep(NOW + 700)  # past close grace -> evidence then guarded reap
        self.assertEqual(self.calls["evidence"], 1)
        self.assertEqual(self.calls["close"], 1)
        self.assertEqual(
            [e[0] for e in self.events],
            ["orphan_pane_interrupted", "orphan_pane_unclaimed_commits", "orphan_pane_reap"],
        )
        # Real reap wrote through the real store: pane released.
        self.assertIsNone(self.store.get_task("wf-x-impl-auto").get("pane_id"))
        # Done episodes never retry.
        self._sweep(NOW + 800)
        self.assertEqual(self.calls["close"], 1)

    def test_missing_pane_releases_metadata_without_close(self):
        self._sweep(NOW)
        self.probe_verdict = {"status": "unavailable", "reason": "pane_missing"}
        self._sweep(NOW + 700)
        self.assertEqual(self.calls["close"], 0, "missing pane needs no close transport")
        self.assertIsNone(self.store.get_task("wf-x-impl-auto").get("pane_id"))

    def test_retained_reap_not_retried(self):
        self._sweep(NOW)
        self.probe_verdict = {"status": "unknown", "reason": "agent_identity_mismatch"}
        self._sweep(NOW + 700)
        self.assertEqual(self.calls["close"], 0)
        reap_events = [e for e in self.events if e[0] == "orphan_pane_reap"]
        self.assertEqual(len(reap_events), 1)
        self.assertIn("retained", str(reap_events[0][1]))
        self._sweep(NOW + 1_400)
        self.assertEqual(self.calls["evidence"], 1, "no repeat reap attempts")

    def test_dead_agent_clears_episode(self):
        self.agent_status_value = None
        self._sweep(NOW)
        self._sweep(NOW + 60)
        # Fresh orphan with no agent behind it never gets interrupted.
        fresh = _task(task_id="wf-x-impl-auto2")
        self.state = {"orphan_panes": {}}
        self.agent_status_value = "idle"
        self._sweep(NOW + 120, tasks=[fresh])
        self.assertEqual(self.calls["interrupt"], 0)

    def test_kill_switch_disables_janitor(self):
        with patch.dict(os.environ, {"HERDR_ORPHAN_PANE_JANITOR": "0"}):
            self._sweep(NOW)
        self.assertEqual(self.calls["interrupt"], 0)


if __name__ == "__main__":
    unittest.main()
