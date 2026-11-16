"""Unit tests for Inbox project and workspace detector and validator."""

import pytest
from unittest.mock import patch
from herdr.inbox.detector import (
    validate_project_and_workspace,
    detect_project_and_workspace,
    CONFIDENCE_THRESHOLD,
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
        # 1. Mentioning HAFlow by name -> high confidence
        res = detect_project_and_workspace(
            instruction="Please fix the issue in HAFlow console UI",
            title="UI Fix",
        )
        assert res["recommended_project_id"] == "haflow-1de29d2d"
        assert res["recommended_workspace_id"] == "w1K"
        assert res["confidence"] >= 0.90
        assert res["is_confident"] is True

        # 2. Mentioning path /Users/user/nexusarchive -> 1.0 confidence
        res2 = detect_project_and_workspace(
            instruction="Check tests in /Users/user/nexusarchive/tests",
        )
        assert res2["recommended_project_id"] == "nexusarchive-54433229"
        assert res2["confidence"] == 1.0
        assert res2["is_confident"] is True

        # 3. Unrelated text -> 0.0 confidence, is_confident=False
        res3 = detect_project_and_workspace(instruction="Cook some pasta today")
        assert res3["recommended_project_id"] is None
        assert res3["confidence"] == 0.0
        assert res3["is_confident"] is False


def test_ambiguous_equal_matches_never_guess():
    """P1-01: When multiple projects match with equal score, do NOT pick arbitrarily."""
    tied_projects = {
        "version": 1,
        "projects": {
            "p1": {"project_id": "proj-a", "project_name": "SharedName", "workspace_id": "wA"},
            "p2": {"project_id": "proj-b", "project_name": "SharedName", "workspace_id": "wB"},
        },
    }
    with patch("herdr.inbox.detector.load_projects", return_value=tied_projects):
        res = detect_project_and_workspace(instruction="Work on SharedName module")
        assert res["recommended_project_id"] is None
        assert res["is_confident"] is False
        assert "多个匹配度相同" in res["reason"]


def test_single_project_fallback_marked_not_confident():
    """P1-01: Single project fallback must be marked is_confident=False with < 0.80 score."""
    single_project = {
        "version": 1,
        "projects": {
            "p1": {"project_id": "haflow-1de29d2d", "project_name": "HAFlow", "workspace_id": "w1K"},
        },
    }
    with patch("herdr.inbox.detector.load_projects", return_value=single_project):
        res = detect_project_and_workspace(instruction="Some generic task without keywords")
        assert res["recommended_project_id"] == "haflow-1de29d2d"
        assert res["confidence"] == 0.50
        assert res["confidence"] < CONFIDENCE_THRESHOLD
        assert res["is_confident"] is False
        assert "严禁自动执行" in res["reason"]
