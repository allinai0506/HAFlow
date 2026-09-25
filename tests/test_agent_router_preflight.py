"""Router preflight hardening tests.

Regression cover for the wf-nexusarchive-0917-01 incident (2026-09-17):
the test node auto-dispatched to `pi` even though pi was degraded
(AUTH_REQUIRED in the workflow's unhealthy_agents map), producing an
instant empty agent_done -> DECISION TIMEOUT -> failed -> 28 min of
human relaunch latency before test-auto-r2.

Contract under test (herdr/agent_router.choose_agent, requested="auto"):
1. Agents listed in the workflow record's `unhealthy_agents` are NEVER
   auto-selected -- even on cold start (empty healthy snapshot) and even
   when node preferences rank them first.
2. A stale preflight snapshot (`preflight_checked_at` older than
   HERDR_PREFLIGHT_TTL, default 1800s) must not restrict selection to a
   possibly-dead healthy set; the router falls back to
   allowed - disabled - unhealthy instead of trusting inclusion.
3. Fresh snapshots (and legacy records without a timestamp) keep the
   existing behavior, including the single-agent graceful fallback.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from herdr import agent_router
from herdr.state_store import get_state_store


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class PreflightHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-router-preflight-")
        self.tmp_path = Path(self.tmp.name)
        self.pools_file = self.tmp_path / "agent-pools.json"
        self.reservations_file = self.tmp_path / "agent-reservations.json"
        self.lock_file = self.tmp_path / "agent-router.lock"
        self.db_path = self.tmp_path / "state.db"
        self.store = get_state_store(self.db_path)
        self.wf_id = "wf-preflight-hardening"
        self.patchers = [
            patch("herdr.agent_router._get_store", return_value=self.store),
            patch("herdr.agent_router.POOLS_FILE", self.pools_file),
            patch("herdr.agent_router.RESERVATIONS_FILE", self.reservations_file),
            patch("herdr.agent_router.ROUTER_LOCK_FILE", self.lock_file),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _save_workflow(self, healthy, unhealthy, checked_at=None):
        record = {
            "workflow_id": self.wf_id,
            "project_id": "test-proj",
            "status": "running",
            "healthy_agents": list(healthy),
            "unhealthy_agents": dict(unhealthy),
        }
        if checked_at is not None:
            record["preflight_checked_at"] = checked_at
        self.store.save_workflow(record)

    def _node_cfg(self, node_id, preferred, exclude=None):
        policy = {"preferred": list(preferred)}
        if exclude:
            policy["exclude_stage_agents"] = list(exclude)
        return {"nodes": [{"id": node_id, "agent_policy": policy}]}

    def test_unhealthy_never_auto_selected_on_cold_start(self):
        self._save_workflow(
            healthy=[],
            unhealthy={"pi": "AUTH_REQUIRED", "codex": "TOKEN_EXHAUSTED"},
        )
        with patch(
            "herdr.agent_router.workflow_config_for",
            return_value=self._node_cfg("test", ["pi", "codex", "claude"]),
        ):
            chosen = agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertNotIn(chosen, {"pi", "codex"})

    def test_unhealthy_excluded_despite_preference_and_health(self):
        self._save_workflow(
            healthy=["pi", "claude"],
            unhealthy={"pi": "AUTH_REQUIRED"},
            checked_at=_iso(datetime.now()),
        )
        with patch(
            "herdr.agent_router.workflow_config_for",
            return_value=self._node_cfg("test", ["pi", "claude"]),
        ):
            chosen = agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertEqual(chosen, "claude")

    def test_stale_snapshot_does_not_force_dead_set(self):
        self.store.save_task({
            "task_id": "task-impl-1",
            "workflow_id": self.wf_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })
        self._save_workflow(
            healthy=["codex"],
            unhealthy={},
            checked_at=_iso(datetime.now() - timedelta(seconds=7200)),
        )
        with patch(
            "herdr.agent_router.workflow_config_for",
            return_value=self._node_cfg("test", ["codex", "claude"], exclude=["implementation"]),
        ):
            chosen = agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertNotEqual(chosen, "codex")

    def test_fresh_snapshot_keeps_single_agent_fallback(self):
        # FR-6.1 breaking change: fresh snapshot + single-agent pool fully
        # excluded must fail-closed (no silent fallback to codex).
        self.store.save_task({
            "task_id": "task-impl-1",
            "workflow_id": self.wf_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })
        self._save_workflow(
            healthy=["codex"],
            unhealthy={},
            checked_at=_iso(datetime.now()),
        )
        with patch(
            "herdr.agent_router.workflow_config_for",
            return_value=self._node_cfg("test", ["codex"], exclude=["implementation"]),
        ), self.assertRaises(RuntimeError) as ctx:
            agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertIn("fail-closed", str(ctx.exception).lower())

    def test_missing_timestamp_treated_as_fresh(self):
        # FR-6.1 breaking change: missing timestamp is fresh, single-agent
        # fully excluded -> fail-closed reject (not fallback).
        self.store.save_task({
            "task_id": "task-impl-1",
            "workflow_id": self.wf_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })
        self._save_workflow(healthy=["codex"], unhealthy={})
        with patch(
            "herdr.agent_router.workflow_config_for",
            return_value=self._node_cfg("test", ["codex"], exclude=["implementation"]),
        ), self.assertRaises(RuntimeError) as ctx:
            agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertIn("fail-closed", str(ctx.exception).lower())

    def test_ttl_env_override_controls_freshness(self):
        self.store.save_task({
            "task_id": "task-impl-1",
            "workflow_id": self.wf_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })
        self._save_workflow(
            healthy=["codex"],
            unhealthy={},
            checked_at=_iso(datetime.now() - timedelta(seconds=1800)),
        )
        cfg = self._node_cfg("test", ["codex", "claude"], exclude=["implementation"])
        with patch(
            "herdr.agent_router.workflow_config_for", return_value=cfg
        ), patch.dict("os.environ", {"HERDR_PREFLIGHT_TTL": "3600"}), \
                self.assertRaises(RuntimeError):
            # FR-6.1 breaking change: fresh + single healthy fully excluded
            # -> fail-closed, not fallback to codex.
            agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        with patch(
            "herdr.agent_router.workflow_config_for", return_value=cfg
        ), patch.dict("os.environ", {"HERDR_PREFLIGHT_TTL": "600"}):
            self.assertNotEqual(
                agent_router.choose_agent(self.wf_id, "test", "test", requested="auto"),
                "codex",
            )

    def test_explicit_request_of_unhealthy_still_raises(self):
        self._save_workflow(
            healthy=["claude"],
            unhealthy={"pi": "AUTH_REQUIRED"},
            checked_at=_iso(datetime.now()),
        )
        with patch(
            "herdr.agent_router.workflow_config_for",
            return_value=self._node_cfg("test", ["pi", "claude"]),
        ), self.assertRaises(RuntimeError) as ctx:
            agent_router.choose_agent(self.wf_id, "test", "test", requested="pi")
        self.assertIn("preflight", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
