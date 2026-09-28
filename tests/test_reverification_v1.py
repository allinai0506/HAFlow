"""Selective Reverification v1 tests (HAFlow PR #108).

纯函数层(策略 / glob / diff 解析 / 判定矩阵 / 计划 / 身份 / 指标)
与事实层(真实 git 仓库、事件持久化、Join Gate 消费)。
不启动真实 Agent、不调用收费模型、不写生产状态。
"""
import subprocess
import tempfile
from pathlib import Path

import pytest

from herdr import reverification as rv
from herdr import scheduler as sched
from herdr import scheduler_facts as facts
from herdr.state_store import get_state_store


@pytest.fixture
def db(tmp_path, monkeypatch):
    """临时状态库;同时设环境变量与显式 db_path,避免触碰生产状态。"""
    path = tmp_path / "reverification-facts.db"
    monkeypatch.setenv("HERDR_STATE_DB", str(path))
    get_state_store(path).save_workflow({"workflow_id": "wf-facts-rev",
                                         "status": "running"})
    return path


#: The shipped policy's resolved identity — the identity a fact recorded under
#: it carries, and therefore the one a lookup must present.
_SHIPPED_POLICY = {
    "reverification": {
        "version": "selective-reverification-v1",
        "test": {"reusable_only_if_changes_within": ["docs/**/*.md"]},
        "review": {"reusable_only_if_changes_within": []},
    },
}


def _policy_identity(policy=None):
    return rv.policy_identity(rv.policy_from_workflow(policy or _SHIPPED_POLICY))


# ---------------------------------------------------------------- policy ---

class TestPolicy:
    def test_missing_policy_block_is_fail_closed(self):
        """未声明 reverification 的 workflow -> 无任何 verifier 声明。"""
        policy = rv.policy_from_workflow({"name": "x", "nodes": []})
        assert policy["verifiers"] == {}
        assert policy["version"] == rv.POLICY_VERSION

    def test_declared_scope_is_preserved(self):
        policy = rv.policy_from_workflow({
            "reverification": {
                "test": {"reusable_only_if_changes_within": ["docs/**/*.md"]},
            },
        })
        assert rv.verifier_scope(policy, "test") == ["docs/**/*.md"]

    def test_empty_list_means_no_scope_not_everything(self):
        """§10: [] 是「不存在安全复用范围」,不是「什么都能复用」。"""
        policy = rv.policy_from_workflow({"reverification": {"review": []}})
        assert rv.verifier_scope(policy, "review") == []

    def test_empty_mapping_means_no_scope(self):
        policy = rv.policy_from_workflow({
            "reverification": {"review": {"reusable_only_if_changes_within": []}},
        })
        assert rv.verifier_scope(policy, "review") == []

    def test_absent_verifier_returns_none_not_empty(self):
        """None 与 [] 必须可区分:None=策略未声明,[]=已声明但无安全范围。"""
        policy = rv.policy_from_workflow({"reverification": {"test": []}})
        assert rv.verifier_scope(policy, "review") is None
        assert rv.verifier_scope(policy, "test") == []

    @pytest.mark.parametrize("bad", [
        {"reverification": "not-a-mapping"},
        {"reverification": {"test": "not-a-mapping"}},
        {"reverification": {"test": {"reusable_only_if_changes_within": "docs/**"}}},
        {"reverification": {"test": {"reusable_only_if_changes_within": [1, 2]}}},
    ])
    def test_malformed_policy_fails_closed(self, bad):
        policy = rv.policy_from_workflow(bad)
        assert policy["verifiers"] == {}

    def test_version_override_is_recorded(self):
        policy = rv.policy_from_workflow({
            "reverification": {"version": "custom-v9", "test": []},
        })
        assert policy["version"] == "custom-v9"


class TestScopeMatching:
    @pytest.mark.parametrize("pattern,path,expected", [
        # docs/**/*.md reaches the top of docs and every depth below it,
        # and nothing outside docs/.
        ("docs/**/*.md", "docs/a.md", True),
        ("docs/**/*.md", "docs/x/y/a.md", True),
        ("docs/**/*.md", "docs/a.txt", False),
        ("docs/**/*.md", "herdr/a.md", False),
        ("docs/**/*.md", "README.md", False),
        ("docs/**/*.md", "docs/context/a.txt", False),
        # a single * must not cross a path separator
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "docs/x/a.md", False),
        # docs/** is everything under docs, not the directory itself
        ("docs/**", "docs/a.md", True),
        ("docs/**", "docs/x/y/a.md", True),
        ("docs/**", "docs", False),
        ("docs/**", "docsx/a.md", False),
        # leading **/ behaves like gitignore
        ("**/*.md", "README.md", True),
        ("**/*.md", "herdr/a.py", False),
        # non-md files under docs stay OUT: docs/context/*.txt is not
        # declared non-impact by the v1 policy.
        ("docs/**/*.md", "docs/context/herdr-agent-pool-context.txt", False),
    ])
    def test_scope_match(self, pattern, path, expected):
        assert rv.scope_matches(path, pattern) is expected

    def test_relative_dot_prefix_is_never_safe(self):
        assert rv.scope_matches("./docs/a.md", "docs/**/*.md") is False


# ------------------------------------------------------------- git diff ---

