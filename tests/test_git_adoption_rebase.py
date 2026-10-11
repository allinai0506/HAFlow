import os
import time
import unittest

from herdr.git_adoption import (
    ADOPT,
    REFUSED,
    classify_commit_state,
)


class TestGitAdoptionRebase(unittest.TestCase):
    def test_rebase_replay_with_historical_author_ts_is_adopted_when_allow_rebase_is_true(self):
        now = time.time()
        # Created at `now`. Committer timestamp is fresh (`now`).
        # Author timestamp is 2 hours ago (`now - 7200`) due to rebase replay preserving author date.
        verdict, detail = classify_commit_state(
            baseline_commit="base_sha",
            head="rebased_sha",
            branch="agent/opencode/feat-task-1",
            task_id="task_1",
            created_at=now,
            interval_commits=[{
                "sha": "rebased_sha",
                "committer_ts": now,
                "author_ts": now - 7200,
                "parents": ["base_sha"],
                "paths": ["src/main.py"],
            }],
            baseline_is_ancestor=True,
            remote_shas=set(),
            current_branch="agent/opencode/feat-task-1",
            allow_rebase=True,
        )
        self.assertEqual(verdict, ADOPT)
        self.assertEqual(detail["reason"], "attributable_commits")
        self.assertEqual(detail["commits"], 1)

    def test_rebase_without_allow_rebase_still_refuses_as_tampering_defense(self):
        now = time.time()
        verdict, detail = classify_commit_state(
            baseline_commit="base_sha",
            head="rebased_sha",
            branch="agent/opencode/feat-task-1",
            task_id="task_1",
            created_at=now,
            interval_commits=[{
                "sha": "rebased_sha",
                "committer_ts": now,
                "author_ts": now - 7200,
                "parents": ["base_sha"],
                "paths": ["src/main.py"],
            }],
            baseline_is_ancestor=True,
            remote_shas=set(),
            current_branch="agent/opencode/feat-task-1",
            allow_rebase=False,
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "commit_predates_task")

    def test_rebase_with_old_committer_ts_is_always_refused(self):
        now = time.time()
        # Even with allow_rebase=True, committer_ts < cutoff means it was committed before task
        verdict, detail = classify_commit_state(
            baseline_commit="base_sha",
            head="stale_sha",
            branch="agent/opencode/feat-task-1",
            task_id="task_1",
            created_at=now,
            interval_commits=[{
                "sha": "stale_sha",
                "committer_ts": now - 3600,
                "author_ts": now - 7200,
                "parents": ["base_sha"],
                "paths": ["src/main.py"],
            }],
            baseline_is_ancestor=True,
            remote_shas=set(),
            current_branch="agent/opencode/feat-task-1",
            allow_rebase=True,
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "commit_predates_task")

    def test_foreign_commit_is_still_refused_even_with_allow_rebase(self):
        now = time.time()
        verdict, detail = classify_commit_state(
            baseline_commit="base_sha",
            head="foreign_sha",
            branch="agent/opencode/feat-task-1",
            task_id="task_1",
            created_at=now,
            interval_commits=[{
                "sha": "foreign_sha",
                "committer_ts": now,
                "author_ts": now - 7200,
                "parents": ["base_sha"],
                "paths": ["src/main.py"],
            }],
            baseline_is_ancestor=True,
            remote_shas={"foreign_sha"},
            current_branch="agent/opencode/feat-task-1",
            allow_rebase=True,
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "foreign_commit_in_range")

    def test_onto_branch_automatically_enables_allow_rebase(self):
        now = time.time()
        verdict, detail = classify_commit_state(
            baseline_commit="base_sha",
            head="rebased_sha",
            branch="feat-branch",
            onto_branch="feat-branch",
            task_id="task_1",
            created_at=now,
            interval_commits=[{
                "sha": "rebased_sha",
                "committer_ts": now,
                "author_ts": now - 7200,
                "parents": ["base_sha"],
                "paths": ["src/main.py"],
            }],
            baseline_is_ancestor=True,
            remote_shas=set(),
            current_branch="feat-branch",
        )
        self.assertEqual(verdict, ADOPT)
        self.assertEqual(detail["reason"], "attributable_commits")

    def test_multiple_sequential_commits_with_partial_author_date_skew(self):
        now = time.time()
        # Commit 1: rebased commit with historical author date
        # Commit 2: fresh commit with recent author date
        commits = [
            {
                "sha": "rebased_sha_1",
                "committer_ts": now,
                "author_ts": now - 7200,
                "parents": ["base_sha"],
                "paths": ["src/part1.py"],
            },
            {
                "sha": "fresh_sha_2",
                "committer_ts": now + 10,
                "author_ts": now + 10,
                "parents": ["rebased_sha_1"],
                "paths": ["src/part2.py"],
            },
        ]
        verdict, detail = classify_commit_state(
            baseline_commit="base_sha",
            head="fresh_sha_2",
            branch="agent/codex/feat-multi",
            task_id="multi",
            created_at=now,
            interval_commits=commits,
            baseline_is_ancestor=True,
            remote_shas=set(),
            current_branch="agent/codex/feat-multi",
            allow_rebase=True,
        )
        self.assertEqual(verdict, ADOPT)
        self.assertEqual(detail["reason"], "attributable_commits")
        self.assertEqual(detail["commits"], 2)

    def test_multiple_sequential_commits_with_stale_committer_rejected(self):
        now = time.time()
        # Commit 1: stale committer timestamp (< cutoff)
        # Commit 2: fresh committer timestamp (>= cutoff)
        commits = [
            {
                "sha": "stale_sha_1",
                "committer_ts": now - 3600,
                "author_ts": now - 7200,
                "parents": ["base_sha"],
                "paths": ["src/part1.py"],
            },
            {
                "sha": "rebased_sha_2",
                "committer_ts": now + 10,
                "author_ts": now - 3600,
                "parents": ["stale_sha_1"],
                "paths": ["src/part2.py"],
            },
        ]
        verdict, detail = classify_commit_state(
            baseline_commit="base_sha",
            head="rebased_sha_2",
            branch="agent/codex/feat-multi",
            task_id="multi",
            created_at=now,
            interval_commits=commits,
            baseline_is_ancestor=True,
            remote_shas=set(),
            current_branch="agent/codex/feat-multi",
            allow_rebase=True,
        )
        self.assertEqual(verdict, REFUSED)
        self.assertEqual(detail["reason"], "commit_predates_task")

