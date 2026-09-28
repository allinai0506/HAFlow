"""Critical-Path Scheduler v1 unit tests (HAFlow PR #107).

纯函数核心 tests/herdr.scheduler:场景映射到 PRD §31。
- 场景 1 (test/review 同时就绪): test_parallel_branches_ready_together
- 场景 2 (顺序语义保持): test_legacy_sequential_semantics_preserved
- 场景 4 (Join Gate PASS): test_join_gate_passes_on_same_sha
- 场景 5 (Join Gate FAIL): test_join_gate_refuses_on_mismatch
- 场景 6 (candidate 变更 -> stale): test_join_gate_stale_on_candidate_advance
- 场景 7 (test A / review B 拒绝): test_join_gate_refuses_divergent_branches
- 场景 8 (running 排除): test_running_nodes_excluded_from_ready
- 场景 12 (确定性重放): test_deterministic_replay_same_inputs
"""

import pytest

from herdr import scheduler as sched


def _node(node_id, deps=()):
    return {"id": node_id, "depends_on": list(deps)}


def _task(task_id, workflow_id, node, status="completed", verdict="pass", sha="",
          verified=None):
    """Build a task record.

    ``sha`` is the dispatch claim. ``verified`` is completion evidence; by
    default it mirrors the claim, modelling a verifier that completed and
    actually proved that revision. Pass ``verified=""`` to model a
    scheduler-managed task whose completion evidence is missing, which the
    join gate must refuse.
    """
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "node": node,
        "stage": node,
        "status": status,
        "stage_verdict": verdict,
    }
    if sha:
        task["candidate_sha"] = sha
        task["verified_candidate_sha"] = sha if verified is None else verified
    return task


WF = "wf-sched-v1"


class TestReadyNodes:
    def test_parallel_branches_ready_together(self):
        """场景 1:implementation 完成后 test 与 review 同时就绪。"""
        nodes = [
            _node("implementation"),
            _node("test", ["implementation"]),
            _node("review", ["implementation"]),
            _node("wrapup", ["test", "review"]),
        ]
        ready = sched.compute_ready_nodes(nodes, {"implementation"})
        assert sorted(n["id"] for n in ready) == ["review", "test"]

    def test_legacy_sequential_semantics_preserved(self):
        """场景 2:旧模板 review 依赖 test 时保持串行。"""
        nodes = [
            _node("implementation"),
            _node("test", ["implementation"]),
            _node("review", ["test"]),
            _node("wrapup", ["review"]),
        ]
        ready = sched.compute_ready_nodes(nodes, {"implementation"})
        assert [n["id"] for n in ready] == ["test"]
        ready2 = sched.compute_ready_nodes(nodes, {"implementation", "test"})
        assert [n["id"] for n in ready2] == ["review"]

    def test_running_nodes_excluded_from_ready(self):
        """场景 8:正在执行中的节点不重复进入 ready。"""
        nodes = [
            _node("test", ["implementation"]),
            _node("review", ["implementation"]),
        ]
        ready = sched.compute_ready_nodes(
            nodes, {"implementation"}, running_node_ids={"test"}
        )
        assert [n["id"] for n in ready] == ["review"]

    def test_deterministic_replay_same_inputs(self):
        """场景 12:相同输入 -> 相同 ready 集合(与 YAML 顺序无关)。"""
        nodes_a = [
            _node("review", ["implementation"]),
            _node("test", ["implementation"]),
        ]
        nodes_b = [
            _node("test", ["implementation"]),
            _node("review", ["implementation"]),
        ]
        ready_a = sorted(
            n["id"] for n in sched.compute_ready_nodes(nodes_a, {"implementation"})
        )
        ready_b = sorted(
            n["id"] for n in sched.compute_ready_nodes(nodes_b, {"implementation"})
        )
        assert ready_a == ready_b == ["review", "test"]

    def test_dependencies_satisfied(self):
        assert sched.dependencies_satisfied(_node("x", ["a", "b"]), {"a", "b"})
        assert not sched.dependencies_satisfied(_node("x", ["a", "b"]), {"a"})


