"""Regression tests for P1/P2: clean-worktree PR push and auto-rebase on BASELINE STALE."""

import subprocess
from pathlib import Path

from herdr.launch_baseline import try_auto_rebase_candidate
from herdr.pr_delivery import _push_from_clean_worktree


def _init_repo_with_bare_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "dev", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "dev"], check=True, capture_output=True)
    return repo, remote


def test_push_from_clean_worktree_ignores_dirty_untracked_files_in_source_repo(tmp_path):
    repo, remote = _init_repo_with_bare_remote(tmp_path)
    # Create a candidate commit on a feature branch
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feat"], check=True, capture_output=True)
    (repo / "feat.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feat.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feat"], check=True, capture_output=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "dev"], check=True, capture_output=True)

    # Dirty source_repo with untracked file that a pre-push hook forbids
    (repo / "demo_dirty.txt").write_text("dirty\n", encoding="utf-8")
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nif [ -e demo_dirty.txt ]; then echo 'DIRTY WORKTREE'; exit 1; fi\n", encoding="utf-8")
    hook.chmod(0o755)

    _push_from_clean_worktree(repo, sha, "herdr/integration-t1", str(remote))
    pushed = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", str(remote), "refs/heads/herdr/integration-t1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert pushed.startswith(sha)
    wt_list = subprocess.run(["git", "-C", str(repo), "worktree", "list"], check=True, capture_output=True, text=True).stdout
    assert len(wt_list.strip().splitlines()) == 1


def test_try_auto_rebase_candidate_succeeds_cleanly_and_aborts_on_conflict(tmp_path):
    repo, remote = _init_repo_with_bare_remote(tmp_path)
    # Candidate modifies feat.txt
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "cand"], check=True, capture_output=True)
    (repo / "feat.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feat.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "cand"], check=True, capture_output=True)
    cand_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    # Another clone advances origin/dev on unrelated file other.txt
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "config", "user.email", "o@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "config", "user.name", "Other"], check=True, capture_output=True)
    (other / "other.txt").write_text("other\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "advance dev"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "push", "origin", "dev"], check=True, capture_output=True)
    dev_tip = subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    res = try_auto_rebase_candidate(repo, cand_sha, base_branch="dev")
    assert res["ok"] is True
    assert res["new_sha"] != cand_sha
    assert res["base_tip"] == dev_tip
    # New sha must be descendant of dev_tip
    anc = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", dev_tip, res["new_sha"]], capture_output=True)
    assert anc.returncode == 0

    # Now advance origin/dev on feat.txt to force conflict
    (other / "feat.txt").write_text("conflicting\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "add", "feat.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "conflict"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(other), "push", "origin", "dev"], check=True, capture_output=True)

    conflict_res = try_auto_rebase_candidate(repo, cand_sha, base_branch="dev")
    assert conflict_res["ok"] is False
    assert conflict_res["reason"] == "rebase_conflict"
    wt_list = subprocess.run(["git", "-C", str(repo), "worktree", "list"], check=True, capture_output=True, text=True).stdout
    assert len(wt_list.strip().splitlines()) == 1
