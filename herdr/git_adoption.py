"""Git finalize head-adoption classifier (pure, no I/O).

Decides whether an empty-index ``herdr-task commit`` may adopt the clone's
existing HEAD as the task deliverable commit instead of failing with exit 3.

Verdicts: ``adopt`` | ``empty`` | ``refused``. All inputs are explicit so the
decision is deterministic and unit-testable; git I/O stays in ``bin/herdr-task``.
"""

import os

ADOPT = "adopt"
EMPTY = "empty"
REFUSED = "refused"

DEFAULT_SKEW_SECONDS = 120

INTERNAL_EXACT = frozenset({".agent-task-context", ".herdr-loop", ".herdr"})
INTERNAL_PREFIXES = (".herdr-loop/", ".herdr/")


def adoption_skew_seconds(default=DEFAULT_SKEW_SECONDS):
    """Clock tolerance (seconds) for commit-vs-task timestamp comparison."""
    try:
        return max(0, int(os.environ.get("HERDR_ADOPT_SKEW_SECONDS", default)))
    except (TypeError, ValueError):
        return default


def _valid_sha(value):
    return isinstance(value, str) and bool(value.strip())


def is_internal_path(path):
    """Mirror of bin/herdr-task `_is_internal_untracked` (single semantics).

    Kept in ``herdr/`` so the pure classifier has zero I/O imports.
    The two implementations must stay byte-identical; see
    ``tests/test_t3_probes.py::test_internal_path_parity``.
    """
    if not isinstance(path, str) or not path:
        return False
    if path in INTERNAL_EXACT:
        return True
    return path.startswith(INTERNAL_PREFIXES)


def is_task_branch(branch, task_id):
    """D-2 naming-formula guard for legacy (anchor-less) time attribution.

    Expected form (services/herdr-worker.py:create_task_branch):
    ``agent/<agent>/<task_type>-<slug>`` where
    ``slug == task_id.lower().replace("_", "-")``.

    The task record persists ``branch`` but not ``task_type``/``agent`` as
    separate fields, so the check verifies the structural invariant that is
    computable from ``(branch, task_id)`` alone: three segments, ``agent/``
    prefix, and ``-<slug>`` suffix on the final segment. Anything else
    (e.g. ``fix/...`` PR branches whose ``--onto`` info was lost) fails
    closed.
    """
    if not isinstance(branch, str) or not branch:
        return False
    if not isinstance(task_id, str) or not task_id:
        return False
    slug = task_id.lower().replace("_", "-")
    if not slug:
        return False
    parts = branch.split("/")
    if len(parts) != 3:
        return False
    if parts[0] != "agent":
        return False
    if not parts[1]:
        return False
    leaf = parts[2]
    if not leaf.endswith(slug):
        return False
    prefix = leaf[: -len(slug)] if len(leaf) > len(slug) else ""
    # Require "<task_type>-<slug>": non-empty task_type + dash separator.
    return bool(prefix and prefix.endswith("-") and len(prefix) >= 2)


