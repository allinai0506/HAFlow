"""Tests for coordinator auto-heal and fallback dispatch mechanisms."""

import unittest
from unittest.mock import MagicMock, patch

from herdr import projects


class CoordinatorAutoHealTestCase(unittest.TestCase):
    def setUp(self):
        projects._COORDINATOR_HEAL_ATTEMPTS.clear()

    @patch("herdr.projects._pane_alive")
    @patch("herdr.projects._coordinator_alive")
    @patch("herdr.projects._start_coordinator")
    def test_ensure_coordinator_running_when_already_alive(
        self, mock_start, mock_coord_alive, mock_pane_alive
    ):
        mock_pane_alive.return_value = True
        mock_coord_alive.return_value = True

        res = projects.ensure_coordinator_running("proj1", "w1:p1")
        self.assertTrue(res)
        mock_start.assert_not_called()

    @patch("herdr.projects._pane_alive")
    @patch("herdr.projects._coordinator_alive")
    @patch("herdr.projects._start_coordinator")
    def test_ensure_coordinator_running_when_agent_missing(
        self, mock_start, mock_coord_alive, mock_pane_alive
    ):
        mock_pane_alive.return_value = True
        # First check False (missing), second check True (started)
        mock_coord_alive.side_effect = [False, True]

        res = projects.ensure_coordinator_running("proj1", "w1:p1")
        self.assertTrue(res)
        mock_start.assert_called_once_with("proj1", "w1:p1")

    @patch("herdr.projects._pane_alive")
    @patch("herdr.projects._coordinator_alive")
    @patch("herdr.projects._start_coordinator")
    def test_ensure_coordinator_running_rate_limited(
        self, mock_start, mock_coord_alive, mock_pane_alive
    ):
        mock_pane_alive.return_value = True
        mock_coord_alive.return_value = False

        res1 = projects.ensure_coordinator_running("proj1", "w1:p1", min_interval_seconds=60.0)
        self.assertFalse(res1)
        mock_start.assert_called_once_with("proj1", "w1:p1")

        # Second call within 60s should be debounced
        mock_start.reset_mock()
        res2 = projects.ensure_coordinator_running("proj1", "w1:p1", min_interval_seconds=60.0)
        self.assertFalse(res2)
        mock_start.assert_not_called()

    @patch("herdr.projects._pane_alive")
    def test_ensure_coordinator_running_pane_dead(self, mock_pane_alive):
        mock_pane_alive.return_value = False
        res = projects.ensure_coordinator_running("proj1", "w1:p1")
        self.assertFalse(res)


class ControllerCoordinatorStatusAutoHealTestCase(unittest.TestCase):
    def test_coordinator_status_auto_heal(self):
        import importlib
        import subprocess

        controller = importlib.import_module("services.herdr-controller")

        with patch.object(controller, "coordinator_pane_for_workflow", return_value="w15:p1"), \
             patch.object(controller, "project_for_workflow", return_value={"project_id": "test_proj"}), \
             patch.object(controller, "ensure_coordinator_running", return_value=True) as mock_ensure, \
             patch.object(subprocess, "check_output", side_effect=[
                 subprocess.CalledProcessError(1, ["herdr", "agent", "get"]),
                 '{"id":"1","result":{"agent":{"agent_status":"idle"}},"type":"agent_info"}',
             ]):
            status = controller.coordinator_status("wf-1", auto_heal=True)
            mock_ensure.assert_called_once_with("test_proj", "w15:p1")
            self.assertEqual(status, "idle")


