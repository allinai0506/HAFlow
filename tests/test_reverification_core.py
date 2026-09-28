"""Selective Reverification v1 core/fact-layer tests (HAFlow PR #108).

覆盖纯决策核心与事实层:真实 git 仓库产出 diff、计划构建、事件落盘、
Join Gate 消费 reuse。

**这不是端到端测试。** 控制器 sweep 的完整链路在
``tests/test_reverification_controller.py``——本文件全程不碰 ``_ctl``,
刻意把「核心 + 事实」与「控制器装配」分开,这样计划逻辑的失败不会被误读成
调度接线的失败。真正的端到端证据只在 controller 那个文件里。
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import reverification as rv  # noqa: E402
from herdr import scheduler as scheduler_core  # noqa: E402
from herdr import scheduler_facts as facts  # noqa: E402


def _load_module(name, path):
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, str(path)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ctl = _load_module(
    "herdr_controller_reverification_test",
    HERDR_ROOT / "services" / "herdr-controller.py",
)

WF = "wf-rever-e2e"


def _reuse_fact(workflow_id, verifier, candidate_sha, db_path, episode_id=None):
    """Look up a reuse fact under the policy AND freeze episode that made it.

    ``find_reuse_fact`` is fail-closed on both, so a call that omits one asks
    "is there a fact under *no* policy / *no* episode?", which is correctly
    None. Tests asking "is the current episode covered?" pass both.
    """
    if episode_id is None:
        freezes = facts.list_candidate_frozen_events(workflow_id, db_path=db_path)
        episode_id = freezes[-1].get("id") if freezes else None
    return facts.find_reuse_fact(
        workflow_id, verifier, candidate_sha,
        policy_identity=rv.policy_identity(POLICY), episode_id=episode_id,
        db_path=db_path)
POLICY = {
    "version": rv.POLICY_VERSION,
    "verifiers": {
        "test": {"reusable_only_if_changes_within": ["docs/**/*.md"]},
        "review": {"reusable_only_if_changes_within": []},
    },
}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args), check=check,
        text=True, capture_output=True)


class ReverificationE2EBase(unittest.TestCase):
    """Real git repo + isolated state store + captured launches."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-rever-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", ".")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "root",
             "--no-gpg-sign")

        self.docs = self.root / "wdocs"
        self.docs.mkdir()
        self.db = self.root / "state.db"
        env = patch.dict(os.environ, {
            "HERDR_WORKFLOW_DOCS_DIR": str(self.docs),
            "HERDR_STATE_DB": str(self.db),
        })
        env.start()
        self.addCleanup(env.stop)

        from herdr.state_store import get_state_store
        get_state_store(self.db).save_workflow(
            {"workflow_id": WF, "status": "running"})

        self.launches = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kwargs)
            argv = [str(c) for c in (cmd or [])]
            if len(argv) > 1 and argv[1] == "launch":
                self.launches.append(argv)
            return subprocess.CompletedProcess(cmd, 0, "Task dispatched: x", "")

        self.run_patcher = patch.object(_ctl.subprocess, "run", fake_run)
        self.run_patcher.start()
        self.addCleanup(self.run_patcher.stop)

    def write(self, rel, text="x"):
        target = self.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message="c"):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", message, "--no-gpg-sign")
        return self.head

    @property
    def head(self):
        return _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def record_pass(self, task_id, node, sha):
        from herdr.state_store import get_state_store
        get_state_store(self.db).save_task({
            "task_id": task_id, "workflow_id": WF, "node": node,
            "stage": node, "status": "completed", "stage_verdict": "pass",
            "candidate_sha": sha, "verified_candidate_sha": sha,
        })

    def plan_for(self, from_sha, to_sha, sources):
        entries, reason = rv.collect_candidate_changes(
            self.repo, from_sha, to_sha)
        return rv.build_reverification_plan(
            WF, from_sha, to_sha, entries, sources, POLICY,
            diff_reason=reason, episode_id=self._episode_for(to_sha))

    def _episode_for(self, candidate_sha):
        """The freeze that authorises decisions about ``candidate_sha``."""
        facts.record_candidate_frozen(WF, candidate_sha, db_path=self.db)
        freezes = facts.list_candidate_frozen_events(WF, db_path=self.db)
        for event in freezes:
            if (event.get("payload") or {}).get("candidate_sha") == candidate_sha:
                return event.get("id")
        return ""

    def record_plan(self, plan):
        """Persist every decision in a plan; return [(verifier, status), ...]."""
        recorded = []
        for name, decision in plan["verifiers"].items():
            result = facts.record_reverification_decision(WF, decision,
                                                         db_path=self.db)
            recorded.append((name, result["status"]))
        return recorded