def classify_commit_state(
    *,
    baseline_commit=None,
    head=None,
    onto_branch=None,
    branch=None,
    task_id=None,
    created_at=None,
    interval_commits=None,
    head_history=None,
    baseline_is_ancestor=True,
    skew_seconds=None,
    remote_shas=None,
    enumeration_failed=False,
):
    """Classify an empty-index commit attempt.

    Args:
        baseline_commit: authoritative anchor sha recorded at dispatch (or None
            for legacy tasks).
        head: current clone HEAD sha.
        onto_branch: persisted ``--onto`` branch (or None).
        branch: task's own ``task['branch']`` (D-2 legacy guard).
        task_id: task id used to derive the expected branch suffix (D-2).
        created_at: task creation epoch seconds.
        interval_commits: commits in ``baseline_commit..HEAD`` (oldest first),
            each ``{"sha": str, "committer_ts": float, "parents": [...],
            "paths": [...]}``. ``parents``/``paths`` may be absent on legacy
            callers and are treated as unknown-but-benign for merge/internal
            checks (missing ``paths`` never counts as empty). ``None``
            means enumeration failed (M-4) and must fail closed, never EMPTY.
        head_history: first-parent chain from HEAD (newest first), same item
            shape. Used only for legacy time-basis classification. ``None``
            means enumeration failed.
        baseline_is_ancestor: result of ``git merge-base --is-ancestor``.
        skew_seconds: clock tolerance; defaults to ``adoption_skew_seconds()``.
        remote_shas: shas known reachable from ``origin/*`` remote-tracking
            refs (H-2 fetch/rebase/ff-merge guard). Any interval commit in
            this set is foreign and refuses. ``None`` means unknown (skip).
        enumeration_failed: explicit M-4 signal that git enumeration failed
            while ``head != baseline``. Forces REFUSED ``enumeration_failed``.

    Returns:
        ``(verdict, detail)`` where verdict is ``adopt``/``empty``/``refused``
        and detail carries ``reason``, ``commits``, ``baseline``, ``basis``,
        plus ``noop_commits`` (empty-change commits) and ``changed_paths``.
    """
    skew = DEFAULT_SKEW_SECONDS if skew_seconds is None else max(0, int(skew_seconds))
    created = _coerce_epoch(created_at)
    if created is None:
        return REFUSED, {
            "reason": "missing_created_at",
            "commits": 0,
            "baseline": baseline_commit,
            "basis": "none",
            "noop_commits": [],
            "changed_paths": [],
        }
    cutoff = created - skew

    if _valid_sha(baseline_commit):
        return _classify_with_anchor(
            baseline_commit=baseline_commit.strip(),
            head=head,
            created=created,
            cutoff=cutoff,
            interval_commits=interval_commits,
            baseline_is_ancestor=baseline_is_ancestor,
            remote_shas=remote_shas,
            enumeration_failed=enumeration_failed,
        )
    return _classify_by_time(
        head=head,
        onto_branch=onto_branch,
        branch=branch,
        task_id=task_id,
        cutoff=cutoff,
        head_history=head_history,
        remote_shas=remote_shas,
        enumeration_failed=enumeration_failed,
    )


def _commit_parents(item):
    parents = (item or {}).get("parents")
    if parents is None:
        return []
    if isinstance(parents, (list, tuple)):
        return list(parents)
    return []


def _commit_paths(item):
    # Returns None when unknown (key absent) so callers can distinguish
    # "known empty" (--allow-empty) from "not collected" (legacy callers).
    if item is None or "paths" not in item:
        return None
    paths = item.get("paths")
    if paths is None:
        return None
    try:
        return list(paths)
    except TypeError:
        return None


def _check_merge(commits):
    for item in commits or []:
        if len(_commit_parents(item)) >= 2:
            return item.get("sha")
    return None


def _check_remote_contained(commits, remote_shas):
    """H-2: any interval commit reachable from origin/* is foreign."""
    if not remote_shas:
        return None
    try:
        remote = {str(s).strip() for s in remote_shas if str(s).strip()}
    except TypeError:
        return None
    if not remote:
        return None
    for item in commits or []:
        sha = (item or {}).get("sha")
        if isinstance(sha, str) and sha.strip() in remote:
            return sha.strip()
    return None


def _stale_commits(commits, cutoff):
    """Commits predating the task: committer OR author timestamp old.

    H-2 rebase rewrites ``%ct`` (committer) to now while ``%at`` (author)
    stays old. Either timestamp predating the cutoff refuses.
    Missing ``author_ts`` is benign (legacy callers).
    """
    stale = []
    for item in commits or []:
        cts = _coerce_epoch((item or {}).get("committer_ts"))
        ats = _coerce_epoch((item or {}).get("author_ts"))
        if cts is None or cts < cutoff:
            stale.append(item.get("sha"))
        elif ats is not None and ats < cutoff:
            stale.append(item.get("sha"))
    return stale


def _check_internal(commits):
    for item in commits or []:
        paths = _commit_paths(item)
        if not paths:
            continue
        for path in paths:
            if is_internal_path(path):
                return item.get("sha"), path
    return None


def _union_changed_paths(commits):
    changed = []
    seen = set()
    unknown = False
    for item in commits or []:
        paths = _commit_paths(item)
        if paths is None:
            unknown = True
            continue
        for path in paths:
            if path not in seen:
                seen.add(path)
                changed.append(path)
    return changed, unknown


def _noop_commits(commits):
    return [
        item.get("sha")
        for item in commits or []
        if _commit_paths(item) is not None and len(_commit_paths(item)) == 0
    ]