class TestJoinGate:
    GATE = {"id": "wrapup", "depends_on": ["test", "review"]}

    def test_join_gate_passes_on_same_sha(self):
        """场景 4:同 SHA 全 pass -> PASS。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A"),
        ]
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is True
        assert reason == sched.JOIN_SATISFIED
        assert details["candidate_sha"] == "sha-A"

    def test_join_gate_waits_on_incomplete_branch(self):
        """review 还在 running -> WAIT。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", status="working",
                  verdict="", sha="sha-A"),
        ]
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is False
        assert reason == sched.JOIN_WAITING
        assert details["incomplete"] == ["review"]

    def test_join_gate_refuses_on_blocked_verdict(self):
        """场景 5 一半:test blocked -> BLOCKED。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", verdict="blocked", sha="sha-A"),
        ]
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is False
        assert reason == sched.JOIN_BLOCKED
        assert details["blocked"] == ["review"]

    def test_join_gate_refuses_on_mismatch(self):
        """场景 5:test(A)/review(B) -> MISMATCH,Fail-Closed。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-B"),
        ]
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF
        )
        assert passed is False
        assert reason == sched.JOIN_CANDIDATE_MISMATCH
        assert details["observed_candidate_shas"] == ["sha-A", "sha-B"]

    def test_join_gate_stale_on_candidate_advance(self):
        """场景 6:分支验证 A,但当前候选已是 B -> STALE。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A"),
        ]
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-B"
        )
        assert passed is False
        assert reason == sched.JOIN_STALE

    def test_join_gate_refuses_divergent_branches(self):
        """场景 7:test 验证 A、review 验证 B -> 拒绝汇聚。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-B"),
        ]
        assert sched.join_ready(self.GATE, tasks, WF) is False
        frozen, frozen_details = sched.candidate_frozen_for_nodes(
            tasks, WF, ["test", "review"], "sha-A"
        )
        assert frozen is False
        assert frozen_details["nodes"]["review"]["foreign_candidate_shas"] == ["sha-B"]

    def test_join_gate_missing_candidate_refuses(self):
        """前置缺 candidate_sha -> 无法证明同版本,拒绝。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review"),
        ]
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is False
        assert reason == sched.JOIN_MISSING_CANDIDATE

    def test_join_gate_refuses_claim_evidence_mismatch(self):
        """P1 回归:派发声明 A 但完成时验证了 B -> 拒绝汇聚(证据优先)。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A", verified="sha-B"),
        ]
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is False
        assert reason == sched.JOIN_EVIDENCE_MISMATCH

    def test_join_gate_refuses_missing_completion_evidence(self):
        """P1 回归:有候选声明但无完成证据 -> 不得用启动证据放行。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A", verified=""),
            _task("t-review", WF, "review", sha="sha-A", verified=""),
        ]
        for t in tasks:
            t["baseline_commit"] = "sha-A"
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is False
        assert reason == sched.JOIN_MISSING_CANDIDATE
        assert "missing_completion_evidence" in details
        # 缺证据必须报告为「未证明」,不得混同为「证据矛盾」
        missing = details["missing_completion_evidence"]
        assert sorted(missing) == ["review", "test"]
        assert "evidence_mismatch" not in details
        assert missing["test"][0]["claim"] == "sha-A"

    def test_join_gate_reports_mismatch_and_missing_evidence_separately(self):
        """同时存在「缺证据」与「证据矛盾」时,优先报缺证据。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A", verified=""),
            _task("t-review", WF, "review", sha="sha-A", verified="sha-B"),
        ]
        tasks[0]["baseline_commit"] = "sha-A"
        passed, reason, details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is False
        assert reason == sched.JOIN_MISSING_CANDIDATE
        assert list(details["missing_completion_evidence"]) == ["test"]

    def test_join_gate_accepts_matching_claim_and_evidence(self):
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A"),
        ]
        for task in tasks:
            task["baseline_commit"] = "sha-A"
        passed, reason, _details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is True
        assert reason == sched.JOIN_SATISFIED

    def test_superseded_tasks_ignored(self):
        """被作废任务不污染汇聚判定。"""
        stale = _task("t-test-old", WF, "test", status="superseded",
                      verdict="blocked", sha="sha-OLD")
        stale["superseded_by"] = "t-test"
        tasks = [
            stale,
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A"),
        ]
        passed, reason, _details = sched.evaluate_join_gate(
            self.GATE, tasks, WF, expected_candidate_sha="sha-A"
        )
        assert passed is True
        assert reason == sched.JOIN_SATISFIED


