"""Read-only Git facts used to classify task commit adoption."""

import os
import subprocess


def adoption_skew_seconds():
    try:
        return max(0, int(os.environ.get("HERDR_ADOPT_SKEW_SECONDS", "120")))
    except (TypeError, ValueError):
        return 120


def git_head_sha(clone):
    result = subprocess.run(
        ["git", "-C", clone, "rev-parse", "HEAD"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_is_ancestor(clone, baseline, head):
    if not baseline or not head:
        return False
    result = subprocess.run(
        ["git", "-C", clone, "merge-base", "--is-ancestor", baseline, head],
        text=True, capture_output=True, check=False,
    )
    return result.returncode == 0


def git_log_commits(clone, rev, limit=None, first_parent=False):
    cmd = ["git", "-C", clone, "log", "--format=%H%x1f%P%x1f%at%x1f%ct%x1f%an%x1f%ae"]
    if first_parent:
        cmd.append("--first-parent")
    if limit is not None:
        cmd += ["-n", str(limit)]
    cmd += [rev, "--"]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return None
    commits = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        sha, raw_parents, raw_at, raw_ct = parts[:4]
        sha = sha.strip()
        if not sha:
            continue
        parents = [p for p in raw_parents.strip().split() if p]
        try:
            author_ts = float(raw_at.strip())
            committer_ts = float(raw_ct.strip())
        except ValueError:
            continue
        author_name = parts[4].strip() if len(parts) > 4 else ""
        author_email = parts[5].strip() if len(parts) > 5 else ""
        commits.append({
            "sha": sha,
            "author_ts": author_ts,
            "committer_ts": committer_ts,
            "parents": parents,
            "author_name": author_name,
            "author_email": author_email,
        })
    return commits


def git_commit_paths(clone, sha):
    """Return [] for an empty commit and None when path enumeration fails."""
    if not sha:
        return None
    result = subprocess.run(
        ["git", "-C", clone, "diff-tree", "--no-commit-id", "--name-only",
         "-r", "--root", sha, "--"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def git_current_branch(clone):
    """Return the checked-out branch or None for detached/failed probes."""
    try:
        result = subprocess.run(
            ["git", "-C", clone, "branch", "--show-current"],
            text=True, capture_output=True, check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def git_remote_contained_shas(clone, shas, exclude_branches=None):
    """Find commits reachable from foreign origin refs; None means unknown."""
    contained = set()
    candidates = [sha for sha in (shas or []) if sha]
    if not candidates:
        return contained
    excluded = set()
    for branch in (exclude_branches or []):
        name = branch.strip() if isinstance(branch, str) else ""
        if name:
            excluded.add(f"origin/{name}")
    for sha in candidates:
        try:
            result = subprocess.run(
                ["git", "-C", clone, "branch", "-r", "--contains", sha],
                text=True, capture_output=True, check=False,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        if not (result.stdout or "").strip():
            continue
        for line in result.stdout.splitlines():
            ref = line.strip()
            if not ref or ref == "origin/HEAD" or "->" in ref or ref in excluded:
                continue
            contained.add(sha)
            break
    return contained


def git_interval_commits(clone, baseline, head, *, log_commits=None, commit_paths=None):
    """Return baseline..HEAD commits oldest first, with path facts."""
    log_commits = log_commits or git_log_commits
    commit_paths = commit_paths or git_commit_paths
    commits = log_commits(clone, f"{baseline}..{head}")
    if commits is None:
        return None
    if not commits:
        return []
    commits.reverse()
    for item in commits:
        item["paths"] = commit_paths(clone, item["sha"])
    return commits


def git_head_history(clone, head, limit=100, *, log_commits=None, commit_paths=None):
    """Return the first-parent history newest first, with path facts."""
    log_commits = log_commits or git_log_commits
    commit_paths = commit_paths or git_commit_paths
    commits = log_commits(clone, head, limit=limit, first_parent=True)
    if commits is None:
        return None
    for item in commits or []:
        item["paths"] = commit_paths(clone, item["sha"])
    return commits or []
