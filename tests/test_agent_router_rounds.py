import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import agent_router
from herdr.agent_router import (
    MAX_REMEDIATION_ROUNDS,
    RouterIsolationRejection,
    RouterRemediationExhausted,
    choose_agent,
)
from herdr.state_store import SQLiteStateStore


class TestAgentRouterRounds(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "state.db"
        self.store = SQLiteStateStore(self.db_path)
        self.pools_file = self.root / "pools.json"
        self.reservations_file = self.root / "reservations.json"
        self.lock_file = self.root / "router.lock"

        # Setup pool with exactly 2 agents: agent-a and agent-b
        self.pools_file.write_text(
            json.dumps({
                "projects": {
                    "proj-rounds": {
                        "allowed_agents": ["agent-a", "agent-b"],
                        "disabled_agents": [],
                        "stage_preferences": {},
                        "task_type_preferences": {},
                    }
                }
            }),
            encoding="utf-8",
        )

        self.workflow_id = "wf-rounds-test"
        self.store.save_workflow({
            "workflow_id": self.workflow_id,
            "project_id": "proj-rounds",
            "status": "running",
            "execution": {"mode": "git"},
        })

        self.workflow_config = {
            "nodes": [
                {
                    "id": "craft",
                    "role": "implementation",
                },
                {
                    "id": "review",
                    "role": "verifier",
                    "agent_policy": {
                        "exclude_stage_agents": ["craft", "implementation"],
                        "allow_soft_degrade": False,
                    },
                },
            ]
        }

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_round_scoped_isolation_prevents_pool_exhaustion_in_fix_loop(self):
        """Verify that in a 2-agent pool, Round 2 review does not deadlock when round-scoped."""
        # Round 1: agent-a authors implementation
        self.store.save_task({
            "task_id": "t-craft-r1",
            "workflow_id": self.workflow_id,
            "node": "craft",
            "stage": "craft",
            "agent": "agent-a",
            "status": "completed",
            "dispatch_round": 1,
        })

        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=self.workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):

            # Round 1 Review: agent-a excluded -> agent-b selected
            reviewer_r1 = choose_agent(
                self.workflow_id,
                "review",
                "test",
                dispatch_round=1,
                candidate_author="agent-a",
            )
            self.assertEqual(reviewer_r1, "agent-b")

            # Record Round 1 Review blocked
            self.store.save_task({
                "task_id": "t-review-r1",
                "workflow_id": self.workflow_id,
                "node": "review",
                "stage": "review",
                "agent": "agent-b",
                "status": "completed",
                "stage_verdict": "blocked",
                "dispatch_round": 1,
            })

            # Round 2: agent-a performs fix
            self.store.save_task({
                "task_id": "t-craft-r2",
                "workflow_id": self.workflow_id,
                "node": "craft",
                "stage": "craft",
                "agent": "agent-a",
                "status": "completed",
                "dispatch_round": 2,
            })

            # Round 2 Review:
            # Under round-scoped isolation, agent-b is NOT excluded by Round 1 tasks.
            # agent-a (Round 2 author) IS excluded.
            # Thus agent-b is successfully selected without raising RouterIsolationRejection!
            reviewer_r2 = choose_agent(
                self.workflow_id,
                "review",
                "test",
                dispatch_round=2,
                candidate_author="agent-a",
            )
            self.assertEqual(reviewer_r2, "agent-b")

    def test_candidate_author_explicit_exclusion(self):
        """Verify candidate_author is strictly excluded for verifiers regardless of stage name."""
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=self.workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):

            # If candidate was authored by agent-b, verifier MUST be agent-a
            selected = choose_agent(
                self.workflow_id,
                "review",
                "test",
                dispatch_round=1,
                candidate_author="agent-b",
            )
            self.assertEqual(selected, "agent-a")

    def test_remediation_rounds_circuit_breaker_exhaustion(self):
        """Verify that exceeding MAX_REMEDIATION_ROUNDS raises RouterRemediationExhausted."""
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=self.workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):

            # Round 1 and 2 are within budget
            choose_agent(self.workflow_id, "craft", "task", dispatch_round=1)
            choose_agent(self.workflow_id, "craft", "task", dispatch_round=2)

            # Round 3 exceeds MAX_REMEDIATION_ROUNDS = 2
            with self.assertRaises(RouterRemediationExhausted) as ctx:
                choose_agent(
                    self.workflow_id,
                    "craft",
                    "task",
                    dispatch_round=MAX_REMEDIATION_ROUNDS + 1,
                )
            self.assertIn("Remediation round limit exceeded", str(ctx.exception))

    def test_feature_flag_disabled_falls_back_to_global_accumulation(self):
        """When HERDR_ROUTER_ROUND_SCOPED_ISOLATION=0, old flat exclusion applies and exhausts pool."""
        # Both agent-a and agent-b have been used in craft stage in history
        self.store.save_task({
            "task_id": "t-craft-1",
            "workflow_id": self.workflow_id,
            "node": "craft",
            "stage": "craft",
            "agent": "agent-a",
            "status": "completed",
            "dispatch_round": 1,
        })
        self.store.save_task({
            "task_id": "t-craft-2",
            "workflow_id": self.workflow_id,
            "node": "craft",
            "stage": "craft",
            "agent": "agent-b",
            "status": "completed",
            "dispatch_round": 2,
        })

        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=self.workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file), \
             patch.dict(os.environ, {"HERDR_ROUTER_ROUND_SCOPED_ISOLATION": "0"}):

            # Both agents are in flat exclusion -> RouterIsolationRejection
            with self.assertRaises(RouterIsolationRejection):
                choose_agent(
                    self.workflow_id,
                    "review",
                    "test",
                    dispatch_round=2,
                )

    def test_resolve_launch_agent_forwards_round_and_author_isolation(self):
        """Verify the real CLI launch path (resolve_launch_agent) forwards dispatch_round
        and candidate_author to choose_agent, enforcing author exclusion in production."""
        from types import SimpleNamespace
        from herdr.task_cli_launch import resolve_launch_agent

        # Seed round 1 implementation authored by agent-a
        self.store.save_task({
            "task_id": "t-craft-seed",
            "workflow_id": self.workflow_id,
            "node": "craft",
            "stage": "craft",
            "agent": "agent-a",
            "status": "completed",
            "commit": "a" * 40,
            "integrated_commit": "c" * 40,
            "dispatch_round": 1,
        })

        args = SimpleNamespace(
            workflow_id=self.workflow_id,
            task_id="t-review-launch",
            task_type="test",
            agent="auto",
            dispatch_role="worker",
            dispatch_round=2,
            candidate_sha="c" * 40,  # Matches agent-a's integrated_commit
        )

        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=self.workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):

            resolved, requested = resolve_launch_agent(
                args,
                "review",
                self.store,
                choose_agent=choose_agent,
                workflow_config_for=lambda _: self.workflow_config,
                is_isolation_rejection=lambda _: False,
                record_router_failure_task=lambda *a, **k: None,
                release_agent_reservation=lambda *a, **k: None,
            )

            # agent-a authored candidate "c"*40; verifier in round 2 MUST exclude agent-a and pick agent-b!
            self.assertEqual(resolved, "agent-b")

    def test_verifier_excludes_unchanged_round1_contributors_in_round2(self):
        """In multi-task subset redispatch, an unchanged round-1 task's agent still
        contributes code to the round-2 candidate. The verifier in round 2 must exclude
        both round-2 repairers and non-superseded round-1 contributors, but not superseded agents."""
        with patch.object(agent_router, "_get_store", return_value=self.store), \
             patch.object(agent_router, "workflow_config_for", return_value=self.workflow_config), \
             patch.object(agent_router, "POOLS_FILE", self.pools_file), \
             patch.object(agent_router, "RESERVATIONS_FILE", self.reservations_file), \
             patch.object(agent_router, "ROUTER_LOCK_FILE", self.lock_file):

            # Seed 3 tasks in implementation stage 'craft':
            # 1. Unchanged task from round 1 (agent-a)
            self.store.save_task({
                "task_id": "t-craft-unchanged",
                "workflow_id": self.workflow_id,
                "node": "craft",
                "stage": "craft",
                "agent": "agent-a",
                "status": "completed",
                "commit": "1" * 40,
                "dispatch_round": 1,
            })
            # 2. Superseded task from round 1 (agent-b)
            self.store.save_task({
                "task_id": "t-craft-superseded",
                "workflow_id": self.workflow_id,
                "node": "craft",
                "stage": "craft",
                "agent": "agent-b",
                "status": "superseded",
                "commit": "2" * 40,
                "dispatch_round": 1,
            })
            # 3. Repaired task from round 2 (agent-c)
            # Add agent-c to pool
            data = {
                "projects": {
                    "proj-rounds": {
                        "allowed_agents": ["agent-a", "agent-b", "agent-c"],
                        "disabled_agents": [],
                        "stage_preferences": {},
                        "task_type_preferences": {},
                    }
                }
            }
            with open(self.pools_file, "w", encoding="utf-8") as f:
                import json
                json.dump(data, f)

            self.store.save_task({
                "task_id": "t-craft-repair",
                "workflow_id": self.workflow_id,
                "node": "craft",
                "stage": "craft",
                "agent": "agent-c",
                "status": "completed",
                "commit": "3" * 40,
                "dispatch_round": 2,
            })

            # In round 2, verifier review must exclude agent-a (contributor) and agent-c (repairer).
            # agent-b was superseded and did NOT contribute to candidate, so agent-b is eligible!
            reviewer = choose_agent(
                self.workflow_id,
                "review",
                "test",
                dispatch_round=2,
                candidate_author="agent-c",
            )
            self.assertEqual(reviewer, "agent-b")
