import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import agent_router
from herdr.state_store import get_state_store


class TestAgentRouterStageExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "state.db"
        self.store = get_state_store(self.db_path)
        self.wf_id = "wf-test-exclusion"
        self.store.save_workflow({
            "workflow_id": self.wf_id,
            "project_id": "test-proj",
            "status": "running",
            "healthy_agents": ["codex", "claude", "opencode"],
            "unhealthy_agents": {},
        })
        self.store.save_task({
            "task_id": "task-impl-1",
            "workflow_id": self.wf_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })

    def tearDown(self):
        self.tmp_dir.cleanup()

    @patch("herdr.agent_router._get_store")
    @patch("herdr.agent_router.workflow_config_for")
    def test_exclude_stage_agents_auto_selection(self, mock_wf_cfg, mock_get_store):
        mock_get_store.return_value = self.store
        mock_wf_cfg.return_value = {
            "nodes": [
                {
                    "id": "test",
                    "agent_policy": {
                        "preferred": ["codex", "claude", "opencode"],
                        "exclude_stage_agents": ["implementation"],
                    }
                }
            ]
        }
        chosen = agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertNotEqual(chosen, "codex")
        self.assertEqual(chosen, "claude")

    @patch("herdr.agent_router._get_store")
    @patch("herdr.agent_router.workflow_config_for")
    def test_explicit_requested_conflict_raises(self, mock_wf_cfg, mock_get_store):
        mock_get_store.return_value = self.store
        mock_wf_cfg.return_value = {
            "nodes": [
                {
                    "id": "review",
                    "agent_policy": {
                        "preferred": ["claude", "opencode"],
                        "exclude_stage_agents": ["implementation"],
                    }
                }
            ]
        }
        with self.assertRaises(RuntimeError) as ctx:
            agent_router.choose_agent(self.wf_id, "review", "test", requested="codex")
        self.assertIn("prohibited", str(ctx.exception).lower())

    @patch("herdr.agent_router._get_store")
    @patch("herdr.agent_router.workflow_config_for")
    def test_single_agent_environment_graceful_fallback(self, mock_wf_cfg, mock_get_store):
        self.store.save_workflow({
            "workflow_id": self.wf_id,
            "project_id": "test-proj",
            "status": "running",
            "healthy_agents": ["codex"],
            "unhealthy_agents": {},
        })
        mock_get_store.return_value = self.store
        mock_wf_cfg.return_value = {
            "nodes": [
                {
                    "id": "test",
                    "agent_policy": {
                        "preferred": ["codex"],
                        "exclude_stage_agents": ["implementation"],
                    }
                }
            ]
        }
        chosen = agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertEqual(chosen, "codex")
