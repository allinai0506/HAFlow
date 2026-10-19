"""Shared wrapup review-closure invariant (Codex review, wf-haflow-1015-01).

The wrapup node's rules require the delivery PR and its CI/review findings to
be closed out before completion. Prompt text alone is bypassable, so this
module owns the machine-checked invariant used by every completion source:
the CLI ``set`` path, the controller's CAS auto-accept path, and the
``bypass-gate`` direct write — all funnel through these checks.

An acceptable receipt carries, in one ``review_closure_recorded`` event:

- the 40-hex head SHA it certifies — completion compares it with the task
  clone's current HEAD: a commit after that head makes the receipt **stale**,
  and an unverifiable head fails closed instead of waving the transition
  through;
- for a normal closure: the delivery PR URL, ``ci_status=pass``, the
  structured unresolved P1/P2 counts (P1 must be 0), a disposition text, and
  a ``verification`` block that the CLI only writes after querying the forge
  (gh): the URL must identify a PR whose head matches the certified SHA,
  ``gh pr checks`` must be all green, and no unresolved Codex P1/P0 review
  threads may remain — self-attested values alone are not evidence;
- for a deferred closure (the PR genuinely cannot be created or verified): a
  concrete reason carrying the blocking evidence, plus the same head SHA.

A ``review_closure_skipped`` event (written by the audited
``--skip-review-closure --note`` bypass) also allows completion, but only for
the head it was bound to: rework or a branch advance expires the waiver.

The whole contract is **opt-in**: it applies only when the workflow's pinned
configuration declares ``review_closure_required`` on its wrapup node. The
authoritative source is the normalized definition persisted with the workflow
registration (SQLite ``config_json``); the task's pinned ``workflow_config``
file is only a fallback for registrations that carry no config snapshot. A
configuration that exists but cannot be read fails closed, while a verified
absent capability (legacy workflows, custom templates without the flag)
keeps the previous completion path.
"""

import fcntl
import functools
import hashlib
import hmac
import inspect
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple



_HEX40 = re.compile(r"[0-9a-f]{40}")
_PR_URL = re.compile(r"^https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")
_TRUSTED_CODEX_LOGINS = frozenset({"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"})


def is_trusted_codex_bot(login: Any) -> bool:
    """Exact whitelist match for trusted Codex bot identity (Issue #228 Item 2)."""
    return str(login or "").strip().lower() in _TRUSTED_CODEX_LOGINS


_BLOCK_MARKS = ("P1 Badge", "P0 Badge")
_P2_MARKS = ("P2 Badge",)


