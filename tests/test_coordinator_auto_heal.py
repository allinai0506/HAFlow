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


if __name__ == "__main__":
    unittest.main()


