"""Unit tests for Inbox project and workspace detector and validator."""

import pytest
from unittest.mock import patch
from herdr.inbox.detector import (
    validate_project_and_workspace,
    detect_project_and_workspace,
)

MOCK_PROJECTS = {
    "version": 1,
    "projects": {
        "/Users/user/HAFlow": {
            "project_id": "haflow-1de29d2d",
            "project_name": "HAFlow",
            "project_root": "/Users/user/HAFlow",
            "workspace_id": "w1K",
        },
        "/Users/user/nexusarchive": {
            "project_id": "nexusarchive-54433229",
            "project_name": "nexusarchive",
            "project_root": "/Users/user/nexusarchive",
            "workspace_id": "w17",
        },
    },
}


def test_validate_project_and_workspace():
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        # Valid combination
        ok, err, rec = validate_project_and_workspace("haflow-1de29d2d", "w1K")
        assert ok is True
        assert err is None
        assert rec["project_name"] == "HAFlow"

        # Invalid workspace
        ok, err, _ = validate_project_and_workspace("haflow-1de29d2d", "w99_wrong")
        assert ok is False
        assert "does not match" in err

        # Unknown project
        ok, err, _ = validate_project_and_workspace("unknown-proj", "w1K")
        assert ok is False
        assert "not registered" in err


def test_detect_project_and_workspace():
    with patch("herdr.inbox.detector.load_projects", return_value=MOCK_PROJECTS):
        # 1. Mentioning HAFlow by name
        res = detect_project_and_workspace(
            instruction="Please fix the issue in HAFlow console UI",
            title="UI Fix",
        )
        assert res["recommended_project_id"] == "haflow-1de29d2d"
        assert res["recommended_workspace_id"] == "w1K"
        assert res["confidence"] >= 0.90

        # 2. Mentioning path /Users/user/nexusarchive
        res2 = detect_project_and_workspace(
            instruction="Refactor /Users/user/nexusarchive storage model",
        )
        assert res2["recommended_project_id"] == "nexusarchive-54433229"
        assert res2["recommended_workspace_id"] == "w17"
        assert res2["confidence"] == 1.0

        # 3. Irrelevant text with no project clues
        res3 = detect_project_and_workspace(
            instruction="What is the weather in Shanghai today?",
        )
        assert res3["recommended_project_id"] is None
        assert res3["confidence"] == 0.0