def _classify_with_anchor(
    *,
    baseline_commit,
    head,
    created,
    cutoff,
    interval_commits,
    baseline_is_ancestor,
    remote_shas=None,
    enumeration_failed=False,
):
    if not _valid_sha(head):
        return REFUSED, {
            "reason": "missing_head",
            "commits": 0,
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "noop_commits": [],
            "changed_paths": [],
        }
    if not baseline_is_ancestor:
        return REFUSED, {
            "reason": "baseline_not_ancestor",
            "commits": 0,
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "noop_commits": [],
            "changed_paths": [],
        }
    # M-4: enumeration failure must fail closed, never EMPTY, when head moved.
    if interval_commits is None or enumeration_failed:
        if head.strip() == baseline_commit and not enumeration_failed:
            return EMPTY, {
                "reason": "no_new_commits",
                "commits": 0,
                "baseline": baseline_commit,
                "basis": "baseline_commit",
                "noop_commits": [],
                "changed_paths": [],
            }
        return REFUSED, {
            "reason": "enumeration_failed",
            "commits": 0,
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "noop_commits": [],
            "changed_paths": [],
        }
    commits = list(interval_commits or [])
    if head.strip() == baseline_commit or not commits:
        # M-4: head moved but interval empty due to collection failure is
        # handled above via None; an empty list with head != baseline can
        # only happen when enumeration silently dropped rows, so refuse.
        if head.strip() != baseline_commit:
            return REFUSED, {
                "reason": "enumeration_failed",
                "commits": 0,
                "baseline": baseline_commit,
                "basis": "baseline_commit",
                "noop_commits": [],
                "changed_paths": [],
            }
        return EMPTY, {
            "reason": "no_new_commits",
            "commits": 0,
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "noop_commits": [],
            "changed_paths": [],
        }
    merge_sha = _check_merge(commits)
    if merge_sha:
        return REFUSED, {
            "reason": "merge_commit_in_range",
            "commits": len(commits),
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "offending": [merge_sha],
            "noop_commits": _noop_commits(commits),
            "changed_paths": _union_changed_paths(commits)[0],
        }
    internal = _check_internal(commits)
    if internal:
        sha, path = internal
        return REFUSED, {
            "reason": "internal_path_in_range",
            "commits": len(commits),
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "offending": [sha],
            "path": path,
            "noop_commits": _noop_commits(commits),
            "changed_paths": _union_changed_paths(commits)[0],
        }
    changed, unknown = _union_changed_paths(commits)
    noops = _noop_commits(commits)
    if not changed and not unknown:
        return EMPTY, {
            "reason": "only_noop_commits",
            "commits": len(commits),
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "noop_commits": noops,
            "changed_paths": [],
        }
    # H-2: fetch/rebase/fast-forward merge brings foreign commits that are
    # reachable from origin/* into baseline..HEAD. They carry no merge
    # commit and may carry rewritten committer timestamps, so they must be
    # refused explicitly before the staleness check.
    foreign_sha = _check_remote_contained(commits, remote_shas)
    if foreign_sha:
        return REFUSED, {
            "reason": "foreign_commit_in_range",
            "commits": len(commits),
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "offending": [foreign_sha],
            "noop_commits": noops,
            "changed_paths": changed,
        }
    stale = _stale_commits(commits, cutoff)
    if stale:
        return REFUSED, {
            "reason": "commit_predates_task",
            "commits": len(commits),
            "baseline": baseline_commit,
            "basis": "baseline_commit",
            "offending": stale,
            "noop_commits": noops,
            "changed_paths": changed,
        }
    return ADOPT, {
        "reason": "attributable_commits",
        "commits": len(commits),
        "baseline": baseline_commit,
        "basis": "baseline_commit",
        "noop_commits": noops,
        "changed_paths": changed,
    }


