"""Codex P1 merge gate: fail when unresolved, non-outdated Codex P1 threads exist.

Reads PR review threads via the GitHub GraphQL API (stdlib only) and exits
non-zero listing blockers, so branch protection can require this check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from typing import Any, Dict, List

API_URL = "https://api.github.com/graphql"
BOT_LOGIN = "chatgpt-codex-connector"
BLOCK_MARKS = ("P1 Badge", "P0 Badge")
DEFAULT_MAX_ROUNDS = int(os.environ.get("CODEX_MAX_ROUNDS", "2"))

THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviews(first: 50) {
        nodes {
          author { login }
          state
          submittedAt
        }
      }
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id path isResolved isOutdated
          comments(first: 10) { nodes { author { login } body } }
        }
      }
    }
  }
}
"""


def is_thread_backlogged(comments: List[Dict[str, Any]], bot: str = BOT_LOGIN) -> bool:
    """True if any non-bot author replied referencing an issue/backlog."""
    for c in comments:
        author = str((c.get("author") or {}).get("login") or "").lower()
        if bot.lower() in author:
            continue
        body = str(c.get("body") or "").lower()
        if re.search(r"(?:issues/\d+|#\d+|review[-_]backlog|转入\s*backlog|defer|backlog)", body):
            return True
    return False


def find_blocking_threads(payload: Dict[str, Any], bot: str = BOT_LOGIN,
                           fail_on: str = "p1", allow_backlog: bool = True) -> List[Dict[str, Any]]:
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
        comments = [c for c in comments if isinstance(c, dict)]
        if not any(bot in str((c.get("author") or {}).get("login") or "") for c in comments):
            continue
        if allow_backlog and is_thread_backlogged(comments, bot=bot):
            continue
        bodies = [c.get("body") or "" for c in comments]
        text = "\n".join(bodies)
        if fail_on == "any" or any(m in text for m in BLOCK_MARKS):
            lines = bodies[0].strip().splitlines() if bodies else []
            first = lines[0][:160] if lines else ""
            blockers.append({"path": t.get("path"), "title": first})
    return blockers


def count_bot_reviews(payload: Dict[str, Any], bot: str = BOT_LOGIN) -> int:
    """Return count of reviews submitted by bot."""
    reviews: List[Dict[str, Any]] = []
    try:
        reviews = (((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviews", {}).get("nodes", []) or []
    except AttributeError:
        return 0
    return sum(1 for r in reviews if isinstance(r, dict) and bot in str((r.get("author") or {}).get("login") or ""))


def fetch_threads(owner: str, repo: str, pr: int, token: str) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {"nodes": []},
                    "reviews": {"nodes": []}
                }
            }
        }
    }
    nodes = merged["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    rev_nodes = merged["data"]["repository"]["pullRequest"]["reviews"]["nodes"]
    cursor = None
    first_page = True
    while True:
        body = json.dumps({"query": THREADS_QUERY, "variables": {
            "owner": owner, "repo": repo, "pr": pr, "cursor": cursor}}).encode()
        req = urllib.request.Request(
            API_URL, data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = json.loads(resp.read().decode())
        if isinstance(page.get("errors"), list) and page["errors"]:
            raise RuntimeError(f"GitHub GraphQL errors: {page['errors'][0].get('message')}")
        pr_node = ((page.get("data") or {}).get("repository") or {}).get("pullRequest")
        if pr_node is None:
            raise RuntimeError("GitHub GraphQL returned no pullRequest node (fail-closed)")
        if first_page:
            rev_nodes.extend((pr_node.get("reviews") or {}).get("nodes") or [])
            first_page = False
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
    ap.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                    help="Max adversarial review rounds before converting P1s to advisory backlog (Protocol §10.5). Default 2, 0 = unlimited.")
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
    rounds = count_bot_reviews(payload, bot=args.bot)
    blockers = find_blocking_threads(payload, bot=args.bot, fail_on=args.fail_on)
    if blockers:
        if args.max_rounds > 0 and rounds >= args.max_rounds:
            print(f"codex-gate: PASS (exceeded max review rounds limit {args.max_rounds} [actual bot reviews: {rounds}]; "
                  f"{len(blockers)} finding(s) converted to advisory backlog per Protocol §10.5):")
            for b in blockers:
                print(f"  - [ADVISORY BACKLOG] {b['path']}: {b['title']}")
            return 0
        print(f"codex-gate: BLOCKED by {len(blockers)} unresolved Codex thread(s) (round {rounds}/{args.max_rounds}):")
        for b in blockers:
            print(f"  - {b['path']}: {b['title']}")
        print("Resolve or fix the threads above, or reply with an issue backlog reference, then re-run. "
              "This check must be green (and required in branch protection) before merge.")
        return 1
    print("codex-gate: PASS (no unresolved Codex P1 threads)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