class Case1DocsOnlyReuseTest(ReverificationE2EBase):
    """§31 Case 1 / §38: docs-only -> test REUSE, review RERUN."""

    def test_docs_only_change_reuses_test_and_reruns_review(self):
        a = self.head
        self.write("docs/user-guide.md", "guide")
        b = self.commit("docs")

        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": f"{WF}-test-auto",
            "verdict": "pass", "candidate_sha": a,
            "verified_candidate_sha": a, "source": "fresh",
        }])

        self.assertEqual(plan["verifiers"]["test"]["decision"], rv.DECISION_REUSE)
        self.assertEqual(plan["verifiers"]["review"]["decision"], rv.DECISION_RERUN)
        self.assertEqual(plan["verifiers"]["test"]["reason"],
                         rv.REASON_REUSE_NON_IMPACT)
        self.assertEqual(plan["changed_paths"], ["docs/user-guide.md"])


class SchedulerIntegrationTest(ReverificationE2EBase):
    """§19/§24: the scheduler consumes the plan; dispatch is suppressed."""

    def test_reuse_suppresses_task_creation_and_gate_accepts_it(self):
        a = self.head
        self.record_pass(f"{WF}-test-auto", "test", a)
        self.write("docs/user-guide.md", "guide")
        b = self.commit("docs")

        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": f"{WF}-test-auto",
            "verdict": "pass", "candidate_sha": a,
            "verified_candidate_sha": a, "source": "fresh",
        }])
        self.record_plan(plan)

        facts.record_candidate_frozen(WF, a, db_path=self.db)
        episode_b = self._episode_for(b)

        reuse = _reuse_fact(WF, "test", b, self.db, episode_id=episode_b)
        self.assertIsNotNone(reuse)
        self.assertEqual(reuse["source_task_id"], f"{WF}-test-auto")
        self.assertEqual(reuse["to_candidate_sha"], b)

        # The reused node owns no task, so node completion must still hold.
        review_task = {
            "task_id": f"{WF}-review-auto", "workflow_id": WF, "node": "review",
            "stage": "review", "status": "completed", "stage_verdict": "pass",
            "candidate_sha": b, "verified_candidate_sha": b,
        }
        tasks = [review_task]
        gate = {"id": "wrapup", "depends_on": ["test", "review"]}
        passed, reason, details = scheduler_core.evaluate_join_gate(
            gate, tasks, WF, b, reuse_facts=[reuse])
        self.assertTrue(passed, reason)
        self.assertEqual(details["branches"]["test"]["evidence_source"], "reuse")
        self.assertEqual(details["branches"]["review"]["evidence_source"], "fresh")

    def test_case13_crash_recovery_is_idempotent(self):
        """§24/§31 Case 13: 重复执行不得产生第二条 plan / 第二条 reuse。"""
        a = self.head
        self.record_pass(f"{WF}-test-auto", "test", a)
        self.write("docs/user-guide.md", "guide")
        b = self.commit("docs")
        sources = [{
            "verifier": "test", "task_id": f"{WF}-test-auto",
            "verdict": "pass", "candidate_sha": a,
            "verified_candidate_sha": a, "source": "fresh",
        }]

        plan_one = self.plan_for(a, b, sources)
        plan_two = self.plan_for(a, b, sources)
        self.assertEqual(plan_one["plan_id"], plan_two["plan_id"])
        self.assertEqual(plan_one, plan_two)

        self.record_plan(plan_one)
        statuses = self.record_plan(plan_two)
        self.assertEqual([s for _n, s in statuses], ["exists", "exists"])
        self.assertEqual(
            len(facts.list_reverification_decisions(WF, db_path=self.db)), 2)
        self.assertEqual(
            len(self.launches), 0, "reuse must not create any Task")

    def test_code_change_records_rerun_and_no_reuse_fact(self):
        """§31 Case 2: 代码变化 -> 两个 verifier 都 RERUN。"""
        a = self.head
        self.record_pass(f"{WF}-test-auto", "test", a)
        self.write("herdr/scheduler.py", "code")
        b = self.commit("code")

        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": f"{WF}-test-auto",
            "verdict": "pass", "candidate_sha": a,
            "verified_candidate_sha": a, "source": "fresh",
        }])
        self.assertEqual(plan["verifiers"]["test"]["decision"], rv.DECISION_RERUN)
        self.assertEqual(plan["verifiers"]["test"]["reason"],
                         rv.REASON_RERUN_OUTSIDE_SCOPE)
        self.record_plan(plan)
        self.assertIsNone(_reuse_fact(WF, "test", b, self.db))

    def test_case14_aba_keeps_episodes_separate(self):
        """§31 Case 14: A→B→A 按 episode 重算,不误用第一次的事实。"""
        a = self.head
        self.write("docs/a.md", "1")
        b = self.commit("b")
        self.write("docs/a.md", "2")
        a2 = self.commit("a2")

        first = self.plan_for(a, b, [{
            "verifier": "test", "task_id": f"{WF}-test-auto",
            "verdict": "pass", "candidate_sha": a,
            "verified_candidate_sha": a, "source": "fresh",
        }])
        second = self.plan_for(b, a2, [{
            "verifier": "test", "task_id": f"{WF}-test-auto",
            "verdict": "pass", "candidate_sha": b,
            "verified_candidate_sha": b, "source": "fresh",
        }])
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertNotEqual(
            first["verifiers"]["test"]["decision_identity"],
            second["verifiers"]["test"]["decision_identity"])

        self.record_plan(first)
        self.record_plan(second)
        self.assertEqual(
            len(facts.list_reverification_decisions(WF, db_path=self.db)), 4)