class CoordinatorIntakeFallbackTestCase(unittest.TestCase):
    def setUp(self):
        import importlib
        self.controller = importlib.import_module("services.herdr-controller")

    def test_intake_stalled_fallback_with_coordinator_stalled(self):
        """When coordinator_stalled has >= 2 attempts, fallback to direct dispatch must trigger."""
        workflow_id = "wf-fallback-coord"
        node_id = "first_node"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        fake_episode = {"attempts": 2, "reason": "coordinator_stalled"}

        with patch.object(self.controller, "attention_get", return_value=fake_episode) as mock_get, \
             patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
             patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
             patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"):
            self.controller._handle_coordinator_item(item)
            mock_direct.assert_called_once_with(item)
            mock_get.assert_called_with(f"{workflow_id}:stage_advance:{node_id}")

    def test_intake_stalled_not_triggered_for_stage_advance_stalled(self):
        """When reason is stage_advance_stalled (notification-only identifier), intake must NOT fall back to direct dispatch."""
        workflow_id = "wf-no-fallback-stage"
        node_id = "first_node"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        fake_episode = {"attempts": 2, "reason": "stage_advance_stalled"}

        with patch.object(self.controller, "attention_get", return_value=fake_episode), \
             patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
             patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
             patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
             patch.object(self.controller, "project_for_workflow", return_value={}):
            self.controller._handle_coordinator_item(item)
            mock_direct.assert_not_called()

    def test_missing_coordinator_pane_accumulates_attempts_and_falls_back(self):
        """When coordinator pane is repeatedly missing, attempts accumulate and fallback triggers on >= 2 attempts."""
        workflow_id = "wf-missing-pane"
        node_id = "node1"
        key = f"{workflow_id}:stage_advance:{node_id}"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        self.controller.attention_clear(key)

        try:
            with patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
                 patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
                 patch.object(self.controller, "coordinator_pane_for_workflow", return_value=None):

                # First failure: attempts becomes 1, direct dispatch must NOT be triggered
                self.controller._handle_coordinator_item(item)
                ep1 = self.controller.attention_get(key)
                self.assertIsNotNone(ep1)
                self.assertEqual(ep1["attempts"], 1)
                self.assertEqual(ep1["reason"], "coordinator_stalled")
                mock_direct.assert_not_called()

                # Second failure: attempts becomes 2, direct dispatch still not called on this run
                self.controller._handle_coordinator_item(item)
                ep2 = self.controller.attention_get(key)
                self.assertEqual(ep2["attempts"], 2)
                mock_direct.assert_not_called()

                # Third invocation: attempts >= 2 detected at intake entry, fallback triggers!
                self.controller._handle_coordinator_item(item)
                mock_direct.assert_called_once_with(item)
        finally:
            self.controller.attention_clear(key)

    def test_prompt_crash_accumulates_attempts_and_falls_back(self):
        """When coordinator agent prompt fails/crashes (returncode != 0), attempts accumulate and fallback triggers on >= 2 attempts."""
        workflow_id = "wf-prompt-crash"
        node_id = "node1"
        key = f"{workflow_id}:stage_advance:{node_id}"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        self.controller.attention_clear(key)

        crash_result = MagicMock()
        crash_result.returncode = 1
        crash_result.stderr = "agent process died unexpectedly"
        crash_result.stdout = ""

        try:
            with patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
                 patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
                 patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
                 patch.object(self.controller, "project_for_workflow", return_value={"project_name": "test", "project_root": "/tmp", "coordinator_pane_id": "w1:p1"}), \
                 patch.object(self.controller, "coordinator_status", return_value="idle"), \
                 patch.object(self.controller, "workflow_closed", return_value=False), \
                 patch("subprocess.run", return_value=crash_result):

                # First failure: attempts becomes 1, no direct dispatch
                self.controller._handle_coordinator_item(item)
                ep1 = self.controller.attention_get(key)
                self.assertIsNotNone(ep1)
                self.assertEqual(ep1["attempts"], 1)
                self.assertEqual(ep1["reason"], "coordinator_stalled")
                mock_direct.assert_not_called()

                # Second failure: attempts becomes 2, no direct dispatch
                self.controller._handle_coordinator_item(item)
                ep2 = self.controller.attention_get(key)
                self.assertEqual(ep2["attempts"], 2)
                mock_direct.assert_not_called()

                # Third invocation: fallback to direct dispatch triggers
                self.controller._handle_coordinator_item(item)
                mock_direct.assert_called_once_with(item)
        finally:
            self.controller.attention_clear(key)

    def test_prompt_exception_accumulates_attempts_and_falls_back(self):
        """When coordinator agent prompt raises an exception (e.g. process timeout/kill), attempts accumulate and fallback triggers on >= 2 attempts."""
        workflow_id = "wf-prompt-exc"
        node_id = "node1"
        key = f"{workflow_id}:stage_advance:{node_id}"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        self.controller.attention_clear(key)

        try:
            with patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
                 patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
                 patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
                 patch.object(self.controller, "project_for_workflow", return_value={"project_name": "test", "project_root": "/tmp", "coordinator_pane_id": "w1:p1"}), \
                 patch.object(self.controller, "coordinator_status", return_value="idle"), \
                 patch.object(self.controller, "workflow_closed", return_value=False), \
                 patch("subprocess.run", side_effect=Exception("Subprocess killed")):

                # First exception: attempts becomes 1
                self.controller._handle_coordinator_item(item)
                ep1 = self.controller.attention_get(key)
                self.assertIsNotNone(ep1)
                self.assertEqual(ep1["attempts"], 1)
                mock_direct.assert_not_called()

                # Second exception: attempts becomes 2
                self.controller._handle_coordinator_item(item)
                ep2 = self.controller.attention_get(key)
                self.assertEqual(ep2["attempts"], 2)
                mock_direct.assert_not_called()

                # Third invocation: fallback triggers
                self.controller._handle_coordinator_item(item)
                mock_direct.assert_called_once_with(item)
        finally:
            self.controller.attention_clear(key)

    def test_intake_stalled_not_triggered_below_attempts_threshold(self):
        """When attempts < 2, intake must NOT fall back and should proceed with coordinator intake."""
        workflow_id = "wf-no-fallback"
        node_id = "first_node"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        fake_episode = {"attempts": 1, "reason": "coordinator_stalled"}

        with patch.object(self.controller, "attention_get", return_value=fake_episode), \
             patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
             patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
             patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
             patch.object(self.controller, "project_for_workflow", return_value={}):
            self.controller._handle_coordinator_item(item)
            mock_direct.assert_not_called()

    def test_intake_stalled_not_triggered_for_unrelated_reason(self):
        """When reason is unrelated (e.g. upstream_blocked), intake must NOT fall back."""
        workflow_id = "wf-unrelated-reason"
        node_id = "first_node"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        fake_episode = {"attempts": 2, "reason": "upstream_blocked"}

        with patch.object(self.controller, "attention_get", return_value=fake_episode), \
             patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
             patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
             patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
             patch.object(self.controller, "project_for_workflow", return_value={}):
            self.controller._handle_coordinator_item(item)
            mock_direct.assert_not_called()

    def test_producer_to_consumer_end_to_end_attention_consistency(self):
        """Verify real attention_note writes match attention_get expectations in controller."""
        import time
        workflow_id = "wf-e2e-contract"
        node_id = "step1"
        key = f"{workflow_id}:stage_advance:{node_id}"
        self.controller.attention_clear(key)

        try:
            # Step 1: Producer writes first timeout stall
            ep = self.controller.attention_get(key) or {}
            att1 = int(ep.get("attempts") or 0) + 1
            self.controller.attention_note(
                key,
                {"task_id": f"stage_advance:{node_id}", "workflow_id": workflow_id},
                "stage_advance",
                reason="coordinator_stalled",
                attempts=att1,
                next_retry_at=time.time() + 600,
            )

            # Step 2: Producer writes second timeout stall
            ep = self.controller.attention_get(key) or {}
            att2 = int(ep.get("attempts") or 0) + 1
            self.controller.attention_note(
                key,
                {"task_id": f"stage_advance:{node_id}", "workflow_id": workflow_id},
                "stage_advance",
                reason="coordinator_stalled",
                attempts=att2,
                next_retry_at=time.time() + 600,
            )

            recorded_ep = self.controller.attention_get(key)
            self.assertEqual(recorded_ep["attempts"], 2)
            self.assertEqual(recorded_ep["reason"], "coordinator_stalled")

            # Step 3: Consumer _handle_coordinator_item consumes this exact record
            item = {
                "kind": "stage_advance",
                "workflow_id": workflow_id,
                "stage": "start",
                "node_id": node_id,
                "next_stage": node_id,
            }

            with patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
                 patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
                 patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"):
                self.controller._handle_coordinator_item(item)
                mock_direct.assert_called_once_with(item)
        finally:
            self.controller.attention_clear(key)

    def test_stage_advance_success_clears_attention_and_resets_attempts(self):
        """When coordinator successfully notifies stage advance, attention is cleared and subsequent failures start from 1."""
        workflow_id = "wf-success-clears"
        node_id = "node1"
        key = f"{workflow_id}:stage_advance:{node_id}"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        self.controller.attention_clear(key)

        crash_result = MagicMock()
        crash_result.returncode = 1
        crash_result.stderr = "transient crash"
        crash_result.stdout = ""

        success_result = MagicMock()
        success_result.returncode = 0
        success_result.stderr = ""
        success_result.stdout = "ok"

        try:
            with patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
                 patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
                 patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
                 patch.object(self.controller, "project_for_workflow", return_value={"project_name": "test", "project_root": "/tmp", "coordinator_pane_id": "w1:p1"}), \
                 patch.object(self.controller, "coordinator_status", return_value="idle"), \
                 patch.object(self.controller, "workflow_closed", return_value=False):

                # Step 1: First failure sets attempts = 1
                with patch("subprocess.run", return_value=crash_result):
                    self.controller._handle_coordinator_item(item)
                ep1 = self.controller.attention_get(key)
                self.assertIsNotNone(ep1)
                self.assertEqual(ep1["attempts"], 1)

                # Step 2: Next invocation succeeds, clearing attention
                with patch("subprocess.run", return_value=success_result):
                    self.controller._handle_coordinator_item(item)
                ep_cleared = self.controller.attention_get(key)
                self.assertIsNone(ep_cleared)

                # Step 3: Later, another isolated crash occurs; attempts must start at 1, NOT 2
                with patch("subprocess.run", return_value=crash_result):
                    self.controller._handle_coordinator_item(item)
                ep2 = self.controller.attention_get(key)
                self.assertIsNotNone(ep2)
                self.assertEqual(ep2["attempts"], 1)
                # Must NOT have fallen back to direct dispatch!
                mock_direct.assert_not_called()
        finally:
            self.controller.attention_clear(key)

    def test_non_consecutive_crash_after_stale_window_resets_attempts(self):
        """When a failure occurs long after a previous failure (> task_stall_after SLA), attempts reset to 1."""
        import time
        workflow_id = "wf-stale-reset"
        node_id = "node1"
        key = f"{workflow_id}:stage_advance:{node_id}"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        self.controller.attention_clear(key)

        try:
            # Seed an old episode from 1 hour ago (stale)
            self.controller.attention_note(
                key,
                {"task_id": f"stage_advance:{node_id}", "workflow_id": workflow_id},
                "stage_advance",
                reason="coordinator_stalled",
                attempts=1,
            )
            # Artificially set last_attempt_at to 3600 seconds ago
            ep = self.controller.attention_get(key)
            ep["last_attempt_at"] = time.time() - 3600
            self.controller._attention_store.upsert(key, ep)

            with patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
                 patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
                 patch.object(self.controller, "coordinator_pane_for_workflow", return_value=None):

                # Missing pane failure occurs now; because previous was > 1800s ago, attempts resets to 1
                self.controller._handle_coordinator_item(item)
                new_ep = self.controller.attention_get(key)
                self.assertEqual(new_ep["attempts"], 1)
                mock_direct.assert_not_called()
        finally:
            self.controller.attention_clear(key)

    def test_stale_attention_episode_does_not_trigger_intake_fallback(self):
        """Even if an old episode had attempts=2, if it is stale (> task_stall_after), intake must NOT fall back."""
        import time
        workflow_id = "wf-stale-no-fallback"
        node_id = "node1"
        item = {
            "kind": "stage_advance",
            "workflow_id": workflow_id,
            "stage": "start",
            "node_id": node_id,
            "next_stage": node_id,
        }
        fake_episode = {
            "attempts": 2,
            "reason": "coordinator_stalled",
            "last_attempt_at": time.time() - 3600,  # 1 hour ago
        }

        with patch.object(self.controller, "attention_get", return_value=fake_episode), \
             patch.object(self.controller, "coordinator_intake_enabled", return_value=True), \
             patch.object(self.controller, "try_direct_stage_advance", return_value=True) as mock_direct, \
             patch.object(self.controller, "coordinator_pane_for_workflow", return_value="w1:p1"), \
             patch.object(self.controller, "project_for_workflow", return_value={}):
            self.controller._handle_coordinator_item(item)
            mock_direct.assert_not_called()

    def test_mark_stage_advance_notified_clears_exact_target_node_id_key(self):
        """Verify mark_stage_advance_notified clears the exact target_node_id episode key used by _handle_coordinator_item."""
        workflow_id = "wf-key-align"
        target_node = "plan_step"
        key = f"{workflow_id}:stage_advance:{target_node}"

        self.controller.attention_note(
            key,
            {"task_id": f"stage_advance:{target_node}", "workflow_id": workflow_id},
            "stage_advance",
            reason="coordinator_stalled",
            attempts=1,
        )
        self.assertIsNotNone(self.controller.attention_get(key))

        try:
            self.controller.mark_stage_advance_notified(workflow_id, target_node)
            self.assertIsNone(self.controller.attention_get(key))
        finally:
            self.controller.attention_clear(key)

    def test_mark_stage_advance_notified_handles_clear_error_gracefully(self):
        """Verify that an exception in attention_clear does not crash mark_stage_advance_notified or leave silent failure."""
        workflow_id = "wf-clear-err"
        target_node = "test_step"

        with patch.object(self.controller, "attention_clear", side_effect=OSError("Disk failure")), \
             patch("builtins.print") as mock_print:
            # Should not raise exception
            self.controller.mark_stage_advance_notified(workflow_id, target_node)
            # Verify error was explicitly logged
            printed = " ".join(str(call.args) for call in mock_print.call_args_list)
            self.assertIn("STAGE ADVANCE ATTENTION CLEAR ERROR", printed)


if __name__ == "__main__":
    unittest.main()


