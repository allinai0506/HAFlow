"""Codex P1 merge gate: fail when unresolved, non-outdated Codex P1 threads exist.

Reads PR review threads via the GitHub GraphQL API (stdlib only) and exits
non-zero listing blockers, so branch protection can require this check.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List

API_URL = "https://api.github.com/graphql"
BOT_LOGIN = "chatgpt-codex-connector"
P1_MARK = "P1 Badge"

THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id path isResolved isOutdated
          comments(first: 5) { nodes { author { login } body } }
        }
      }
    }
  }
}
"""


def find_blocking_threads(payload: Dict[str, Any], bot: str = BOT_LOGIN,
                           fail_on: str = "p1") -> List[Dict[str, Any]]:
    """Return threads that block merge from a GraphQL reviewThreads payload."""
    nodes: List[Dict[str, Any]] = []
    try:
        nodes = (((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads", {}).get("nodes", []) or []
    except AttributeError:
        return []
    blockers = []
    for t in nodes:
        if not isinstance(t, dict):
            continue
        if t.get("isResolved") or t.get("isOutdated"):
            continue
        comments = ((t.get("comments") or {}).get("nodes") or [])
        bodies = [c.get("body") or "" for c in comments if isinstance(c, dict)]
        if not any(bot in str((c.get("author") or {}).get("login") or "") for c in comments if isinstance(c, dict)):
            continue
        text = "\n".join(bodies)
        if fail_on == "any" or P1_MARK in text:
            first = bodies[0].strip().splitlines()[0][:160] if bodies else ""
            blockers.append({"path": t.get("path"), "title": first})
    return blockers


def fetch_threads(owner: str, repo: str, pr: int, token: str) -> Dict[str, Any]:
    merged: Dict[str, Any] = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
    nodes = merged["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    cursor = None
    while True:
        body = json.dumps({"query": THREADS_QUERY, "variables": {
            "owner": owner, "repo": repo, "pr": pr, "cursor": cursor}}).encode()
        req = urllib.request.Request(
            API_URL, data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = json.loads(resp.read().decode())
        pr_node = ((page.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        threads = pr_node.get("reviewThreads") or {}
        nodes.extend(threads.get("nodes") or [])
        info = threads.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fail on unresolved Codex P1 review threads.")
    ap.add_argument("--repo-slug", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--pr-number", type=int, default=int(os.environ.get("PR_NUMBER") or 0))
    ap.add_argument("--bot", default=BOT_LOGIN)
    ap.add_argument("--fail-on", choices=["p1", "any"], default="p1")
    args = ap.parse_args(argv)
    if not args.repo_slug or "/" not in args.repo_slug or not args.pr_number:
        print("codex-gate: --repo-slug OWNER/REPO and --pr-number are required", file=sys.stderr)
        return 2
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        print("codex-gate: GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    owner, repo = args.repo_slug.split("/", 1)
    try:
        payload = fetch_threads(owner, repo, args.pr_number, token)
    except Exception as exc:
        print(f"codex-gate: failed to query review threads: {exc}", file=sys.stderr)
        return 2
    blockers = find_blocking_threads(payload, bot=args.bot, fail_on=args.fail_on)
    if blockers:
        print(f"codex-gate: BLOCKED by {len(blockers)} unresolved Codex thread(s):")
        for b in blockers:
            print(f"  - {b['path']}: {b['title']}")
        print("Resolve or fix the threads above, then re-run. "
              "This check must be green (and required in branch protection) before merge.")
        return 1
    print("codex-gate: PASS (no unresolved Codex P1 threads)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