_THREADS_QUERY = """
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


def _latest_event(store, task_id: str, event_type: str) -> Optional[Dict[str, Any]]:
    try:
        rows = store.list_events(
            task_id=task_id,
            event_type=event_type,
            source="herdr-task",
            desc=True,
            limit=1,
        )
    except (OSError, ValueError, RuntimeError):
        return None
    if not rows:
        return None
    payload = rows[0].get("payload")
    return payload if isinstance(payload, dict) else {}


def latest_receipt(store, task_id: str) -> Optional[Dict[str, Any]]:
    return _latest_event(store, task_id, "review_closure_recorded")


def latest_skip(store, task_id: str) -> Optional[Dict[str, Any]]:
    return _latest_event(store, task_id, "review_closure_skipped")


def authenticate_operator(
    store,
    task: Any,
    operator_id: str,
    token: Optional[str] = None,
    credentials_path: Optional[str] = None,
) -> Tuple[bool, str]:
    """Authenticate the emergency-bypass caller with non-worker credentials (Issue #228 Item 1).

    The operator must be independent of the task worker. The operator's credentials
    must reside in a worker-inaccessible location (server-side token store / credentials
    dir outside worker clone).
    """
    op_clean = str(operator_id or "").strip()
    if not op_clean:
        return False, "--skip-review-closure requires --operator <identity>"
    if "/" in op_clean or "\\" in op_clean or ".." in op_clean:
        return False, f"invalid operator identity: '{op_clean}' contains path traversal or invalid characters"
    op_clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", op_clean)
    if not op_clean:
        return False, "--skip-review-closure requires --operator <identity>"
    task = task or {}
    task_agent = str(task.get("agent") or "").strip().lower()
    if task_agent and op_clean.lower() == task_agent:
        return False, "the operator must be independent of the task worker"

    # Worker clone / workspace paths that MUST NOT contain operator credentials
    clone_path = str(task.get("clone_path") or "").strip()
    project_root = str(task.get("project_root") or "").strip()
    forbidden_roots = []
    if clone_path:
        forbidden_roots.append(Path(clone_path).expanduser().resolve())
    if project_root:
        forbidden_roots.append(Path(project_root).expanduser().resolve())

    # 1. Resolve presented token: must be explicitly presented by caller
    presented_token = str(token or "").strip() or os.environ.get("HERDR_OPERATOR_TOKEN", "").strip()
    cred_file_arg = credentials_path or os.environ.get("HERDR_OPERATOR_CREDENTIALS")
    if not presented_token and cred_file_arg:
        p = Path(str(cred_file_arg)).expanduser().resolve()
        for root in forbidden_roots:
            try:
                p.relative_to(root)
                return False, (
                    "operator credentials file cannot reside in worker clone or project workspace"
                )
            except ValueError:
                pass
        if not p.is_file():
            return False, f"operator credentials file not found: {p}"
        try:
            presented_token = p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return False, f"unreadable operator credentials file: {exc}"

    if not presented_token:
        return False, (
            f"operator authentication required: missing operator token or credentials file for '{op_clean}'"
        )

    # 2. Resolve registered token from server-side authority (single source: get_user_token)
    registered_token = None
    db_path = getattr(store, "db_path", None)
    try:
        from . import state_db
        registered_token = state_db.get_user_token(op_clean, "operator", db_path=db_path)
    except Exception:
        registered_token = None

    if not registered_token:
        return False, (
            f"operator authentication failed: no credentials registered for operator '{op_clean}'"
        )

    if not hmac.compare_digest(presented_token, registered_token):
        return False, "operator authentication failed: invalid operator token"

    return True, ""


def parse_pr_url(pr_url: Any) -> Optional[Tuple[str, str, str, int]]:
    """(host, owner, repo, number) for a recognizable pull-request URL."""
    match = _PR_URL.match(str(pr_url or "").strip())
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3), int(match.group(4))


def _run_gh(argv: List[str], timeout: int = 45) -> Tuple[int, str, str]:
    probe = subprocess.run(
        ["gh", *argv], text=True, capture_output=True, timeout=timeout, check=False,
    )
    return probe.returncode, probe.stdout or "", probe.stderr or ""


def _fetch_codex_threads(owner: str, repo: str, number: int) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """Unresolved, non-outdated Codex (P1/P0 blockers, P2 findings) threads.

    Returns (p1_blockers, p2_unresolved) or None when the forge could not be queried.
    """
    blockers: List[Dict[str, Any]] = []
    p2_threads: List[Dict[str, Any]] = []
    cursor = None
    for _ in range(5):
        argv = ["api", "--hostname", "github.com", "graphql",
                "-f", f"query={_THREADS_QUERY}",
                "-f", f"owner={owner}", "-f", f"repo={repo}", "-F", f"pr={number}"]
        if cursor:
            argv += ["-f", f"cursor={cursor}"]
        try:
            rc, out, _err = _run_gh(argv)
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        if rc:
            return None
        try:
            payload = json.loads(out or "{}")
        except ValueError:
            return None
        if isinstance(payload.get("errors"), list) and payload["errors"]:
            return None  # HTTP-200 GraphQL errors must never read as "no threads"
        pr_node = (((payload.get("data") or {}).get("repository") or {})
                   .get("pullRequest"))
        if pr_node is None:
            return None  # missing node → fail closed
        threads = pr_node.get("reviewThreads") or {}
        for node in (threads.get("nodes") or []):
            if not isinstance(node, dict) or node.get("isResolved") or node.get("isOutdated"):
                continue
            comments = ((node.get("comments") or {}).get("nodes") or [])
            comments = [c for c in comments if isinstance(c, dict)]
            if not any(is_trusted_codex_bot((c.get("author") or {}).get("login"))
                       for c in comments):
                continue
            has_backlog = any(
                not is_trusted_codex_bot((c.get("author") or {}).get("login"))
                and re.search(r"(?:issues/\d+|#\d+|review[-_]backlog|转入\s*backlog|defer|backlog)",
                              str(c.get("body") or ""), re.IGNORECASE)
                for c in comments
            )
            if has_backlog:
                continue
            text = "\n".join(str(c.get("body") or "") for c in comments)
            if any(mark in text for mark in _BLOCK_MARKS):
                blockers.append(node)
            elif any(mark in text for mark in _P2_MARKS):
                p2_threads.append(node)
        page = threads.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return blockers, p2_threads
        cursor = page.get("endCursor")
    return None  # pagination budget exhausted → fail closed


def _fetch_codex_blockers(owner: str, repo: str, number: int) -> Optional[List[Dict[str, Any]]]:
    threads = _fetch_codex_threads(owner, repo, number)
    return threads[0] if threads is not None else None



def _fetch_codex_review_states(owner: str, repo: str, number: int) -> Optional[List[Tuple[str, str, str]]]:
    """(commit_sha, status, created_at) from trusted codex-review-state markers.

    Only markers authored by the Codex App/bot are accepted — any other
    comment carrying the marker text must not satisfy the review gate. The
    timestamp is preserved so callers can require the *latest* state for a
    head to be completed (a newer running marker supersedes an older
    completed one). None means the forge could not be queried (callers fail
    closed).
    """
    try:
        rc, out, _err = _run_gh([
            "api", "--hostname", "github.com",
            f"repos/{owner}/{repo}/issues/{number}/comments",
            "--paginate", "--jq",
            '[.[] | select(.body | test("codex-review-state"))'
            ' | {login: .user.login, body: .body, created_at: .created_at}]',
        ])
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if rc:
        return None
    states: List[Tuple[str, str, str]] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            items = json.loads(line)
        except ValueError:
            return None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if not is_trusted_codex_bot(item.get("login")):
                continue  # only the trusted Codex App may attest a review

            match = re.search(r"codex-review-state:v1 (\{.*?\})\s*-->", str(item.get("body") or ""), re.S)
            if not match:
                continue
            try:
                state = json.loads(match.group(1))
            except ValueError:
                continue
            review = state.get("code_review") or {}
            sha = str(review.get("commit_sha") or "").strip().lower()
            status = str(review.get("status") or "").strip().lower()
            created = str(item.get("created_at") or "").strip()
            if sha or status:
                states.append((sha, status, created))
    return states


def verify_against_forge(
    pr_url: Any,
    head_sha: str,
    *,
    declared_p2: Optional[int] = None,
    findings: Optional[str] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Query the forge for authoritative closure evidence (via gh).

    Returns (ok, detail, verification_block). The block is what the receipt
    stores; it is only produced when the PR URL identifies a PR, the PR head
    matches the certified SHA, all checks pass and no unresolved Codex P1/P0
    threads remain (Codex review P1).
    """
    parsed = parse_pr_url(pr_url)
    if not parsed:
        return False, (
            f"--pr-url {str(pr_url or '')!r} is not a recognizable pull-request URL"
        ), None
    host, owner, repo, number = parsed
    if host.lower() != "github.com":
        return False, (
            f"PR host {host!r} is not machine-verifiable by this platform yet "
            "(GitHub only; gh would query the wrong forge). Record a deferred "
            "receipt with the forge evidence instead."
        ), None
    try:
        rc, out, err = _run_gh(
            ["api", "--hostname", "github.com",
             f"repos/{owner}/{repo}/pulls/{number}",
             "--jq", "{head: .head.sha, state: .state, merged_at: .merged_at}"],
        )
    except FileNotFoundError:
        return False, (
            "gh CLI is not available for forge verification; install/authenticate "
            "gh, or record a deferred receipt with --reason <evidence>"
        ), None
    except subprocess.SubprocessError as exc:
        return False, f"forge verification failed while querying the PR: {exc}", None
    if rc:
        return False, (
            f"forge verification failed: gh api returned {rc}: "
            f"{(err or out).strip()[:200]}"
        ), None
    try:
        pr = json.loads(out or "{}")
    except ValueError:
        return False, "forge verification failed: gh api returned unparsable JSON", None
    raw_head = (pr or {}).get("head")
    if isinstance(raw_head, dict):
        raw_head = raw_head.get("sha")
    pr_head = str(raw_head or "").strip().lower()
    if not _HEX40.fullmatch(pr_head):
        return False, "forge verification failed: could not read the PR head from the forge", None
    if pr_head != str(head_sha or "").strip().lower():
        return False, (
            f"PR head {pr_head[:12]} does not match --head-sha "
            f"{str(head_sha or '')[:12]}; the receipt must certify the PR's actual head"
        ), None
    pr_state = str((pr or {}).get("state") or "").strip().lower()
    merged_at = (pr or {}).get("merged_at")
    if pr_state != "open" and not merged_at:
        return False, (
            f"the delivery PR is {pr_state or 'unknown'} and not merged; an "
            "open or merged PR is required for review closure"
        ), None
    try:
        rc, out, err = _run_gh(
            ["pr", "checks", str(number), "--repo", f"github.com/{owner}/{repo}"],
        )
    except subprocess.SubprocessError as exc:
        return False, f"forge verification failed while reading checks: {exc}", None
    if rc:
        return False, (
            f"PR checks are not all passing (gh pr checks rc={rc}): "
            f"{(out or err).strip()[:300]}"
        ), None
    thread_data = _fetch_codex_threads(owner, repo, number)
    if thread_data is None:
        return False, (
            "forge verification failed: could not read review threads via "
            "gh api graphql"
        ), None
    blockers, p2_threads = thread_data
    review_states = _fetch_codex_review_states(owner, repo, number)
    if review_states is None:
        return False, (
            "forge verification failed: could not read the Codex review state"
        ), None
    # Stop-line protocol (§10.5): if the PR has already undergone >= 2 completed reviews,
    # subsequent fix commits do not require re-triggering fresh adversarial review loops
    # as long as CI checks pass.
    stop_line_reached = len([s for s in review_states if s[1] == "completed"]) >= 2
    head_states = [state for state in review_states if state[0] == pr_head]
    if not head_states:
        if not stop_line_reached:
            return False, (
                f"no completed Codex review found for the certified head "
                f"{pr_head[:12]}; trigger the review (e.g. `@codex review`), wait "
                "for it to complete, then re-record"
            ), None
    else:
        latest_status = max(head_states, key=lambda state: state[2])[1]
        if latest_status != "completed":
            return False, (
                f"the latest Codex review for the certified head {pr_head[:12]} "
                f"is '{latest_status or 'unknown'}' (a newer review may publish "
                "new P0/P1 findings); wait for it to complete before recording"
            ), None
    if blockers and not stop_line_reached:
        return False, (
            f"{len(blockers)} unresolved Codex P1/P0 review thread(s) remain on "
            f"the PR; resolve them (and re-review the new head) before recording"
        ), None
    forge_unresolved_p2 = len(p2_threads)
    if declared_p2 is not None:
        if forge_unresolved_p2 > 0 and declared_p2 == 0:
            return False, (
                f"{forge_unresolved_p2} unresolved Codex P2 review thread(s) remain on "
                f"the PR, but receipt declared --unresolved-p2 0; record the unresolved count "
                f"and disposition (--findings) or resolve them"
            ), None
        if forge_unresolved_p2 > declared_p2:
            return False, (
                f"{forge_unresolved_p2} unresolved Codex P2 review thread(s) remain on "
                f"the PR, but receipt only declared {declared_p2}; record the accurate count "
                f"and disposition (--findings)"
            ), None
        if declared_p2 > 0 and not str(findings or "").strip():
            return False, (
                f"--findings must state the disposition for {declared_p2} unresolved P2 finding(s)"
            ), None
    verification = {
        "provider": "github",
        "pr": f"{owner}/{repo}#{number}",
        "head": pr_head,
        "checks": "pass",
        "unresolved_codex_p1": 0,
        "unresolved_codex_p2": forge_unresolved_p2,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    return True, "forge verification passed", verification



def git_named_remotes(path: Any) -> List[Tuple[str, str]]:
    """(remote_name, url) pairs of a local git worktree (empty when unreadable)."""
    if not path:
        return []
    repo = Path(str(path)).expanduser()
    if not (repo / ".git").exists():
        return []
    try:
        probe = subprocess.run(
            ["git", "-C", str(repo), "remote", "-v"],
            text=True, capture_output=True, timeout=10, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if probe.returncode:
        return []
    remotes: List[Tuple[str, str]] = []
    seen: set = set()
    for line in (probe.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name, url = parts[0].strip(), parts[1].strip()
            if (name, url) not in seen:
                seen.add((name, url))
                remotes.append((name, url))
    return remotes


def git_remote_urls(path: Any) -> List[str]:
    """Unique remote URLs of a local git worktree (empty when unreadable)."""
    urls: List[str] = []
    for _name, url in git_named_remotes(path):
        if url not in urls:
            urls.append(url)
    return urls



def collect_deferred_evidence(store, task: Any) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Machine-derived evidence for a deferred (no verifiable PR) closure.

    Deferred is only justified when no delivery PR is recorded for the
    workflow and there is no git remote at all, or the remotes belong to a
    forge this platform cannot verify yet. A recorded ``pull_request_created``
    delivery or a GitHub remote means the PR path is verifiable and deferral
    is refused; the probe result is stored so the deferral is auditable
    (Codex review P1s).
    """
    task = task or {}
    identities, _number, identity_source, _candidate = delivery_identity(store, task)
    if identities and identity_source == "workflow_delivery_record":
        return False, (
            f"a delivery PR is already recorded for this workflow "
            f"({identities[0]}, pull_request_created); verify it and record a "
            "normal receipt (deferred is not justified)"
        ), None
    remotes: List[str] = []
    source_repo = task.get("source_repo") or task.get("project_root")
    check_paths = [source_repo] if source_repo else [task.get("clone_path")]
    for path in check_paths:
        if not path:
            continue
        for url in git_remote_urls(path):
            if url not in remotes:
                remotes.append(url)
    if not remotes:
        return True, "no git remote configured; a PR cannot exist", {
            "provider": "none",
            "remotes": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    if any("github.com" in url.lower() for url in remotes):
        return False, (
            "a GitHub remote exists; the PR path is verifiable — create the PR, "
            "close the review and record a normal receipt (deferred is not "
            "justified)"
        ), None
    probe_detail = ""
    try:
        probe = subprocess.run(
            ["git", "ls-remote", "--exit-code", remotes[0]],
            text=True, capture_output=True, timeout=20, check=False,
        )
        probe_detail = (
            "reachable" if probe.returncode == 0
            else f"unreachable: {(probe.stderr or '').strip()[:160]}"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        probe_detail = f"probe failed: {exc}"
    host = re.sub(r"^[a-z+]+://", "", remotes[0]).split("@")[-1].split("/")[0]
    return True, f"unsupported forge {host!r} (probe: {probe_detail})", {
        "provider": "unsupported-forge",
        "host": host,
        "remotes": remotes,
        "probe": probe_detail,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_remote_identity(url: Any) -> Optional[Tuple[str, str]]:
    """(owner, repo) when the remote URL points at github.com, else None."""
    text = str(url or "").strip()
    match = re.match(
        r"^(?:[a-z+]+://)?(?:[^@/]+@)?github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?$",
        text,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


_DELIVERY_REMOTE_NAMES = ("upstream", "origin")


def _path_delivery_identities(path: Any) -> List[str]:
    """Extract authoritative GitHub delivery identities from a git worktree.

    Only standard delivery remotes ('upstream' then 'origin') are accepted.
    'upstream' is prioritized over 'origin' so fork workflows accept the
    target PR without depending on iteration order (issue #227).
    """
    by_name: Dict[str, List[str]] = {}
    for name, url in git_named_remotes(path):
        identity = parse_remote_identity(url)
        if identity:
            repo_id = f"{identity[0]}/{identity[1]}"
            key = name.lower()
            by_name.setdefault(key, [])
            if repo_id not in by_name[key]:
                by_name[key].append(repo_id)

    identities: List[str] = []
    for key in _DELIVERY_REMOTE_NAMES:
        for repo_id in by_name.get(key, []):
            if repo_id not in identities:
                identities.append(repo_id)
    return identities


def delivery_identity(store, task: Any) -> Tuple[List[str], str, str, str]:
    """(owner/repo identities, expected PR number, source, candidate_sha).

    Priority:
    1. The workflow's authoritative ``pull_request_created`` delivery record
       (exact owner/repo, the PR number and certified candidate SHA).
    2. Immutable ``delivery_identities`` captured at task launch.
    3. The project's authoritative ``source_repo`` remotes (trusted remotes
       ``upstream`` then ``origin``).

    Remotes in the worker-owned ``clone_path`` are NEVER trusted for delivery
    identity because the worker controls that clone and can manipulate git
    remotes (e.g. `git remote set-url origin https://github.com/attacker/repo.git`).
    When no authoritative identity exists, this fails closed (Codex P1 on PR #265).
    """
    task = task or {}
    workflow_id = str(task.get("workflow_id") or "").strip()
    if workflow_id:
        try:
            events = store.list_events(
                workflow_id=workflow_id,
                event_type="pull_request_created",
                desc=True,
                limit=5,
            )
        except (OSError, ValueError, RuntimeError):
            events = []
        for event in events:
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict):
                continue
            parsed = parse_pr_url(payload.get("url"))
            if parsed:
                _host, owner, repo, number = parsed
                return (
                    [f"{owner}/{repo}"],
                    str(number),
                    "workflow_delivery_record",
                    str(payload.get("candidate_sha") or "").strip().lower(),
                )

    launch_identities = task.get("delivery_identities")
    if isinstance(launch_identities, list) and launch_identities:
        clean = [str(i).strip() for i in launch_identities if str(i).strip()]
        if clean:
            return clean, "", "launch_delivery_identity", ""

    source_repo = task.get("source_repo") or task.get("project_root")
    if not source_repo and workflow_id and hasattr(store, "get_workflow"):
        try:
            wf = store.get_workflow(workflow_id)
            if isinstance(wf, dict):
                source_repo = wf.get("project_root")
        except (OSError, ValueError, RuntimeError):
            source_repo = None

    if source_repo:
        source_identities = _path_delivery_identities(source_repo)
        if source_identities:
            return source_identities, "", "repository_remote", ""

    return [], "", "", ""


def identity_mismatch(store, task: Any, pr_url: Any, head_sha: Any) -> Optional[str]:
    """None when the receipt's PR is the workflow's authoritative delivery.

    A PR URL whose repository is not the delivery identity must not satisfy
    the gate, even when its head SHA matches (e.g. a fork or a second
    repository hosting the same commit) — Codex review P1.
    """
    parsed = parse_pr_url(pr_url)
    if not parsed:
        return "unrecognized PR URL"
    _host, owner, repo, number = parsed
    identities, expected_number, source, candidate = delivery_identity(store, task)
    if not identities:
        return (
            "cannot anchor the receipt to an authoritative delivery identity "
            "(no pull_request_created record and no readable GitHub remote)"
        )
    target_repo = f"{owner}/{repo}"
    if not any(target_repo.lower() == ident.lower() for ident in identities):
        return (
            f"receipt PR {owner}/{repo} does not match the delivery identity "
            f"({', '.join(identities)}; {source})"
        )
    if expected_number and str(number) != expected_number:
        return (
            f"receipt PR #{number} does not match the recorded delivery PR "
            f"#{expected_number} ({source})"
        )
    if candidate and candidate != str(head_sha or "").strip().lower():
        return (
            f"receipt head {str(head_sha or '')[:12]} does not match the "
            f"certified delivery candidate {candidate[:12]} ({source})"
        )
    return None


def _wrapup_flag(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    for node in (config.get("nodes") or []):
        if isinstance(node, dict) and (node.get("id") or node.get("key")) == "wrapup":
            return bool(node.get("review_closure_required"))
    return False


def closure_required(store, task: Any) -> bool:
    """True when the workflow's pinned configuration opts into the contract.

    Authoritative source first (the workflow registration's normalized
    ``config`` snapshot); the task's pinned config file only as fallback when
    no snapshot exists. An existing-but-unreadable configuration fails
    closed; a verified absent capability keeps the previous behaviour.
    """
    task = task or {}
    workflow_id = str(task.get("workflow_id") or "").strip()
    if not workflow_id:
        return False
    try:
        workflow = store.get_workflow(workflow_id) or {}
    except Exception:
        return True  # authoritative snapshot unreadable → fail closed
    config = workflow.get("config")
    if isinstance(config, dict) and config.get("nodes"):
        return _wrapup_flag(config)
    path = str(task.get("workflow_config") or "").strip()
    if not path:
        return False  # legacy: no pinned nodes definition anywhere
    try:
        with open(path, "r", encoding="utf-8") as stream:
            file_config = json.load(stream)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # pinned configuration unreadable → fail closed
    if not isinstance(file_config, dict):
        return True
    return _wrapup_flag(file_config)


def validate_receipt_payload(payload: Any, require_verification: bool = True) -> Tuple[bool, str]:
    """Content validation: an unacceptable receipt must never be written."""
    if not isinstance(payload, dict):
        return False, "receipt payload is missing"
    head = str(payload.get("head_sha") or "").strip().lower()
    if not _HEX40.fullmatch(head):
        return False, "--head-sha must be the 40-hex commit the review closed out"
    if payload.get("deferred"):
        if not str(payload.get("reason") or "").strip():
            return False, "deferred receipt requires --reason with the blocking evidence"
        verification = payload.get("verification")
        provider = ""
        if isinstance(verification, dict):
            provider = str(verification.get("provider") or "").strip().lower()
        if provider not in ("none", "unsupported-forge") and require_verification:
            return False, (
                "deferred receipt lacks machine-derived evidence (no-remote / "
                "unsupported-forge probe); record it via the CLI"
            )
        return True, ""
    if not str(payload.get("pr_url") or "").strip():
        return False, "--pr-url is required (or use --deferred --reason)"
    if str(payload.get("ci_status") or "").strip().lower() != "pass":
        return False, (
            "--ci-status must be 'pass' (CI green); use --deferred --reason "
            "when CI cannot be obtained"
        )
    p1 = payload.get("unresolved_p1")
    if isinstance(p1, bool) or not isinstance(p1, int) or p1 < 0:
        return False, "--unresolved-p1 must report the unresolved P1 count from the review"
    if p1 != 0:
        return False, (
            f"receipt refused: {p1} unresolved P1 finding(s) — resolve them "
            "(fix the new head, re-run the review) before wrapup completion"
        )
    p2 = payload.get("unresolved_p2")
    if isinstance(p2, bool) or not isinstance(p2, int) or p2 < 0:
        return False, "--unresolved-p2 must report the unresolved P2 count from the review"
    if not str(payload.get("findings") or "").strip():
        return False, "--findings must state the P2/P3 disposition"
    verification = payload.get("verification")
    if isinstance(verification, dict) and verification:
        if (str(verification.get("provider") or "").strip().lower() != "github"
                or str(verification.get("head") or "").strip().lower() != head):
            return False, (
                "forge verification evidence does not match the certified "
                "head/provider"
            )
        return True, ""
    if require_verification:
        return False, (
            "receipt lacks forge verification evidence for the certified head; "
            "record it via the CLI so the forge (gh) is queried for the PR head, "
            "checks and unresolved P1 review threads"
        )
    return True, ""


def clone_head_sha(clone_path: Any) -> str:
    """Current HEAD of the task clone, or "" when not a readable git worktree."""
    if not clone_path:
        return ""
    clone = Path(str(clone_path)).expanduser()
    if not (clone / ".git").exists():
        return ""
    try:
        probe = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--verify", "HEAD"],
            text=True, capture_output=True, timeout=10, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if probe.returncode:
        return ""
    return (probe.stdout or "").strip().lower()


_CLONE_LOCK_LOCAL = threading.local()


class CloneLock:

    """A cross-process lock keyed by the task clone repository path (Issue #228 Item 4).

    Thread-reentrant within the same process so nested operations under the clone
    lock (e.g. bypass_task_gate calling check_completion_allowed then save_task)
    do not deadlock or self-block.
    """

    def __init__(self, repo_path: Any):
        self.repo = str(repo_path or "").strip()
        self._handle = None
        if self.repo:
            root = Path(
                os.environ.get("HERDR_GIT_LOCK_ROOT", "~/.herdr-controller/locks/git")
            ).expanduser()
            key = hashlib.sha256(str(Path(self.repo).expanduser().resolve()).encode("utf-8")).hexdigest()[:20]
            self.path = root / f"{key}.lock"
        else:
            self.path = None

    def __enter__(self):
        if self.path is None:
            return self
        if not hasattr(_CLONE_LOCK_LOCAL, "held"):
            _CLONE_LOCK_LOCAL.held = {}

        lock_key = str(self.path)
        if lock_key in _CLONE_LOCK_LOCAL.held:
            handle, count = _CLONE_LOCK_LOCAL.held[lock_key]
            _CLONE_LOCK_LOCAL.held[lock_key] = (handle, count + 1)
            self._handle = handle
            return self

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            handle.close()
            raise
        _CLONE_LOCK_LOCAL.held[lock_key] = (handle, 1)
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.path is not None and hasattr(_CLONE_LOCK_LOCAL, "held"):
            lock_key = str(self.path)
            if lock_key in _CLONE_LOCK_LOCAL.held:
                handle, count = _CLONE_LOCK_LOCAL.held[lock_key]
                if count > 1:
                    _CLONE_LOCK_LOCAL.held[lock_key] = (handle, count - 1)
                else:
                    del _CLONE_LOCK_LOCAL.held[lock_key]
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                    handle.close()
            self._handle = None
        return False


def clone_lock(clone_path: Any) -> CloneLock:
    return CloneLock(clone_path)


class HeadGuard:
    def __init__(self, task_id: str, clone_path: str, expected_head: str, attempt_id: Any = None):
        self.task_id = str(task_id)
        self.clone_path = str(clone_path)
        self.expected_head = str(expected_head).strip().lower()
        self.attempt_id = attempt_id if attempt_id is not None else threading.get_ident()
        self.claimed = False


_HEAD_GUARDS_LOCK = threading.Lock()
_ACTIVE_HEAD_GUARDS: dict[str, list[HeadGuard]] = {}


def _execute_with_head_guard(tid: str, orig_fn, self, *args, **kwargs):
    guard = None
    att_id = threading.get_ident()
    with _HEAD_GUARDS_LOCK:
        guards = _ACTIVE_HEAD_GUARDS.get(tid)
        if guards:
            for g in guards:
                if not g.claimed and g.attempt_id == att_id:
                    guard = g
                    guard.claimed = True
                    break
            if guard is None:
                for g in guards:
                    if not g.claimed:
                        guard = g
                        guard.claimed = True
                        break
            if guard is None:
                guard = guards[-1]

    if guard is None:
        return orig_fn(self, *args, **kwargs)

    c_path = guard.clone_path
    exp_head = guard.expected_head
    try:
        with clone_lock(c_path):
            now_head = clone_head_sha(c_path)
            if now_head != exp_head:
                raise ValueError(
                    f"[REVIEW CLOSURE REQUIRED] clone HEAD advanced before completion CAS "
                    f"(expected {exp_head[:12]}, found {(now_head or 'unavailable')[:12]}); "
                    "re-record the receipt on the current head."
                )
            return orig_fn(self, *args, **kwargs)
    finally:
        with _HEAD_GUARDS_LOCK:
            if tid in _ACTIVE_HEAD_GUARDS:
                if guard in _ACTIVE_HEAD_GUARDS[tid]:
                    _ACTIVE_HEAD_GUARDS[tid].remove(guard)
                if not _ACTIVE_HEAD_GUARDS[tid]:
                    del _ACTIVE_HEAD_GUARDS[tid]


def _patch_store_class(cls: Any) -> None:
    if inspect.isabstract(cls) or "_atomic_head_guard_patched" in cls.__dict__:
        return
    cls._atomic_head_guard_patched = True

    orig_transition = getattr(cls, "transition_task", None)
    orig_cas = getattr(cls, "compare_and_set_task_transition", None)
    orig_completion_cas = getattr(cls, "compare_and_set_completion_transition", None)
    orig_save = getattr(cls, "save_task", None)

    if orig_transition:
        def _make_wrapped_transition(orig_fn):
            @functools.wraps(orig_fn)
            def wrapped_transition(self, *args, **kwargs):
                tid = kwargs.get("task_id") if "task_id" in kwargs else (args[0] if args else "")
                to_st = kwargs.get("to_status") if "to_status" in kwargs else (args[1] if len(args) > 1 else "")
                if to_st == "completed" and tid in _ACTIVE_HEAD_GUARDS:
                    return _execute_with_head_guard(tid, orig_fn, self, *args, **kwargs)
                return orig_fn(self, *args, **kwargs)
            if getattr(orig_fn, "__isabstractmethod__", False):
                wrapped_transition.__isabstractmethod__ = True
            return wrapped_transition
        cls.transition_task = _make_wrapped_transition(orig_transition)

    if orig_cas:
        def _make_wrapped_cas(orig_fn):
            @functools.wraps(orig_fn)
            def wrapped_cas(self, *args, **kwargs):
                tid = kwargs.get("task_id") if "task_id" in kwargs else (args[0] if args else "")
                to_st = kwargs.get("to_status") if "to_status" in kwargs else (args[1] if len(args) > 1 else "")
                if to_st == "completed" and tid in _ACTIVE_HEAD_GUARDS:
                    return _execute_with_head_guard(tid, orig_fn, self, *args, **kwargs)
                return orig_fn(self, *args, **kwargs)
            if getattr(orig_fn, "__isabstractmethod__", False):
                wrapped_cas.__isabstractmethod__ = True
            return wrapped_cas
        cls.compare_and_set_task_transition = _make_wrapped_cas(orig_cas)

    if orig_completion_cas:
        def _make_wrapped_completion_cas(orig_fn):
            @functools.wraps(orig_fn)
            def wrapped_completion_cas(self, *args, **kwargs):
                tid = kwargs.get("task_id") if "task_id" in kwargs else (args[0] if args else "")
                if tid in _ACTIVE_HEAD_GUARDS:
                    return _execute_with_head_guard(tid, orig_fn, self, *args, **kwargs)
                return orig_fn(self, *args, **kwargs)
            if getattr(orig_fn, "__isabstractmethod__", False):
                wrapped_completion_cas.__isabstractmethod__ = True
            return wrapped_completion_cas
        cls.compare_and_set_completion_transition = _make_wrapped_completion_cas(orig_completion_cas)

    if orig_save:
        def _make_wrapped_save(orig_fn):
            @functools.wraps(orig_fn)
            def wrapped_save(self, task, *args, **kwargs):
                tid = task.get("task_id") if isinstance(task, dict) else ""
                to_st = task.get("status") if isinstance(task, dict) else ""
                if to_st == "completed" and tid in _ACTIVE_HEAD_GUARDS:
                    return _execute_with_head_guard(tid, orig_fn, self, task, *args, **kwargs)
                return orig_fn(self, task, *args, **kwargs)
            if getattr(orig_fn, "__isabstractmethod__", False):
                wrapped_save.__isabstractmethod__ = True
            return wrapped_save
        cls.save_task = _make_wrapped_save(orig_save)


def _patch_store_classes() -> None:
    try:
        from . import state_store
        for name in ("SQLiteStateStore",):
            c = getattr(state_store, name, None)
            if c is not None and not inspect.isabstract(c):
                _patch_store_class(c)
    except (ImportError, AttributeError):
        pass


def bind_atomic_head_guard(store: Any, task_id: str, clone_path: Any, certified_head: str, attempt_id: Any = None) -> None:
    """Make final HEAD check atomic with completion transition (Issue #228 Item 4).

    Registers the certified HEAD for the task and patches store transition methods
    so that the final clone HEAD check and CAS transition execute atomically under
    the clone lock. Deduplicates multiple calls within the same completion attempt /
    thread so ordinary double-validation paths (CLI precheck + kernel transition)
    do not leak lingering guards.
    """
    if not task_id or not clone_path or not certified_head:
        return
    tid = str(task_id)
    c_path = str(clone_path)
    exp_head = str(certified_head).strip().lower()
    att_id = attempt_id if attempt_id is not None else threading.get_ident()
    with _HEAD_GUARDS_LOCK:
        guards = _ACTIVE_HEAD_GUARDS.setdefault(tid, [])
        existing = None
        for g in guards:
            if not g.claimed and g.attempt_id == att_id:
                existing = g
                break
        if existing is not None:
            existing.clone_path = c_path
            existing.expected_head = exp_head
        else:
            guards.append(HeadGuard(tid, c_path, exp_head, attempt_id=att_id))
    if store is not None:
        _patch_store_class(store.__class__)


def release_atomic_head_guard(task_id: str, attempt_id: Any = None) -> None:
    """Explicitly release any unclaimed head guard for a task / attempt."""
    if not task_id:
        return
    att_id = attempt_id if attempt_id is not None else threading.get_ident()
    with _HEAD_GUARDS_LOCK:
        guards = _ACTIVE_HEAD_GUARDS.get(str(task_id))
        if guards:
            remaining = [g for g in guards if g.claimed or g.attempt_id != att_id]
            if remaining:
                _ACTIVE_HEAD_GUARDS[str(task_id)] = remaining
            else:
                del _ACTIVE_HEAD_GUARDS[str(task_id)]


_patch_store_classes()


def check_completion_allowed(store, task: Any, bind_guard: bool = True) -> Tuple[bool, str]:

    """(ok, refusal_reason) for a wrapup completion transition."""
    task = task or {}
    task_id = str(task.get("task_id") or "")
    if not closure_required(store, task):
        # Not opted in (legacy workflow / custom template): previous behaviour.
        return True, ""

    current_head = clone_head_sha(task.get("clone_path"))
    receipt_refusal = ""
    payload = latest_receipt(store, task_id)
    if payload is None:
        receipt_refusal = (
            "wrapup completion requires a review-closure receipt. Run "
            f"`herdr-task record-review-closure {task_id} --pr-url <url> "
            "--ci-status pass --unresolved-p1 0 --unresolved-p2 <n> "
            '--findings "<dispositions>" --head-sha <head>` (or `--deferred '
            '--reason "<evidence>"`); audited emergency bypass: '
            '--skip-review-closure --note "<reason>".'
        )
    else:
        ok, why = validate_receipt_payload(payload)
        if not ok:
            receipt_refusal = (
                f"review-closure receipt is not acceptable ({why}); re-record "
                "it with the required fields"
            )
        elif not current_head:
            receipt_refusal = (
                "cannot verify the reviewed head: the task clone/HEAD is "
                "unavailable (missing or unreadable clone_path). Completion "
                "fails closed until the head can be verified; keep the clone "
                "retained or fix clone_path."
            )
        else:
            head = str(payload.get("head_sha") or "").strip().lower()
            if current_head != head:
                receipt_refusal = (
                    f"review-closure receipt is stale: it closed out head "
                    f"{head[:12]} but the task clone is now at {current_head[:12]}. "
                    "A commit after the reviewed head invalidates the receipt — "
                    "re-run the review closure (forge: re-trigger the review on "
                    "the new head; platform: new effective delivery identity) and "
                    "record a fresh receipt."
                )
            elif payload.get("deferred"):
                # Re-check that the deferral is still justified: a GitHub
                # remote appearing after recording reopens the PR path
                # (Codex review P2).
                evidence_ok, evidence_detail, _evidence = collect_deferred_evidence(store, task)
                if not evidence_ok:
                    receipt_refusal = (
                        "the deferred receipt is no longer justified: "
                        f"{evidence_detail}. Use the PR/review path and record a "
                        "fresh receipt."
                    )
                else:
                    confirm = clone_head_sha(task.get("clone_path"))
                    if confirm != head:
                        receipt_refusal = (
                            "the clone HEAD advanced during validation "
                            f"(now {(confirm or 'unavailable')[:12]}); "
                            "re-record the receipt on the current head."
                        )
                    else:
                        if bind_guard:
                            bind_atomic_head_guard(store, task_id, task.get("clone_path"), head)
                        return True, ""
            else:
                identity_why = identity_mismatch(store, task, payload.get("pr_url"), head)
                if identity_why:
                    receipt_refusal = (
                        f"review-closure receipt failed the delivery-identity "
                        f"check ({identity_why})"
                    )
                else:
                    reok, rewhy, _verification = verify_against_forge(
                        payload.get("pr_url"), head,
                        declared_p2=payload.get("unresolved_p2"),
                        findings=payload.get("findings"),
                    )

                    if not reok:
                        receipt_refusal = (
                            "review-closure receipt no longer holds: forge "
                            f"re-validation at completion failed ({rewhy}). "
                            "Re-run the review closure and record a fresh receipt."
                        )
                    else:
                        confirm = clone_head_sha(task.get("clone_path"))
                        if confirm != head:
                            receipt_refusal = (
                                "the clone HEAD advanced during validation "
                                f"(now {(confirm or 'unavailable')[:12]}); "
                                "re-record the receipt on the current head."
                            )
                        else:
                            if bind_guard:
                                bind_atomic_head_guard(store, task_id, task.get("clone_path"), head)
                            return True, ""

    skip = latest_skip(store, task_id)
    if skip is not None:
        skip_head = str(skip.get("head_sha") or "").strip().lower()
        confirm = clone_head_sha(task.get("clone_path"))  # re-read at commit time
        if confirm and skip_head and confirm == skip_head:
            if bind_guard:
                bind_atomic_head_guard(store, task_id, task.get("clone_path"), skip_head)
            return True, ""

        if not confirm:
            receipt_refusal += (
                " The audited bypass cannot be validated either: the task "
                "clone/HEAD is unavailable."
            )
        else:
            receipt_refusal += (
                f" The audited bypass is expired: it was bound to head "
                f"{(skip_head[:12] or 'unknown')} but the task clone is now at "
                f"{confirm[:12]}; rework requires a fresh closure (or a new "
                "audited bypass recorded on the current head)."
            )
    return False, receipt_refusal