class TestParseNameStatus:
    def test_modify_add_delete(self):
        raw = "M\0docs/a.md\0A\0new-engine/x.xyz\0D\0services/foo.py\0"
        entries = rv.parse_name_status(raw)
        assert entries == [
            {"status": "M", "old_path": "docs/a.md", "new_path": "docs/a.md"},
            {"status": "A", "old_path": "", "new_path": "new-engine/x.xyz"},
            {"status": "D", "old_path": "services/foo.py", "new_path": ""},
        ]

    def test_rename_keeps_both_sides(self):
        raw = "R100\0herdr/foo.py\0docs/foo.md\0"
        entries = rv.parse_name_status(raw)
        assert entries == [
            {"status": "R", "old_path": "herdr/foo.py", "new_path": "docs/foo.md"},
        ]

    def test_copy_keeps_both_sides(self):
        raw = "C075\0a.py\0b.py\0"
        assert rv.parse_name_status(raw) == [
            {"status": "C", "old_path": "a.py", "new_path": "b.py"},
        ]

    def test_type_change_reports_one_path(self):
        """git --name-status -z 对 T 只给一个路径(已实测),按 M 处理。"""
        assert rv.parse_name_status("T\0a.txt\0") == [
            {"status": "T", "old_path": "a.txt", "new_path": "a.txt"},
        ]

    def test_empty_output_is_an_empty_diff(self):
        assert rv.parse_name_status("") == []

    @pytest.mark.parametrize("raw", [
        "Z\0a.md\0",              # unknown status letter
        "M\0",                    # modify with no path
        "R100\0only-one.py\0",    # rename missing its destination
        "\0\0",                   # empty path
        "M\0a.md\0M\0",           # trailing status with no path
        b"M\0a.md\0",             # not text
        None,
        42,
    ])
    def test_unparsable_output_is_rejected(self, raw):
        assert rv.parse_name_status(raw) is None

    def test_paths_with_spaces_survive(self):
        raw = "M\0docs/a b c.md\0"
        assert rv.parse_name_status(raw) == [
            {"status": "M", "old_path": "docs/a b c.md",
             "new_path": "docs/a b c.md"},
        ]

    def test_changed_paths_cover_both_rename_sides(self):
        entries = rv.parse_name_status(
            "R100\0herdr/foo.py\0docs/foo.md\0M\0README.md\0")
        assert rv.changed_paths(entries) == [
            "README.md", "docs/foo.md", "herdr/foo.py",
        ]


# ------------------------------------------------------- impact decision ---

def _policy(test_scope=("docs/**/*.md",), review_scope=()):
    return {
        "version": rv.POLICY_VERSION,
        "verifiers": {
            "test": {"reusable_only_if_changes_within": list(test_scope)},
            "review": {"reusable_only_if_changes_within": list(review_scope)},
        },
    }


def _entries(*pairs):
    return [
        {"status": st, "old_path": old, "new_path": new} for st, old, new in pairs
    ]


class TestEvaluateVerifierImpact:
    def test_case1_docs_only_reuse(self):
        """§31 Case 1: 只改 docs/user-guide.md -> test 可复用。"""
        out = rv.evaluate_verifier_impact(
            "test", _entries(("M", "", "docs/user-guide.md")), _policy())
        assert out["decision"] == rv.DECISION_REUSE
        assert out["reason"] == rv.REASON_REUSE_NON_IMPACT

    def test_case2_code_change_reruns(self):
        """§31 Case 2: herdr/scheduler.py 变化 -> test 必须重跑。"""
        out = rv.evaluate_verifier_impact(
            "test", _entries(("M", "", "herdr/scheduler.py")), _policy())
        assert out["decision"] == rv.DECISION_RERUN
        assert out["reason"] == rv.REASON_RERUN_OUTSIDE_SCOPE
        assert "herdr/scheduler.py" in out["out_of_scope_paths"]

    def test_case3_unknown_directory_reruns(self):
        """§31 Case 3: new-engine/foo.xyz 未知目录 -> RERUN。"""
        out = rv.evaluate_verifier_impact(
            "test", _entries(("A", "", "new-engine/foo.xyz")), _policy())
        assert out["decision"] == rv.DECISION_RERUN

    def test_case4_mixed_diff_does_not_reuse(self):
        """§31 Case 4: docs + 代码混合不得因为含 docs 就复用。"""
        out = rv.evaluate_verifier_impact("test", _entries(
            ("M", "", "docs/a.md"), ("M", "", "herdr/a.py")), _policy())
        assert out["decision"] == rv.DECISION_RERUN
        assert out["out_of_scope_paths"] == ["herdr/a.py"]

    def test_case5_delete_reruns(self):
        """§31 Case 5: D services/foo.py -> RERUN。"""
        out = rv.evaluate_verifier_impact(
            "test", _entries(("D", "services/foo.py", "")), _policy())
        assert out["decision"] == rv.DECISION_RERUN

    def test_case6_rename_checks_old_and_new(self):
        """§31 Case 6: R herdr/foo.py -> docs/foo.md 必须看 old_path。"""
        out = rv.evaluate_verifier_impact(
            "test", _entries(("R", "herdr/foo.py", "docs/foo.md")), _policy())
        assert out["decision"] == rv.DECISION_RERUN
        assert "herdr/foo.py" in out["out_of_scope_paths"]

    def test_rename_within_scope_reuses(self):
        out = rv.evaluate_verifier_impact(
            "test", _entries(("R", "docs/a.md", "docs/b.md")), _policy())
        assert out["decision"] == rv.DECISION_REUSE

    def test_rename_out_of_docs_is_not_docs_only(self):
        """反向:old 在 docs,new 在代码目录,同样 RERUN。"""
        out = rv.evaluate_verifier_impact(
            "test", _entries(("R", "docs/a.md", "herdr/a.py")), _policy())
        assert out["decision"] == rv.DECISION_RERUN
        assert "herdr/a.py" in out["out_of_scope_paths"]

    def test_review_never_reuses_by_default(self):
        """§28/§38: review 在 v1 永远 RERUN。"""
        out = rv.evaluate_verifier_impact(
            "review", _entries(("M", "", "docs/user-guide.md")), _policy())
        assert out["decision"] == rv.DECISION_RERUN
        assert out["reason"] == rv.REASON_RERUN_NO_SCOPE

    def test_policy_absent_reruns(self):
        """§37.10: policy 缺失 -> RERUN。"""
        out = rv.evaluate_verifier_impact(
            "test", _entries(("M", "", "docs/a.md")), rv.default_policy())
        assert out["decision"] == rv.DECISION_RERUN
        assert out["reason"] == rv.REASON_RERUN_POLICY_ABSENT

    def test_empty_diff_does_not_grant_reuse(self):
        """A!=B 但树相同(空提交)时,无变化证据不得当作「无需重跑」。"""
        out = rv.evaluate_verifier_impact("test", [], _policy())
        assert out["decision"] == rv.DECISION_RERUN
        assert out["reason"] == rv.REASON_RERUN_NO_CHANGES

    def test_decision_is_deterministic(self):
        entries = _entries(("M", "", "docs/a.md"))
        first = rv.evaluate_verifier_impact("test", entries, _policy())
        second = rv.evaluate_verifier_impact("test", entries, _policy())
        assert first == second