def _classify_by_time(
    *, head, onto_branch, branch, task_id, cutoff, head_history,
    remote_shas=None, enumeration_failed=False,
):
    if not is_task_branch(branch, task_id):
        return REFUSED, {
            "reason": "legacy_branch_not_task_branch",
            "commits": 0,
            "baseline": None,
            "basis": "time",
            "noop_commits": [],
            "changed_paths": [],
        }
    if onto_branch:
        return REFUSED, {
            "reason": "no_baseline_onto",
            "commits": 0,
            "baseline": None,
            "basis": "time",
            "noop_commits": [],
            "changed_paths": [],
        }
    if not _valid_sha(head):
        return REFUSED, {
            "reason": "missing_head",
            "commits": 0,
            "baseline": None,
            "basis": "time",
            "noop_commits": [],
            "changed_paths": [],
        }
    # M-4: history enumeration failure must fail closed, never EMPTY.
    if head_history is None or enumeration_failed:
        return REFUSED, {
            "reason": "enumeration_failed",
            "commits": 0,
            "baseline": None,
            "basis": "time",
            "noop_commits": [],
            "changed_paths": [],
        }
    history = list(head_history or [])
    implicit = None
    for item in reversed(history):
        ts = _coerce_epoch(item.get("committer_ts"))
        if ts is not None and ts < cutoff:
            implicit = item
    if implicit is None:
        return REFUSED, {
            "reason": "no_attributable_baseline",
            "commits": 0,
            "baseline": None,
            "basis": "time",
            "noop_commits": [],
            "changed_paths": [],
        }
    attributable = []
    seen_implicit = False
    for item in reversed(history):
        if seen_implicit:
            attributable.append(item)
        elif item.get("sha") == implicit.get("sha"):
            seen_implicit = True
    if not seen_implicit:
        return REFUSED, {
            "reason": "no_attributable_baseline",
            "commits": 0,
            "baseline": None,
            "basis": "time",
            "noop_commits": [],
            "changed_paths": [],
        }
    if not attributable:
        return EMPTY, {
            "reason": "no_new_commits",
            "commits": 0,
            "baseline": implicit.get("sha"),
            "basis": "time",
            "noop_commits": [],
            "changed_paths": [],
        }
    merge_sha = _check_merge(attributable)
    if merge_sha:
        return REFUSED, {
            "reason": "merge_commit_in_range",
            "commits": len(attributable),
            "baseline": implicit.get("sha"),
            "basis": "time",
            "offending": [merge_sha],
            "noop_commits": _noop_commits(attributable),
            "changed_paths": _union_changed_paths(attributable)[0],
        }
    internal = _check_internal(attributable)
    if internal:
        sha, path = internal
        return REFUSED, {
            "reason": "internal_path_in_range",
            "commits": len(attributable),
            "baseline": implicit.get("sha"),
            "basis": "time",
            "offending": [sha],
            "path": path,
            "noop_commits": _noop_commits(attributable),
            "changed_paths": _union_changed_paths(attributable)[0],
        }
    changed, unknown = _union_changed_paths(attributable)
    noops = _noop_commits(attributable)
    if not changed and not unknown:
        return EMPTY, {
            "reason": "only_noop_commits",
            "commits": len(attributable),
            "baseline": implicit.get("sha"),
            "basis": "time",
            "noop_commits": noops,
            "changed_paths": [],
        }
    # H-2 legacy path: same remote-containment guard as anchored path.
    foreign_sha = _check_remote_contained(attributable, remote_shas)
    if foreign_sha:
        return REFUSED, {
            "reason": "foreign_commit_in_range",
            "commits": len(attributable),
            "baseline": implicit.get("sha"),
            "basis": "time",
            "offending": [foreign_sha],
            "noop_commits": noops,
            "changed_paths": changed,
        }
    stale = _stale_commits(attributable, cutoff)
    if stale:
        return REFUSED, {
            "reason": "unattributable_commits",
            "commits": len(attributable),
            "baseline": implicit.get("sha"),
            "basis": "time",
            "offending": stale,
            "noop_commits": noops,
            "changed_paths": changed,
        }
    return ADOPT, {
        "reason": "attributable_commits",
        "commits": len(attributable),
        "baseline": implicit.get("sha"),
        "basis": "time",
        "noop_commits": noops,
        "changed_paths": changed,
    }


def _coerce_epoch(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def explain(verdict, detail):
    """One-line human explanation of a classification result."""
    detail = detail or {}
    reason = detail.get("reason", "unknown")
    commits = detail.get("commits", 0)
    baseline = detail.get("baseline") or "none"
    basis = detail.get("basis", "none")
    return (
        f"verdict={verdict} reason={reason} "
        f"commits={commits} baseline={baseline} basis={basis}"
    )
