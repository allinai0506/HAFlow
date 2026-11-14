"""Unit tests for console runtime offline space fallback behavior."""
import unittest
from unittest.mock import patch
from console import runtime


class TestConsoleOfflineSpaceFallback(unittest.TestCase):
    def test_spaces_offline_fallback_for_registered_projects(self):
        fake_projects = [
            {
                "project_id": "proj-haflow",
                "project_name": "HAFlow",
                "project_root": "/Users/user/HAFlow",
                "workspace_id": "w1H",
            },
            {
                "project_id": "proj-no-wid",
                "project_name": "ProjectWithoutWid",
                "project_root": "/Users/user/proj2",
                "workspace_id": None,
            },
        ]
        # Herdr server is offline: herdr_workspaces returns []
        with patch("console.inventory.projects", return_value=fake_projects), \
             patch("console.runtime.herdr_workspaces", return_value=[]):
            ss = runtime.spaces()
            self.assertEqual(len(ss), 2)
            s1 = next(s for s in ss if s["project_id"] == "proj-haflow")
            self.assertEqual(s1["workspace_id"], "w1H")
            self.assertEqual(s1["label"], "HAFlow")
            self.assertEqual(s1["relation"], "current_factory")
            self.assertEqual(s1["tab_count"], 0)
            self.assertEqual(s1["pane_count"], 0)
            self.assertEqual(s1["project_root"], "/Users/user/HAFlow")

            s2 = next(s for s in ss if s["project_id"] == "proj-no-wid")
            self.assertEqual(s2["workspace_id"], "proj-no-wid")
            self.assertEqual(s2["label"], "ProjectWithoutWid")
            self.assertEqual(s2["relation"], "current_factory")

    def test_spaces_merges_live_workspaces_without_duplication(self):
        fake_projects = [
            {
                "project_id": "proj-haflow",
                "project_name": "HAFlow",
                "project_root": "/Users/user/HAFlow",
                "workspace_id": "w1H",
            }
        ]
        fake_live_workspaces = [
            {
                "workspace_id": "w1H",
                "label": "HAFlow Live",
                "number": 1,
                "tab_count": 4,
                "pane_count": 8,
            }
        ]
        with patch("console.inventory.projects", return_value=fake_projects), \
             patch("console.runtime.herdr_workspaces", return_value=fake_live_workspaces):
            ss = runtime.spaces()
            self.assertEqual(len(ss), 1)
            self.assertEqual(ss[0]["workspace_id"], "w1H")
            self.assertEqual(ss[0]["relation"], "current_factory")
            self.assertEqual(ss[0]["tab_count"], 4)
            self.assertEqual(ss[0]["pane_count"], 8)

    def test_spaces_empty_when_no_projects_and_no_workspaces(self):
        with patch("console.inventory.projects", return_value=[]), \
             patch("console.runtime.herdr_workspaces", return_value=[]):
            ss = runtime.spaces()
            self.assertEqual(ss, [])

    def test_overview_includes_fallback_spaces(self):
        fake_projects = [
            {
                "project_id": "proj-haflow",
                "project_name": "HAFlow",
                "project_root": "/Users/user/HAFlow",
                "workspace_id": "w1H",
            }
        ]
        with patch("console.inventory.projects", return_value=fake_projects), \
             patch("console.runtime.herdr_workspaces", return_value=[]), \
             patch("console.inventory.tasks", return_value=[]), \
             patch("console.inventory.workflows", return_value={}), \
             patch("console.runtime.service_status", return_value={}):
            ov = runtime.overview()
            self.assertEqual(ov["space_count"], 1)
            self.assertEqual(len(ov["spaces"]), 1)
            self.assertEqual(ov["spaces"][0]["workspace_id"], "w1H")
            self.assertEqual(ov["spaces"][0]["project_name"], "HAFlow")