# ----------------------------------------------------------------- plan ---

def _source(verifier="test", verdict="pass", verified="A", claim="A",
            task_id="wf-test-auto", source="fresh"):
    return {
        "verifier": verifier, "task_id": task_id, "verdict": verdict,
        "candidate_sha": claim, "verified_candidate_sha": verified,
        "source": source,
    }


class TestBuildReverificationPlan:
    def test_plan_shape_and_determinism(self):
        """§18: plan 必须 deterministic / explainable。"""
        entries = _entries(("M", "", "docs/user-guide.md"))
        sources = [_source()]
        policy = _policy()
        first = rv.build_reverification_plan(
            "wf-1", "A", "B", entries, sources, policy)
        second = rv.build_reverification_plan(
            "wf-1", "A", "B", entries, sources, policy)
        assert first == second
        assert first["from_candidate_sha"] == "A"
        assert first["to_candidate_sha"] == "B"
        assert first["changed_paths"] == ["docs/user-guide.md"]
        assert first["verifiers"]["test"]["decision"] == rv.DECISION_REUSE
        assert first["verifiers"]["test"]["source_task_id"] == "wf-test-auto"
        assert first["verifiers"]["review"]["decision"] == rv.DECISION_RERUN
        assert first["policy_version"] == rv.POLICY_VERSION

    def test_case9_blocked_source_cannot_be_reused(self):
        """§31 Case 9: test(A) BLOCKED 即使 docs-only 也不得复用。"""
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", _entries(("M", "", "docs/a.md")),
            [_source(verdict="blocked")], _policy())
        assert plan["verifiers"]["test"]["decision"] == rv.DECISION_RERUN
        assert plan["verifiers"]["test"]["reason"] == rv.REASON_RERUN_SOURCE_NOT_PASS

    def test_case10_missing_verified_candidate_sha_cannot_be_reused(self):
        """§31 Case 10: PASS 但没有 verified_candidate_sha -> RERUN。"""
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", _entries(("M", "", "docs/a.md")),
            [_source(verified="")], _policy())
        assert plan["verifiers"]["test"]["decision"] == rv.DECISION_RERUN
        assert plan["verifiers"]["test"]["reason"] == rv.REASON_RERUN_SOURCE_UNBOUND

    def test_source_must_bind_to_from_candidate(self):
        """§14: source 必须严格绑定 from_candidate。"""
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", _entries(("M", "", "docs/a.md")),
            [_source(verified="Z")], _policy())
        assert plan["verifiers"]["test"]["reason"] == rv.REASON_RERUN_SOURCE_FOREIGN

    def test_absent_source_reruns(self):
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", _entries(("M", "", "docs/a.md")), [], _policy())
        assert plan["verifiers"]["test"]["reason"] == rv.REASON_RERUN_SOURCE_ABSENT

    def test_case15_no_recursive_reuse_chain(self):
        """§15: B 上的 test 来自 reuse 而非 fresh -> C 必须 RERUN。"""
        plan = rv.build_reverification_plan(
            "wf-1", "B", "C", _entries(("M", "", "docs/a.md")),
            [_source(claim="B", verified="B", source="reuse")], _policy())
        assert plan["verifiers"]["test"]["decision"] == rv.DECISION_RERUN
        assert plan["verifiers"]["test"]["reason"] == rv.REASON_RERUN_SOURCE_NOT_FRESH

    def test_diff_unavailable_reruns_everything(self):
        """§8: diff 不可得 -> 全部 RERUN,不做任何「看起来没问题」判断。"""
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", None, [_source()], _policy(),
            diff_reason=rv.REASON_RERUN_DIFF_UNAVAILABLE)
        for name in ("test", "review"):
            assert plan["verifiers"][name]["decision"] == rv.DECISION_RERUN
            assert plan["verifiers"][name]["reason"] == rv.REASON_RERUN_DIFF_UNAVAILABLE

    def test_non_linear_candidate_reruns_everything(self):
        """§16: 非线性历史 -> 全部 RERUN。"""
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", None, [_source()], _policy(),
            diff_reason=rv.REASON_RERUN_NON_LINEAR)
        assert all(
            plan["verifiers"][n]["reason"] == rv.REASON_RERUN_NON_LINEAR
            for n in ("test", "review")
        )

    def test_evidence_reason_wins_over_scope_reason(self):
        """源码证据缺失时,理由必须指向证据,而不是含糊地说「路径不安全」。"""
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", _entries(("M", "", "herdr/a.py")),
            [], _policy())
        assert plan["verifiers"]["test"]["reason"] == rv.REASON_RERUN_OUTSIDE_SCOPE

    def test_policy_version_enters_the_plan(self):
        """§27: policy_version 必须进入事实。"""
        policy = _policy()
        policy["version"] = "selective-reverification-v1.1"
        plan = rv.build_reverification_plan(
            "wf-1", "A", "B", _entries(("M", "", "docs/a.md")),
            [_source()], policy)
        assert plan["policy_version"] == "selective-reverification-v1.1"
        assert plan["verifiers"]["test"]["policy_version"] == \
            "selective-reverification-v1.1"