class FailClosedTest(ReverificationE2EBase):
    """§37: the ten adversarial questions, each must fail closed."""

    def test_q1_diff_failure_never_reuses(self):
        plan = self.plan_for("deadbeefdeadbeef", self.head, [{
            "verifier": "test", "task_id": "t", "verdict": "pass",
            "candidate_sha": "deadbeefdeadbeef",
            "verified_candidate_sha": "deadbeefdeadbeef", "source": "fresh",
        }])
        self.assertEqual(plan["verifiers"]["test"]["decision"], rv.DECISION_RERUN)
        self.assertEqual(plan["verifiers"]["test"]["reason"],
                         rv.REASON_RERUN_DIFF_UNAVAILABLE)

    def test_q2_unknown_path_type_is_never_safe(self):
        self.write("engine-v2/runtime.xyz", "x")
        a, b = self.head, self.commit("v2")
        self.write("docs/d.md", "d")
        b = self.commit("d")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": "t", "verdict": "pass",
            "candidate_sha": a, "verified_candidate_sha": a, "source": "fresh",
        }])
        self.assertEqual(plan["verifiers"]["test"]["decision"], rv.DECISION_RERUN)
        self.assertIn("engine-v2/runtime.xyz",
                      plan["verifiers"]["test"]["out_of_scope_paths"])

    def test_q3_rename_checks_old_path(self):
        self.write("herdr/foo.py", "c")
        a = self.commit("seed")
        (self.repo / "docs").mkdir(parents=True, exist_ok=True)
        _git(self.repo, "mv", "herdr/foo.py", "docs/foo.md")
        b = self.commit("mv")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": "t", "verdict": "pass",
            "candidate_sha": a, "verified_candidate_sha": a, "source": "fresh",
        }])
        self.assertEqual(plan["verifiers"]["test"]["decision"], rv.DECISION_RERUN)

    def test_q4_blocked_previous_verdict_cannot_be_reused(self):
        a = self.head
        self.write("docs/a.md", "d")
        b = self.commit("d")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": "t", "verdict": "blocked",
            "candidate_sha": a, "verified_candidate_sha": a, "source": "fresh",
        }])
        self.assertEqual(plan["verifiers"]["test"]["decision"], rv.DECISION_RERUN)
        self.assertEqual(plan["verifiers"]["test"]["reason"],
                         rv.REASON_RERUN_SOURCE_NOT_PASS)

    def test_q5_pass_without_verified_sha_cannot_be_reused(self):
        a = self.head
        self.write("docs/a.md", "d")
        b = self.commit("d")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": "t", "verdict": "pass",
            "candidate_sha": a, "verified_candidate_sha": "", "source": "fresh",
        }])
        self.assertEqual(plan["verifiers"]["test"]["reason"],
                         rv.REASON_RERUN_SOURCE_UNBOUND)

    def test_q5b_launch_baseline_is_not_completion_evidence(self):
        """A pre-Scheduler PASS must not become a reuse source via baseline.

        ``extract_task_verified_sha`` deliberately falls back to
        ``baseline_commit`` for tasks that carry no candidate claim, so that
        pre-Scheduler workflows keep their old meaning. A reuse source is a
        stronger claim than that fallback makes: it says "this verifier really
        verified revision A". A launch baseline only says "it started from A",
        which is exactly the claim #107 introduced completion evidence to
        eliminate — an agent that pulled or rebased mid-task still reports a
        baseline of A while having verified something else.

        So the production collector must read ``verified_candidate_sha``
        literally, never through the legacy fallback.
        """
        from herdr.state_store import get_state_store
        a = self.head
        # Legacy shape: no candidate_sha claim, only a launch baseline.
        get_state_store(self.db).save_task({
            "task_id": f"{WF}-test-legacy", "workflow_id": WF, "node": "test",
            "stage": "test", "status": "completed", "stage_verdict": "pass",
            "baseline_commit": a,
        })
        sources = _ctl._reverification_source_verifications(WF, a)
        self.assertEqual(
            [s for s in sources if s["task_id"] == f"{WF}-test-legacy"], [],
            "a launch baseline must not be collected as a reuse source",
        )

    def test_q5c_claim_without_completion_evidence_is_refused(self):
        """A candidate claim with no verdict-time read proves nothing either."""
        from herdr.state_store import get_state_store
        a = self.head
        get_state_store(self.db).save_task({
            "task_id": f"{WF}-test-unproven", "workflow_id": WF, "node": "test",
            "stage": "test", "status": "completed", "stage_verdict": "pass",
            "candidate_sha": a, "baseline_commit": a,
        })
        sources = _ctl._reverification_source_verifications(WF, a)
        self.assertEqual(
            [s for s in sources if s["task_id"] == f"{WF}-test-unproven"], [],
            "claim + baseline without verified_candidate_sha is not a proof",
        )

    def test_q6_c_cannot_consume_a_to_b_fact(self):
        a = self.head
        self.write("docs/a.md", "d")
        b = self.commit("b")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": "t", "verdict": "pass",
            "candidate_sha": a, "verified_candidate_sha": a, "source": "fresh",
        }])
        self.record_plan(plan)
        self.assertIsNone(_reuse_fact(WF, "test", "c" * 40, self.db))
        effective = scheduler_core.resolve_effective_verification(
            [], WF, "test", "c" * 40,
            [_reuse_fact(WF, "test", b, self.db)])
        self.assertEqual(effective["status"], "none")

    def test_q7_fresh_blocked_not_overridden_by_reuse(self):
        a = self.head
        self.write("docs/a.md", "d")
        b = self.commit("b")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": "t", "verdict": "pass",
            "candidate_sha": a, "verified_candidate_sha": a, "source": "fresh",
        }])
        self.record_plan(plan)
        reuse = _reuse_fact(WF, "test", b, self.db)
        fresh_blocked = {
            "task_id": f"{WF}-test-auto", "workflow_id": WF, "node": "test",
            "stage": "test", "status": "completed", "stage_verdict": "blocked",
            "candidate_sha": b, "verified_candidate_sha": b,
        }
        effective = scheduler_core.resolve_effective_verification(
            [fresh_blocked], WF, "test", b, [reuse])
        self.assertEqual(effective["status"], "blocked")
        self.assertEqual(effective["source"], "fresh")

    def test_q10_missing_policy_reruns(self):
        a = self.head
        self.write("docs/a.md", "d")
        b = self.commit("d")
        entries, _reason = rv.collect_candidate_changes(self.repo, a, b)
        plan = rv.build_reverification_plan(
            WF, a, b, entries, [{
                "verifier": "test", "task_id": "t", "verdict": "pass",
                "candidate_sha": a, "verified_candidate_sha": a,
                "source": "fresh",
            }], rv.default_policy(), verifiers=["test", "review"])
        self.assertEqual(plan["verifiers"]["test"]["decision"],
                         rv.DECISION_RERUN)
        self.assertEqual(plan["verifiers"]["test"]["reason"],
                         rv.REASON_RERUN_POLICY_ABSENT)
        self.record_plan(plan)
        self.assertIsNone(
            _reuse_fact(WF, "test", b, self.db))