class TestCandidateBinding:
    def test_evidence_wins_over_claim(self):
        """P1 回归:claim=A 但完成时验证的是 B,证据(B)必须胜出。"""
        task = {
            "candidate_sha": "sha-A",
            "baseline_commit": "sha-A",
            "verified_candidate_sha": "sha-B",
        }
        assert sched.extract_task_candidate_claim(task) == "sha-A"
        assert sched.extract_task_verified_sha(task) == "sha-B"
        assert sched.extract_task_candidate_sha(task) == "sha-B"

    def test_claim_evidence_consistency(self):
        ok, claim, evidence = sched.task_claim_evidence_consistent(
            {"candidate_sha": "sha-A", "verified_candidate_sha": "sha-A"})
        assert (ok, claim, evidence) == (True, "sha-A", "sha-A")
        ok, claim, evidence = sched.task_claim_evidence_consistent(
            {"candidate_sha": "sha-A", "verified_candidate_sha": "sha-B"})
        assert (ok, claim, evidence) == (False, "sha-A", "sha-B")
        # 声明了候选却没有任何完成证据:这是未证明,不是一致
        ok, _claim, evidence = sched.task_claim_evidence_consistent(
            {"candidate_sha": "sha-A"})
        assert ok is False and evidence == ""
        # 完全没有候选声明的旧任务:evidence 仍可读出,但不存在需要支撑的声明,
        # 由调用方按 legacy 语义跳过,不是矛盾。
        legacy = {"baseline_commit": "sha-A"}
        assert not sched.is_scheduler_managed_task(legacy)
        assert sched.extract_task_verified_sha(legacy) == "sha-A"

    def test_legacy_task_falls_back_to_claim(self):
        assert sched.extract_task_candidate_sha({"candidate_sha": "sha-X"}) == "sha-X"
        assert sched.extract_task_candidate_sha({"baseline_commit": "sha-Y"}) == "sha-Y"

    def test_launch_baseline_is_not_completion_evidence(self):
        """P1 回归:scheduler 管理的任务不得用 baseline 冒充完成证据。"""
        task = {"candidate_sha": "sha-A", "baseline_commit": "sha-A"}
        assert sched.is_scheduler_managed_task(task) is True
        assert sched.extract_task_verified_sha(task) == ""
        ok, _claim, evidence = sched.task_claim_evidence_consistent(task)
        assert ok is False
        assert evidence == ""

    def test_legacy_task_without_claim_keeps_baseline_fallback(self):
        task = {"baseline_commit": "sha-Y"}
        assert sched.is_scheduler_managed_task(task) is False
        assert sched.extract_task_verified_sha(task) == "sha-Y"

    def test_revision_matches_uses_evidence(self):
        same = {"candidate_sha": "sha-A", "verified_candidate_sha": "sha-A"}
        assert sched.candidate_revision_matches(same, "sha-A")
        assert not sched.candidate_revision_matches(same, "sha-B")
        assert not sched.candidate_revision_matches(same, "")
        assert not sched.candidate_revision_matches({}, "sha-A")
        # 声明 A 但完成时验证 B:不得算作验证了 A
        assert not sched.candidate_revision_matches(
            {"candidate_sha": "sha-A", "verified_candidate_sha": "sha-B"}, "sha-A")
        # 声明 A 但无完成证据:同样不得算作验证了 A
        assert not sched.candidate_revision_matches(
            {"candidate_sha": "sha-A", "baseline_commit": "sha-A"}, "sha-A")

    def test_completion_evidence_wins_over_launch_baseline(self):
        """P1 回归:launch 时 HEAD=A,验收完成时 HEAD=B -> 以 B 为准。

        baseline_commit 是启动证据,不是完成证据。Agent 执行期间
        git pull/checkout 后,只有 verified_candidate_sha 能证明
        「完成验证时到底验证了谁」。
        """
        task = {
            "candidate_sha": "sha-A",
            "baseline_commit": "sha-A",
            "verified_candidate_sha": "sha-B",
        }
        assert sched.extract_task_verified_sha(task) == "sha-B"
        assert sched.extract_task_candidate_sha(task) == "sha-B"
        ok, claim, evidence = sched.task_claim_evidence_consistent(task)
        assert ok is False
        assert (claim, evidence) == ("sha-A", "sha-B")

    def test_join_gate_refuses_launch_only_evidence(self):
        """A claim + A launch baseline 但完成时 HEAD=B:门禁必须拒绝。"""
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A"),
        ]
        for t in tasks:
            t["baseline_commit"] = "sha-A"
        tasks[0]["verified_candidate_sha"] = "sha-B"

        passed, reason, details = sched.evaluate_join_gate(
            _node("wrapup", ["test", "review"]), tasks, WF, "sha-A"
        )
        assert passed is False
        assert reason in (
            sched.JOIN_EVIDENCE_MISMATCH, sched.JOIN_CANDIDATE_MISMATCH,
        )
        assert details["branches"]["test"]["claim_evidence_mismatch"]

    def test_completion_evidence_matching_passes(self):
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A"),
        ]
        for t in tasks:
            t["baseline_commit"] = "sha-A"
            t["verified_candidate_sha"] = "sha-A"

        passed, reason, _ = sched.evaluate_join_gate(
            _node("wrapup", ["test", "review"]), tasks, WF, "sha-A"
        )
        assert passed is True and reason == sched.JOIN_SATISFIED

    def test_abbreviated_sha_is_not_a_mismatch(self):
        """P2 回归:delivery 记 abc1234,clone 记完整 SHA,同一 commit 不算冲突。"""
        full = "abc1234f9287a1b2c3d4e5f60718293a4b5c6d7e"
        task = {
            "candidate_sha": "abc1234",
            "baseline_commit": full,
            "verified_candidate_sha": full,
        }
        ok, claim, evidence = sched.task_claim_evidence_consistent(task)
        assert ok is True
        assert claim != evidence  # 字符串不同,但规范化后是同一 commit

    def test_revision_matches_accepts_abbreviated(self):
        full = "abc1234f9287a1b2c3d4e5f60718293a4b5c6d7e"
        task = {"candidate_sha": "abc1234", "verified_candidate_sha": full}
        assert sched.candidate_revision_matches(task, "abc1234")
        assert not sched.candidate_revision_matches(task, "def5678")

    def test_frozen_for_nodes_requires_all_bound(self):
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", status="working", verdict=""),
        ]
        ok, details = sched.candidate_frozen_for_nodes(
            tasks, WF, ["test", "review"], "sha-A"
        )
        assert ok is False
        assert details["nodes"]["review"]["ok"] is False

    def test_parallel_metrics_overlap_evidence(self):
        tasks = [
            _task("t-test", WF, "test", sha="sha-A"),
            _task("t-review", WF, "review", sha="sha-A"),
        ]
        tasks[0]["started_at"] = 1000.0
        tasks[0]["updated_at"] = 1200.0
        tasks[1]["started_at"] = 1100.0
        tasks[1]["updated_at"] = 1300.0
        metrics = sched.parallel_section_metrics(tasks, WF, ["test", "review"])
        assert metrics["parallel_evidence"] is True
        assert metrics["overlap"]["max_overlap_seconds"] == pytest.approx(100.0)