class TestDecisionIdentity:
    def test_identity_is_stable(self):
        args = ("wf-1", "A", "B", "test", rv.POLICY_VERSION)
        assert rv.decision_identity(*args) == rv.decision_identity(*args)

    def test_true_aba_same_sha_on_both_ends_is_not_an_episode(self, db):
        """§17/§26: A -> A is the same candidate, never a reuse episode.

        The controller-level ABA tests are A -> B -> C. This pins the genuinely
        same-SHA case, which must be rejected at the fact boundary: recording it
        would let a "reuse" claim exist with no candidate change behind it.
        """
        result = facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "A",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
        }, db_path=db)
        assert result["status"] == "rejected"
        assert result["reason"] == "same_candidate"

    def test_aba_rotation_uses_distinct_episodes(self, db):
        """A -> B and B -> A are separate facts, each resolving to its own target."""
        for src, dst in (("A", "B"), ("B", "A")):
            facts.record_reverification_decision("wf-facts-rev", {
                "decision": rv.DECISION_REUSE, "verifier": "test",
                "from_candidate_sha": src, "to_candidate_sha": dst,
                "source_task_id": f"t-{src}", "source_verdict": "pass",
                "source_verified_candidate_sha": src,
                "policy_identity": _policy_identity(),
            }, db_path=db)
        assert len(facts.list_reverification_decisions(
            "wf-facts-rev", db_path=db)) == 2
        policy = _policy_identity()
        assert facts.find_reuse_fact("wf-facts-rev", "test", "B",
                                     policy_identity=policy,
                                     db_path=db)["from_candidate_sha"] == "A"
        assert facts.find_reuse_fact("wf-facts-rev", "test", "A",
                                     policy_identity=policy,
                                     db_path=db)["from_candidate_sha"] == "B"

    def test_policy_identity_scopes_the_lookup(self, db):
        """A fact is honoured only under the policy that authorised it.

        Keyed on the *resolved* policy, not the version label: narrowing a
        scope does not change the version, and a version-keyed filter would keep
        honouring reuse the operator has just revoked.
        """
        wide = rv.policy_from_workflow({
            "reverification": {"test": {"reusable_only_if_changes_within":
                                       ["docs/**/*.md"]}}})
        narrow = rv.policy_from_workflow({
            "reverification": {"test": {"reusable_only_if_changes_within":
                                       ["docs/adr/**"]}}})
        assert rv.policy_identity(wide) != rv.policy_identity(narrow)

        facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
            "policy_version": wide["version"],
            "policy_identity": rv.policy_identity(wide),
        }, db_path=db)
        assert facts.find_reuse_fact(
            "wf-facts-rev", "test", "B",
            policy_identity=rv.policy_identity(wide), db_path=db)
        assert facts.find_reuse_fact(
            "wf-facts-rev", "test", "B",
            policy_identity=rv.policy_identity(narrow), db_path=db) is None
        assert facts.find_reuse_fact(
            "wf-facts-rev", "test", "B", policy_identity="", db_path=db) is None

    def test_removing_the_block_changes_the_policy_identity(self, db):
        """Deleting the config must revoke reuse, not be a no-op."""
        declared = rv.policy_from_workflow({
            "reverification": {"test": {"reusable_only_if_changes_within":
                                       ["docs/**/*.md"]}}})
        removed = rv.policy_from_workflow({"name": "x", "nodes": []})
        assert rv.policy_identity(declared) != rv.policy_identity(removed)

    def test_fact_carries_the_scope_that_authorised_it(self, db):
        """§32: the fact must explain itself without its parent plan."""
        facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
            "changed_paths": ["docs/a.md"],
            "reusable_scope": ["docs/**/*.md"],
            "out_of_scope_paths": [],
        }, db_path=db)
        payload = facts.list_reverification_decisions(
            "wf-facts-rev", db_path=db)[0]["payload"]
        assert payload["reusable_scope"] == ["docs/**/*.md"]
        assert payload["out_of_scope_paths"] == []

    def test_caller_supplied_identity_must_match_the_fact(self, db):
        """The identity is recomputed, never taken on trust from the payload."""
        payload = {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
            "policy_version": "selective-reverification-v1",
        }
        forged = dict(payload, decision_identity="rever_deadbeef")
        result = facts.record_reverification_decision(
            "wf-facts-rev", forged, db_path=db)
        assert result["status"] == "rejected"
        assert result["reason"] == "decision_identity_mismatch"
        assert facts.list_reverification_decisions("wf-facts-rev", db_path=db) == []

        # The honest identity is accepted and is the recomputed one.
        ok = facts.record_reverification_decision(
            "wf-facts-rev", dict(payload, decision_identity=result["expected"]),
            db_path=db)
        assert ok["status"] == "created"

    def test_changed_policy_version_is_a_distinct_fact(self, db):
        """A different policy version is a different decision, not a replay."""
        base = {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
            "policy_version": "selective-reverification-v1",
        }
        facts.record_reverification_decision("wf-facts-rev", base, db_path=db)
        result = facts.record_reverification_decision("wf-facts-rev", dict(
            base, policy_version="selective-reverification-v1-strict"),
            db_path=db)
        assert result["status"] == "created"
        assert len(facts.list_reverification_decisions(
            "wf-facts-rev", db_path=db)) == 2

    def test_case14_aba_episodes_are_distinct(self):
        """§26/§31 Case 14: A->B 与 B->A 必须是不同 episode。"""
        ab = rv.decision_identity("wf-1", "A", "B", "test", rv.POLICY_VERSION)
        ba = rv.decision_identity("wf-1", "B", "A", "test", rv.POLICY_VERSION)
        assert ab != ba

    def test_identity_separates_verifiers(self):
        t = rv.decision_identity("wf-1", "A", "B", "test", rv.POLICY_VERSION)
        r = rv.decision_identity("wf-1", "A", "B", "review", rv.POLICY_VERSION)
        assert t != r

    def test_identity_separates_policy_versions(self):
        one = rv.decision_identity("wf-1", "A", "B", "test", "v1")
        two = rv.decision_identity("wf-1", "A", "B", "test", "v2")
        assert one != two

    def test_identity_has_no_ambiguous_delimiter_collision(self):
        """字段边界必须保留:("a|b","c") 不得等于 ("a","b|c")。"""
        left = rv.decision_identity("wf-1", "A|B", "C", "test", "v1")
        right = rv.decision_identity("wf-1", "A", "B|C", "test", "v1")
        assert left != right

    def test_plan_identity_covers_the_episode(self):
        ab = rv.plan_identity("wf-1", "A", "B", rv.POLICY_VERSION)
        ba = rv.plan_identity("wf-1", "B", "A", rv.POLICY_VERSION)
        assert ab != ba