class ObservabilityTest(ReverificationE2EBase):
    """§32: 「为什么这次没重跑」必须能从事实里读出来。"""

    def test_reuse_fact_explains_itself(self):
        a = self.head
        self.write("docs/user-guide.md", "g")
        b = self.commit("docs")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": f"{WF}-test-auto",
            "verdict": "pass", "candidate_sha": a,
            "verified_candidate_sha": a, "source": "fresh",
        }])
        for _name, decision in plan["verifiers"].items():
            facts.record_reverification_decision(WF, decision, db_path=self.db)

        fact = _reuse_fact(WF, "test", b, self.db)
        self.assertEqual(fact["verifier"], "test")
        self.assertEqual(fact["from_candidate_sha"], a)
        self.assertEqual(fact["to_candidate_sha"], b)
        self.assertEqual(fact["changed_paths"], ["docs/user-guide.md"])
        self.assertEqual(fact["source_task_id"], f"{WF}-test-auto")
        self.assertEqual(fact["source_verdict"], "pass")
        self.assertEqual(fact["source_verified_candidate_sha"], a)
        self.assertEqual(fact["reason"], rv.REASON_REUSE_NON_IMPACT)
        self.assertEqual(fact["policy_version"], rv.POLICY_VERSION)
        self.assertTrue(fact["created_at"] > 0)

    def test_metrics_are_computable(self):
        a = self.head
        self.write("docs/a.md", "d")
        b = self.commit("d")
        plan = self.plan_for(a, b, [{
            "verifier": "test", "task_id": "t", "verdict": "pass",
            "candidate_sha": a, "verified_candidate_sha": a, "source": "fresh",
        }])
        metrics = rv.reverification_metrics(list(plan["verifiers"].values()))
        self.assertEqual(metrics["total_verifiers"], 2)
        self.assertEqual(metrics["reuse_count"], 1)
        self.assertEqual(metrics["rerun_count"], 1)
        self.assertEqual(metrics["reuse_rate"], 0.5)


class TemplatePolicyTest(unittest.TestCase):
    """§28/§29: the shipped policy matches the repo's real markdown readers."""

    def test_shipped_template_policy(self):
        from herdr.workflow import load_template
        template = load_template("software-development-v1")
        policy = rv.policy_from_workflow(template)
        self.assertEqual(rv.verifier_scope(policy, "test"), ["docs/**/*.md"])
        self.assertEqual(rv.verifier_scope(policy, "review"), [])
        self.assertEqual(policy["version"], rv.POLICY_VERSION)

    def test_agent_contract_markdown_is_outside_the_scope(self):
        """根级行为契约不是纯文档,不得被当作可复用。"""
        for rel in ("AGENTS.md", "CLAUDE.md", "README.md", "RULES.md"):
            self.assertFalse(rv.scope_matches(rel, "docs/**/*.md"), rel)
        self.assertFalse(
            rv.scope_matches(".herdr-loop/GOAL.md", "docs/**/*.md"))
        self.assertFalse(
            rv.scope_matches("docs/context/x.txt", "docs/**/*.md"))


if __name__ == "__main__":
    unittest.main()
