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


def _task(task_id, workflow_id, node, status="completed", verdict="pass", sha=""):
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
    def test_extract_prefers_candidate_sha(self):
        task = {"candidate_sha": "sha-X", "baseline_commit": "sha-Y"}
        assert sched.extract_task_candidate_sha(task) == "sha-X"

    def test_extract_falls_back_to_baseline_commit(self):
        assert sched.extract_task_candidate_sha({"baseline_commit": "sha-Y"}) == "sha-Y"

    def test_revision_matches(self):
        assert sched.candidate_revision_matches({"candidate_sha": "sha-A"}, "sha-A")
        assert not sched.candidate_revision_matches({"candidate_sha": "sha-A"}, "sha-B")
        assert not sched.candidate_revision_matches({"candidate_sha": "sha-A"}, "")
        assert not sched.candidate_revision_matches({}, "sha-A")

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