class TestMetrics:
    def test_counts_and_rate(self):
        """§33: 可计算的最小指标集。"""
        decisions = [
            {"decision": rv.DECISION_REUSE},
            {"decision": rv.DECISION_RERUN},
            {"decision": rv.DECISION_REUSE},
            {"decision": rv.DECISION_RERUN},
        ]
        out = rv.reverification_metrics(decisions)
        assert out["total_verifiers"] == 4
        assert out["reuse_count"] == 2
        assert out["rerun_count"] == 2
        assert out["reuse_rate"] == pytest.approx(0.5)

    def test_empty_metrics_do_not_divide_by_zero(self):
        out = rv.reverification_metrics([])
        assert out == {
            "total_verifiers": 0, "rerun_count": 0, "reuse_count": 0,
            "reuse_rate": 0.0,
        }


# ------------------------------------------------------------ git facts ---

def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args), check=check,
        text=True, capture_output=True)


class _Repo:
    """真实临时 git 仓库,用于验证 diff / ancestor 事实层。"""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="herdr-rever-")
        self.path = Path(self._tmp.name)
        _git(self.path, "init", "-q", ".")
        _git(self.path, "config", "user.email", "t@t")
        _git(self.path, "config", "user.name", "t")
        _git(self.path, "commit", "-q", "--allow-empty", "-m", "root",
             "--no-gpg-sign")
        self.default_branch = _git(
            self.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def write(self, rel, text="x"):
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def remove(self, rel):
        (self.path / rel).unlink()

    def mv(self, src, dst):
        (self.path / dst).parent.mkdir(parents=True, exist_ok=True)
        _git(self.path, "mv", src, dst)

    def commit(self, message="c"):
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", message, "--no-gpg-sign")
        return self.head

    def branch_off(self, name):
        _git(self.path, "checkout", "-q", "-b", name)
        return self

    def checkout(self, ref):
        _git(self.path, "checkout", "-q", ref)

    @property
    def head(self):
        return _git(self.path, "rev-parse", "HEAD").stdout.strip()

    def cleanup(self):
        self._tmp.cleanup()


@pytest.fixture
def repo():
    r = _Repo()
    yield r
    r.cleanup()


class TestGitFacts:
    def test_diff_is_the_source_of_truth(self, repo):
        """§7: 变更事实必须来自真实 git diff。"""
        a = repo.head
        repo.write("docs/user-guide.md", "doc")
        b = repo.commit("docs")

        entries, reason = rv.compute_candidate_diff(repo.path, a, b)
        assert reason == ""
        assert rv.changed_paths(entries) == ["docs/user-guide.md"]
        assert entries[0]["status"] == "A"

    def test_abbreviated_sha_is_canonicalized(self, repo):
        a = repo.head
        repo.write("docs/a.md", "x")
        b = repo.commit("d")
        short = a[:8]
        assert rv.canonicalize_sha(repo.path, short) == a
        entries, reason = rv.compute_candidate_diff(repo.path, short, b)
        assert reason == "" and entries is not None

    def test_unresolvable_sha_yields_empty(self, repo):
        assert rv.canonicalize_sha(repo.path, "deadbeefdeadbeef") == ""
        assert rv.canonicalize_sha(repo.path, "") == ""

    def test_diff_unavailable_when_sha_missing(self, repo):
        entries, reason = rv.compute_candidate_diff(
            repo.path, "deadbeefdeadbeef", repo.head)
        assert entries is None
        assert reason == rv.REASON_RERUN_DIFF_UNAVAILABLE

    def test_diff_unavailable_outside_repository(self, tmp_path):
        entries, reason = rv.compute_candidate_diff(tmp_path, "a" * 40, "b" * 40)
        assert entries is None
        assert reason == rv.REASON_RERUN_DIFF_UNAVAILABLE

    def test_case8_non_linear_candidate_is_refused(self, repo):
        """§31 Case 8: A 不是 B 的祖先 -> 全部 RERUN。"""
        # Both candidates must be siblings off the same root, otherwise `side`
        # would simply be a descendant and the history would still be linear.
        repo.branch_off("side")
        repo.checkout(repo.default_branch)
        repo.write("herdr/left.py", "l")
        left = repo.commit("left")
        repo.checkout("side")
        repo.write("herdr/right.py", "r")
        right = repo.commit("right")
        repo.checkout(repo.default_branch)

        assert rv.is_ancestor(repo.path, left, right) is False
        assert rv.is_ancestor(repo.path, right, left) is False
        entries, reason = rv.collect_candidate_changes(repo.path, left, right)
        assert entries is None
        assert reason == rv.REASON_RERUN_NON_LINEAR

    def test_ancestor_direction_matters(self, repo):
        a = repo.head
        repo.write("docs/a.md", "x")
        b = repo.commit("d")
        assert rv.is_ancestor(repo.path, a, b) is True
        assert rv.is_ancestor(repo.path, b, a) is False

    def test_rename_records_both_paths(self, repo):
        repo.write("herdr/foo.py", "c")
        repo.write("docs/keep.md", "k")
        a = repo.commit("seed")
        repo.mv("herdr/foo.py", "docs/foo.md")
        b = repo.commit("rename")

        entries, reason = rv.compute_candidate_diff(repo.path, a, b)
        assert reason == ""
        assert rv.changed_paths(entries) == ["docs/foo.md", "herdr/foo.py"]

    def test_delete_records_the_removed_path(self, repo):
        repo.write("services/foo.py", "c")
        a = repo.commit("seed")
        repo.remove("services/foo.py")
        b = repo.commit("del")

        entries, _reason = rv.compute_candidate_diff(repo.path, a, b)
        assert rv.changed_paths(entries) == ["services/foo.py"]
        assert entries[0]["status"] == "D"

    def test_identical_sha_is_not_a_candidate_change(self, repo):
        """§17: A → A 不是 candidate change。"""
        entries, reason = rv.collect_candidate_changes(
            repo.path, repo.head, repo.head)
        assert entries is None
        assert reason == rv.REASON_RERUN_DIFF_UNAVAILABLE

    def test_collect_chain_produces_entries_for_docs_only(self, repo):
        a = repo.head
        repo.write("docs/user-guide.md", "d")
        b = repo.commit("docs")
        entries, reason = rv.collect_candidate_changes(repo.path, a, b)
        assert reason == ""
        plan = rv.build_reverification_plan(
            "wf-git", a, b, entries,
            [_source(claim=a, verified=a)], _policy())
        assert plan["verifiers"]["test"]["decision"] == rv.DECISION_REUSE
        assert plan["verifiers"]["review"]["decision"] == rv.DECISION_RERUN


# ---------------------------------------------------------- fact store ---

class TestReverificationFactStore:
    def test_reuse_fact_never_mutates_the_source_task(self, db):
        """§5/§38: 历史验证不可变,复用是新的派生事实。"""
        store = get_state_store(db)
        store.save_task({
            "task_id": "wf-test-auto", "workflow_id": "wf-facts-rev",
            "node": "test", "status": "completed", "stage_verdict": "pass",
            "candidate_sha": "A", "verified_candidate_sha": "A",
        })
        facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "wf-test-auto", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
        }, db_path=db)

        task = get_state_store(db).get_task("wf-test-auto")
        assert task["candidate_sha"] == "A"
        assert task["verified_candidate_sha"] == "A"
        assert task["stage_verdict"] == "pass"

    def test_fact_records_the_proof_it_relies_on(self, db):
        facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "wf-test-auto", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
            "changed_paths": ["docs/a.md"],
            "policy_version": rv.POLICY_VERSION,
        }, db_path=db)
        payload = facts.list_reverification_decisions("wf-facts-rev", db_path=db)[0]["payload"]
        assert payload["source_verdict"] == "pass"
        assert payload["source_verified_candidate_sha"] == "A"
        assert payload["policy_version"] == rv.POLICY_VERSION
        assert payload["decision_identity"]
        assert payload["created_at"] > 0

    def test_same_episode_is_idempotent(self, db):
        """§25: 同一事实重复执行必须幂等。"""
        payload = {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "wf-test-auto", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
        }
        first = facts.record_reverification_decision(
            "wf-facts-rev", payload, db_path=db)
        second = facts.record_reverification_decision(
            "wf-facts-rev", payload, db_path=db)
        assert first["status"] == "created"
        assert second["status"] == "exists"
        assert len(facts.list_reverification_decisions(
            "wf-facts-rev", db_path=db)) == 1

    def test_case14_aba_produces_two_distinct_facts(self, db):
        """§26/§31 Case 14: A→B 与 B→A 是不同 episode。"""
        for src, dst in (("A", "B"), ("B", "A")):
            facts.record_reverification_decision("wf-facts-rev", {
                "decision": rv.DECISION_REUSE, "verifier": "test",
                "from_candidate_sha": src, "to_candidate_sha": dst,
                "source_task_id": f"t-{src}", "source_verdict": "pass",
                "source_verified_candidate_sha": src,
                "policy_identity": _policy_identity(),
            }, db_path=db)
        recorded = facts.list_reverification_decisions("wf-facts-rev", db_path=db)
        assert len(recorded) == 2
        assert {e["payload"]["from_candidate_sha"] for e in recorded} == {"A", "B"}

    def test_lookup_is_bound_to_the_exact_to_candidate(self, db):
        """§23: 复用事实只能满足它自己记录的 to_candidate。"""
        facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
            "policy_identity": _policy_identity(),
        }, db_path=db)
        assert facts.find_reuse_fact("wf-facts-rev", "test", "B",
                                     policy_identity=_policy_identity(), db_path=db)
        assert facts.find_reuse_fact("wf-facts-rev", "test", "C",
                                     policy_identity=_policy_identity(), db_path=db) is None
        assert facts.find_reuse_fact("wf-facts-rev", "review", "B",
                                     policy_identity=_policy_identity(), db_path=db) is None

    def test_facts_are_scoped_per_workflow(self, db):
        facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
        }, db_path=db)
        assert facts.find_reuse_fact("other-wf", "test", "B",
                                     policy_identity=_policy_identity(), db_path=db) is None

    def test_reuse_requires_a_passed_source(self, db):
        """§13: 只有 PASS 可以成为 reuse source。"""
        result = facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "blocked",
            "source_verified_candidate_sha": "A",
        }, db_path=db)
        assert result["status"] == "rejected"
        assert facts.list_reverification_decisions("wf-facts-rev", db_path=db) == []

    def test_reuse_requires_a_bound_source(self, db):
        """§14: 没有 verified_candidate_sha 的 PASS 不可复用。"""
        result = facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "",
        }, db_path=db)
        assert result["status"] == "rejected"

    def test_reuse_requires_an_exact_from_candidate(self, db):
        result = facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "Z",
        }, db_path=db)
        assert result["status"] == "rejected"

    def test_reuse_requires_a_distinct_target_candidate(self, db):
        """§17: A → A 不是一次重新验证。"""
        result = facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_REUSE, "verifier": "test",
            "from_candidate_sha": "A", "to_candidate_sha": "A",
            "source_task_id": "t-A", "source_verdict": "pass",
            "source_verified_candidate_sha": "A",
        }, db_path=db)
        assert result["status"] == "rejected"

    def test_rerun_decisions_are_recordable(self, db):
        result = facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_RERUN, "verifier": "review",
            "from_candidate_sha": "A", "to_candidate_sha": "B",
            "reason": rv.REASON_RERUN_NO_SCOPE,
        }, db_path=db)
        assert result["status"] == "created"
        assert facts.find_reuse_fact("wf-facts-rev", "review", "B",
                                     policy_identity=_policy_identity(), db_path=db) is None

    def test_missing_identity_fields_are_rejected(self, db):
        result = facts.record_reverification_decision("wf-facts-rev", {
            "decision": rv.DECISION_RERUN, "verifier": "test",
            "from_candidate_sha": "", "to_candidate_sha": "B",
        }, db_path=db)
        assert result["status"] == "rejected"


