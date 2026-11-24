import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import agent_router
from herdr.agent_router import RouterIsolationRejection, choose_agent
from herdr.state_store import SQLiteStateStore


class TestAgentRouterSoftDegrade(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.pools_file = self.root / "pools.json"
        self.reservations_file = self.root / "reservations.json"
        self.lock_file = self.root / "router.lock"

        # Setup pool with only codex
        self.pools_file.write_text(
            json.dumps({
                "projects": {
                    "proj-1": {
                        "allowed_agents": ["codex"],
                        "disabled_agents": [],
                        "stage_preferences": {},
                        "task_type_preferences": {},
                    }
                }
            }),
            encoding="utf-8",
        )

        # Setup workflow with implementation stage having used codex
        self.workflow_id = "wf-soft-degrade"
        self.store.save_workflow({
            "workflow_id": self.workflow_id,
            "project_id": "proj-1",
            "status": "running",
            "execution": {"mode": "git"},
        })
        self.store.save_task({
            "task_id": "t-impl",
            "workflow_id": self.workflow_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_strict_isolation_fails_closed_without_soft_degrade(self):
        workflow_config = {
            "nodes": [
                {
                    "id": "review",
                    "agent_policy": {
                        "exclude_stage_agents": ["implementation"],
                        "allow_soft_degrade": False,
                    },
                }
            ]
        }
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):
            with self.assertRaises(RouterIsolationRejection):
                choose_agent(self.workflow_id, "review", "test")

    def test_soft_degrade_enabled_via_node_policy_reuses_agent_and_audits(self):
        workflow_config = {
            "nodes": [
                {
                    "id": "review",
                    "agent_policy": {
                        "exclude_stage_agents": ["implementation"],
                        "allow_soft_degrade": True,
                    },
                }
            ]
        }
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):
            selected, ctx = agent_router._choose_agent_impl(
                self.workflow_id, "review", "test", requested="auto"
            )
            self.assertEqual(selected, "codex")
            self.assertTrue(ctx.get("isolation_degraded"))
            self.assertIn("soft_degrade", ctx.get("degraded_reason", ""))

        events = self.store.list_events(workflow_id=self.workflow_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("router_isolation_degraded", event_types)
        degraded_event = next(e for e in events if e["event_type"] == "router_isolation_degraded")
        self.assertEqual(degraded_event["payload"]["selected"], "codex")
        self.assertTrue(degraded_event["payload"]["degraded"])

    def test_soft_degrade_enabled_via_env_var(self):
        workflow_config = {
            "nodes": [
                {
                    "id": "review",
                    "agent_policy": {
                        "exclude_stage_agents": ["implementation"],
                    },
                }
            ]
        }
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file), \
             patch.dict(os.environ, {"HERDR_ROUTER_SOFT_DEGRADE": "1"}):
            selected = choose_agent(self.workflow_id, "review", "test")
            self.assertEqual(selected, "codex")

        events = self.store.list_events(workflow_id=self.workflow_id)
        self.assertTrue(any(e["event_type"] == "router_isolation_degraded" for e in events))

    def test_node_policy_false_overrides_env_var_true(self):
        workflow_config = {
            "nodes": [
                {
                    "id": "review",
                    "agent_policy": {
                        "exclude_stage_agents": ["implementation"],
                        "allow_soft_degrade": False,
                    },
                }
            ]
        }
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file), \
             patch.dict(os.environ, {"HERDR_ROUTER_SOFT_DEGRADE": "1"}):
            with self.assertRaises(RouterIsolationRejection):
                choose_agent(self.workflow_id, "review", "test")

    def test_soft_degrade_secondary_audit_failure_raises(self):
        workflow_config = {
            "nodes": [
                {
                    "id": "review",
                    "agent_policy": {
                        "exclude_stage_agents": ["implementation"],
                        "allow_soft_degrade": True,
                    },
                }
            ]
        }
        class FailingSecondaryStore:
            def __init__(self, inner):
                self.inner = inner
            def __getattr__(self, name):
                return getattr(self.inner, name)
            def record_event(self, event_type, *args, **kwargs):
                if event_type == "router_opt_out_used":
                    return False
                return self.inner.record_event(event_type, *args, **kwargs)

        failing_store = FailingSecondaryStore(self.store)
        with patch.object(agent_router, "_get_store", return_value=failing_store), \
             patch.object(agent_router, "workflow_config_for", return_value=workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):
            with self.assertRaises(RuntimeError) as ctx:
                choose_agent(self.workflow_id, "review", "test")
            self.assertIn("not durably recorded", str(ctx.exception))

    def test_superseded_impl_tasks_do_not_exhaust_verifier_isolation_budget(self):
        """wf_ibx_f3eb258d: superseded r1 impl (agy) + active r2 impl (codex) must only exclude codex,
        letting verifier select agy even when dispatch_round=1 matches r1's round."""
        self.store.save_task(
            {
                "task_id": "wf-craft-r1",
                "workflow_id": self.workflow_id,
                "node": "craft",
                "stage": "craft",
                "status": "superseded",
                "superseded_by": "wf-craft-r2",
                "agent": "agy",
                "commit": "1" * 40,
                "dispatch_round": 1,
            }
        )
        self.store.save_task(
            {
                "task_id": "wf-craft-r2",
                "workflow_id": self.workflow_id,
                "node": "craft",
                "stage": "craft",
                "status": "cleaned",
                "agent": "codex",
                "commit": "2" * 40,
                "dispatch_round": 2,
            }
        )
        workflow_config = {
            "nodes": [
                {"id": "craft"},
                {
                    "id": "cleanroom_review",
                    "default_task_type": "review",
                    "gate": {"retry_node": "craft"},
                    "agent_policy": {
                        "preferred": ["grok", "codex", "agy"],
                        "exclude_stage_agents": ["craft"],
                        "allow_soft_degrade": True,
                    },
                },
            ]
        }
        # Stale preflight snapshot with unhealthy 'grok' still in pool before refresh
        self.store.save_workflow(
            {
                "workflow_id": self.workflow_id,
                "project_id": "default",
                "preflight_checked_at": 0,
                "healthy_agents": ["agy", "codex"],
                "unhealthy_agents": {},
            }
        )
        def fake_refresh(wid, pending, store=None):
            store.update_workflow_metadata(
                wid,
                {
                    "healthy_agents": ["agy", "codex"],
                    "unhealthy_agents": {"grok": "TOKEN_EXHAUSTED"},
                    "preflight_checked_at": 9999999999.0,
                    "preflight_agent_checked_at": {a: 9999999999.0 for a in pending},
                    "preflight_identities": {a: {"status": "READY"} for a in ("agy", "codex")},
                },
            )
        pool_data = {
            "allowed_agents": ["grok", "codex", "agy"],
            "disabled_agents": [],
            "stage_preferences": {},
            "task_type_preferences": {},
        }
        import json
        self.pools_file.write_text(json.dumps({"projects": {"default": pool_data}}))
        import herdr.deep_preflight as dp
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file), \
             patch.object(dp, "refresh_workflow_preflight", side_effect=fake_refresh), \
             patch.object(agent_router, "_agent_request_fresh", return_value=True):
            selected = choose_agent(self.workflow_id, "cleanroom_review", "review", dispatch_round=1)
            self.assertEqual(selected, "agy")

    def test_preflight_runs_before_soft_degrade_so_unhealthy_agents_do_not_bypass_degrade(self):
        """When only healthy agent is excluded (codex) and unhealthy 'grok' is still in pool before
        preflight refresh, preflight must run first so allow_soft_degrade=True triggers on ['codex']."""
        self.store.save_task(
            {
                "task_id": "wf-craft-only",
                "workflow_id": self.workflow_id,
                "node": "craft",
                "stage": "craft",
                "status": "cleaned",
                "agent": "codex",
                "commit": "2" * 40,
                "dispatch_round": 1,
            }
        )
        workflow_config = {
            "nodes": [
                {"id": "craft"},
                {
                    "id": "cleanroom_review",
                    "default_task_type": "review",
                    "gate": {"retry_node": "craft"},
                    "agent_policy": {
                        "preferred": ["grok", "codex"],
                        "exclude_stage_agents": ["craft"],
                        "allow_soft_degrade": True,
                    },
                },
            ]
        }
        self.store.save_workflow(
            {
                "workflow_id": self.workflow_id,
                "project_id": "default",
                "preflight_checked_at": 0,
                "healthy_agents": ["codex"],
                "unhealthy_agents": {},
            }
        )
        def fake_refresh(wid, pending, store=None):
            store.update_workflow_metadata(
                wid,
                {
                    "healthy_agents": ["codex"],
                    "unhealthy_agents": {"grok": "TOKEN_EXHAUSTED"},
                    "preflight_checked_at": 9999999999.0,
                    "preflight_agent_checked_at": {a: 9999999999.0 for a in pending},
                    "preflight_identities": {"codex": {"status": "READY"}},
                },
            )
        pool_data = {
            "allowed_agents": ["grok", "codex"],
            "disabled_agents": [],
            "stage_preferences": {},
            "task_type_preferences": {},
        }
        import json
        self.pools_file.write_text(json.dumps({"projects": {"default": pool_data}}))
        import herdr.deep_preflight as dp
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file), \
             patch.object(dp, "refresh_workflow_preflight", side_effect=fake_refresh), \
             patch.object(agent_router, "_agent_request_fresh", return_value=True):
            selected, ctx = agent_router._choose_agent_impl(
                self.workflow_id, "cleanroom_review", "review", dispatch_round=1
            )
            self.assertEqual(selected, "codex")
            self.assertTrue(ctx.get("isolation_degraded"))
