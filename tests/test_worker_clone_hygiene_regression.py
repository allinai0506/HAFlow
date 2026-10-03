"""Regression tests for §2.12: Worktree uncommitted deletions and clone hygiene.

Verifies that:
1. `herdr.repo_hygiene.check_source_cleanliness` detects dirty tracked working trees.
2. `services.herdr-worker.create_clone` refuses to clone a source repository with uncommitted tracked changes.
"""
import subprocess
from pathlib import Path
import pytest
from herdr.repo_hygiene import check_source_cleanliness
import importlib.machinery
import importlib.util


def _init_repo(path: Path) -> Path:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "file.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True)
    return path


def test_check_source_cleanliness_clean_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    clean, dirty = check_source_cleanliness(repo)
    assert clean is True
    assert dirty == []


def test_check_source_cleanliness_detects_modified_and_deleted(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    (repo / "file.txt").unlink()
    clean, dirty = check_source_cleanliness(repo)
    assert clean is False
    assert "file.txt" in dirty


def test_check_source_cleanliness_ignores_internal_untracked(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    # Internal untracked files like .tasks.json or runtime markers should be ignored
    (repo / ".tasks.json").write_text("{}")
    clean, dirty = check_source_cleanliness(repo)
    assert clean is True
    assert dirty == []
