"""Pure adoption-classifier tests (AC-1).

Covers ADOPT / EMPTY / REFUSED and every negative path of
herdr.git_adoption.classify_commit_state: onto without anchor, predated
commits, non-ancestor baseline, empty intervals and legacy time basis.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr.git_adoption import (
    ADOPT,
    EMPTY,
    REFUSED,
    adoption_skew_seconds,
    classify_commit_state,
    explain,
)


def _commit(sha, ts, paths=("a.txt",)):
    # NOTE: paths default to a known non-empty list. A commit without
    # paths is "unknown" (not empty) and fails closed with
    # commit_paths_unknown; pass paths=[] explicitly for --allow-empty.
    return {
        "sha": sha,
        "committer_ts": float(ts),
        "parents": ["base"],
        "paths": list(paths),
    }


def _branch_kwargs(task_id="t1"):
    branch = "agent/opencode/docs-" + task_id.lower().replace("_", "-")
    return {"branch": branch, "current_branch": branch}


class ClassifyWithAnchorTest(unittest.TestCase):
    def test_adopt_with_anchor_commits(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 1100), _commit("c2", 1200)],
            baseline_is_ancestor=True,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, ADOPT)
        self.assertEqual(detail["commits"], 2)
        self.assertEqual(detail["baseline"], "base")
        self.assertEqual(detail["basis"], "baseline_commit")

    def test_empty_when_head_equals_baseline(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="base",
            created_at=1000,
            interval_commits=[],
            baseline_is_ancestor=True,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, EMPTY)
        self.assertEqual(detail["reason"], "no_new_commits")

    def test_empty_when_interval_count_zero(self):
        # M-4: head moved but interval enumerates to nothing must fail
        # closed (enumeration failure), never EMPTY.
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="other",
            created_at=1000,
            interval_commits=[],
            baseline_is_ancestor=True,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "enumeration_failed")

    def test_refused_when_baseline_not_ancestor(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 1100)],
            baseline_is_ancestor=False,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "baseline_not_ancestor")

    def test_refused_when_commit_predates_task(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 1100), _commit("c0", 500)],
            baseline_is_ancestor=True,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "commit_predates_task")
        self.assertIn("c0", detail["offending"])

    def test_skew_tolerance_applies(self):
        verdict, _ = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 950)],
            baseline_is_ancestor=True,
            skew_seconds=120,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, ADOPT)

    def test_skew_boundary_refuses(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 800)],
            baseline_is_ancestor=True,
            skew_seconds=120,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "commit_predates_task")

    def test_refused_when_current_branch_mismatch(self):
        # P1: HEAD attributable to the task but checked out on another
        # branch must not be adopted as this task's deliverable.
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 1100)],
            baseline_is_ancestor=True,
            branch="agent/opencode/docs-t1",
            current_branch="other",
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "current_branch_mismatch")

    def test_refused_when_current_branch_unknown(self):
        # P1: detached HEAD / probe failure (None) never guesses.
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 1100)],
            baseline_is_ancestor=True,
            branch="agent/opencode/docs-t1",
            current_branch=None,
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "current_branch_mismatch")

    def test_onto_mode_accepts_onto_branch(self):
        # P1 onto mode: checkout on the persisted onto branch is a
        # legitimate ownership identity.
        verdict, _ = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[_commit("c1", 1100)],
            baseline_is_ancestor=True,
            branch="fix/pr-1",
            onto_branch="fix/pr-1",
            current_branch="fix/pr-1",
        )
        self.assertEqual(verdict, ADOPT)

    def test_refused_when_paths_unknown(self):
        # F-3: an uninspectable commit must fail closed, never ADOPT,
        # even though timestamps and ancestry are attributable.
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=1000,
            interval_commits=[
                {
                    "sha": "c1",
                    "committer_ts": 1100.0,
                    "parents": ["base"],
                    "paths": None,
                }
            ],
            baseline_is_ancestor=True,
            **_branch_kwargs("t1"),
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "commit_paths_unknown")
        self.assertIn("c1", detail["offending"])


class ClassifyByTimeTest(unittest.TestCase):
    def test_refused_onto_without_anchor(self):
        verdict, detail = classify_commit_state(
            head="head",
            onto_branch="pr-branch",
            branch="agent/opencode/docs-t1",
            task_id="t1",
            created_at=1000,
            head_history=[_commit("head", 1100), _commit("old", 500)],
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "no_baseline_onto")

    def test_adopt_by_time_basis(self):
        verdict, detail = classify_commit_state(
            head="head",
            branch="agent/opencode/docs-t1",
            task_id="t1",
            created_at=1000,
            head_history=[_commit("head", 1200), _commit("c1", 1100), _commit("old", 500)],
            current_branch="agent/opencode/docs-t1",
        )
        self.assertEqual(verdict, ADOPT)
        self.assertEqual(detail["basis"], "time")
        self.assertEqual(detail["baseline"], "old")
        self.assertEqual(detail["commits"], 2)

    def test_empty_by_time_basis(self):
        verdict, _detail = classify_commit_state(
            head="old",
            branch="agent/opencode/docs-t1",
            task_id="t1",
            created_at=1000,
            head_history=[_commit("old", 500)],
            current_branch="agent/opencode/docs-t1",
        )
        self.assertEqual(verdict, EMPTY)

    def test_refused_without_attributable_baseline(self):
        verdict, detail = classify_commit_state(
            head="head",
            branch="agent/opencode/docs-t1",
            task_id="t1",
            created_at=1000,
            head_history=[_commit("head", 1100)],
            current_branch="agent/opencode/docs-t1",
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "no_attributable_baseline")

    def test_refused_when_time_basis_branch_mismatch(self):
        # P1 legacy path: attributable history on the wrong checkout
        # must not adopt.
        verdict, detail = classify_commit_state(
            head="head",
            branch="agent/opencode/docs-t1",
            task_id="t1",
            created_at=1000,
            head_history=[_commit("head", 1200), _commit("c1", 1100), _commit("old", 500)],
            current_branch="other",
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "current_branch_mismatch")

    def test_missing_created_at_refuses(self):
        verdict, detail = classify_commit_state(
            baseline_commit="base",
            head="head",
            created_at=None,
            interval_commits=[_commit("c1", 1100)],
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "missing_created_at")


class AdoptionHelperTest(unittest.TestCase):
    def test_skew_env_override(self):
        with patch.dict(os.environ, {"HERDR_ADOPT_SKEW_SECONDS": "30"}):
            self.assertEqual(adoption_skew_seconds(), 30)

    def test_skew_env_invalid_falls_back(self):
        with patch.dict(os.environ, {"HERDR_ADOPT_SKEW_SECONDS": "bad"}):
            self.assertEqual(adoption_skew_seconds(), 120)

    def test_explain_mentions_verdict_and_reason(self):
        text = explain(ADOPT, {"reason": "attributable_commits", "commits": 2,
                               "baseline": "abc", "basis": "baseline_commit"})
        self.assertIn("adopt", text)
        self.assertIn("attributable_commits", text)


if __name__ == "__main__":
    unittest.main()