# ------------------------------------------------------- join gate path ---

WF = "wf-rever-gate"
GATE = {"id": "wrapup", "depends_on": ["test", "review"]}


def _task(node, sha, *, verdict="pass", status="completed", task_id=None):
    return {
        "task_id": task_id or f"{WF}-{node}-auto", "workflow_id": WF,
        "node": node, "stage": node, "status": status,
        "stage_verdict": verdict, "candidate_sha": sha,
        "verified_candidate_sha": sha,
    }


def _reuse(node="test", to_sha="B", **extra):
    fact = {
        "verifier": node, "decision": rv.DECISION_REUSE,
        "from_candidate_sha": "A", "to_candidate_sha": to_sha,
        "source_task_id": f"{WF}-{node}-auto", "source_verdict": "pass",
        "source_verified_candidate_sha": "A",
        "policy_version": rv.POLICY_VERSION,
    }
    fact.update(extra)
    return fact


class TestEffectiveVerification:
    def test_fresh_pass_wins_over_reuse(self):
        """§22: fresh 永远优先于 reuse。"""
        out = sched.resolve_effective_verification(
            [_task("test", "B")], WF, "test", "B", [_reuse()])
        assert out["status"] == "pass"
        assert out["source"] == "fresh"

    def test_fresh_blocked_is_not_overridden_by_reuse(self, db):
        """§31 Case 12 / §37.7: fresh BLOCKED 不能被旧 reuse PASS 覆盖。"""
        out = sched.resolve_effective_verification(
            [_task("test", "B", verdict="blocked")], WF, "test", "B",
            [_reuse()])
        assert out["status"] == "blocked"
        assert out["source"] == "fresh"

    def test_reuse_applies_when_no_fresh_evidence_exists(self):
        out = sched.resolve_effective_verification(
            [], WF, "test", "B", [_reuse()])
        assert out["status"] == "pass"
        assert out["source"] == "reuse"
        assert out["source_candidate_sha"] == "A"

    def test_no_evidence_is_not_a_pass(self):
        out = sched.resolve_effective_verification([], WF, "test", "B", [])
        assert out["status"] == "none"
        assert out["source"] == "none"

    def test_case11_reuse_is_bound_to_one_candidate(self):
        """§31 Case 11: A→B 的复用不能满足 C。"""
        out = sched.resolve_effective_verification(
            [], WF, "test", "C", [_reuse()])
        assert out["status"] == "none"

    def test_reuse_for_another_verifier_is_ignored(self):
        out = sched.resolve_effective_verification(
            [], WF, "test", "B", [_reuse(node="review")])
        assert out["status"] == "none"

    def test_fresh_on_another_candidate_does_not_count(self):
        out = sched.resolve_effective_verification(
            [_task("test", "A")], WF, "test", "B", [])
        assert out["status"] == "stale"

    def test_incomplete_fresh_task_is_not_a_verdict(self):
        out = sched.resolve_effective_verification(
            [_task("test", "B", status="running", verdict="")], WF, "test", "B",
            [_reuse()])
        assert out["status"] == "pending"

    def test_missing_completion_evidence_is_unproven(self):
        """§14: 有 claim 无 verified_candidate_sha 不得当成 pass。"""
        task = _task("test", "B")
        task["verified_candidate_sha"] = ""
        out = sched.resolve_effective_verification(
            [task], WF, "test", "B", [])
        assert out["status"] == "unproven"


class TestJoinGateWithReuse:
    def test_reuse_plus_fresh_review_passes(self):
        """§20: test 复用 + review(B) fresh PASS -> 门禁放行。"""
        tasks = [_task("review", "B")]
        passed, reason, details = sched.evaluate_join_gate(
            GATE, tasks, WF, "B", reuse_facts=[_reuse()])
        assert passed is True
        assert details["branches"]["test"]["evidence_source"] == "reuse"
        assert details["branches"]["review"]["evidence_source"] == "fresh"

    def test_legacy_behaviour_is_unchanged_without_facts(self):
        """§38: 旧 workflow 保持兼容(默认参数等价于 #107)。"""
        tasks = [_task("test", "B"), _task("review", "B")]
        assert sched.evaluate_join_gate(GATE, tasks, WF, "B")[0] is True
        assert sched.evaluate_join_gate(GATE, tasks, WF, "B", reuse_facts=[])[0] is True

    def test_reuse_cannot_rescue_a_blocked_review(self):
        tasks = [_task("review", "B", verdict="blocked")]
        passed, reason, _details = sched.evaluate_join_gate(
            GATE, tasks, WF, "B", reuse_facts=[_reuse()])
        assert passed is False
        assert reason == sched.JOIN_BLOCKED

    def test_case12_fresh_blocked_beats_reuse(self):
        tasks = [_task("test", "B", verdict="blocked"),
                 _task("review", "B")]
        passed, reason, _details = sched.evaluate_join_gate(
            GATE, tasks, WF, "B", reuse_facts=[_reuse()])
        assert passed is False
        assert reason == sched.JOIN_BLOCKED

    def test_case11_reuse_of_another_candidate_does_not_pass(self):
        tasks = [_task("review", "B")]
        passed, reason, _details = sched.evaluate_join_gate(
            GATE, tasks, WF, "B", reuse_facts=[_reuse(to_sha="A")])
        assert passed is False
        assert reason == sched.JOIN_WAITING

    def test_reuse_with_no_expected_candidate_never_passes(self):
        """§37.10/§20: 没有冻结候选时,复用无法绑定,一律不认。"""
        tasks = [_task("review", "B")]
        passed, _reason, _details = sched.evaluate_join_gate(
            GATE, tasks, WF, "", reuse_facts=[_reuse()])
        assert passed is False

    def test_reuse_source_verdict_must_be_pass(self):
        tasks = [_task("review", "B")]
        passed, _reason, _details = sched.evaluate_join_gate(
            GATE, tasks, WF, "B",
            reuse_facts=[_reuse(source_verdict="blocked")])
        assert passed is False

    def test_reuse_reports_its_own_evidence(self):
        """§32: 复用必须能解释「为什么没重跑」。"""
        tasks = [_task("review", "B")]
        _passed, _reason, details = sched.evaluate_join_gate(
            GATE, tasks, WF, "B",
            reuse_facts=[_reuse(changed_paths=["docs/a.md"],
                                reason=rv.REASON_REUSE_NON_IMPACT)])
        assert details["branches"]["test"]["reuse"]["source_task_id"] == \
            f"{WF}-test-auto"
        assert details["branches"]["test"]["reuse"]["changed_paths"] == ["docs/a.md"]
