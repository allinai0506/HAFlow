"""Selective Replan v1 纯决策核心测试(HAFlow PR #110)。

覆盖:策略解析、affected_task_ids 结构化解析、谱系头、目标校验
(fail-closed,任一非法整体拒绝)、episode 身份(刻意不含 targets)、
plan 构建、门禁清单注入、replacement blocker 上下文、指标、
fix_loop target-aware latch/redelivery/fingerprint、direct_dispatch
契约注入、scheduler_facts 事实持久化(exactly-once / mismatch)、
以及 CLI --affected-task-id 的接纳与拒绝。

纯函数部分零 I/O;facts 部分使用临时 SQLite;CLI 部分走真实子进程。
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERDR_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERDR_ROOT))

from herdr import direct_dispatch  # noqa: E402
from herdr import fix_loop  # noqa: E402
from herdr import scheduler_facts  # noqa: E402
from herdr import selective_replan as srp  # noqa: E402

WF = "wf-srp"
SHA_A = "a" * 40
SHA_B = "b" * 40

SUPERSEDEABLE = {
    "dispatched", "working", "blocked", "agent_done",
    "rework", "cleaned", "failed",
}

POLICY = {
    "version": srp.POLICY_VERSION,
    "mode": srp.MODE_EXPLICIT_TASK_TARGETS,
    "retry_node": "implementation",
}


def _impl(task_id, status="completed", workflow_id=WF, **overrides):
    task = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "node": "implementation",
        "stage": "implementation",
        "status": status,
        "goal": f"goal of {task_id}",
        "created_at": 1000.0,
        "updated_at": 2000.0,
    }
    task.update(overrides)
    return task


def _gate(task_id="wf-srp-review-1", version=7, **overrides):
    task = {
        "task_id": task_id,
        "workflow_id": WF,
        "node": "review",
        "stage": "review",
        "status": "completed",
        "stage_verdict": "blocked",
        "stage_verdict_note": "前端错误提示缺失",
        "version": version,
        "candidate_sha": SHA_A,
        "verified_candidate_sha": SHA_A,
    }
    task.update(overrides)
    return task


def _plan(**overrides):
    kwargs = {
        "workflow_id": WF,
        "gate_node": "review",
        "gate_tasks": [_gate()],
        "policy": POLICY,
        "requested_raw": ["wf-srp-impl-B"],
        "tasks": [
            _impl("wf-srp-impl-A"),
            _impl("wf-srp-impl-B"),
            _impl("wf-srp-impl-C"),
        ],
        "frozen_candidate_sha": SHA_A,
        "supersedeable_statuses": SUPERSEDEABLE,
        "now": 1700000000.0,
    }
    kwargs.update(overrides)
    return srp.build_selective_replan_plan(**kwargs)


class PolicyParsingTest(unittest.TestCase):
    def test_absent_policy_returns_none(self):
        self.assertIsNone(srp.policy_from_workflow({}))
        self.assertIsNone(srp.policy_from_workflow(None))
        self.assertIsNone(srp.policy_from_workflow({"selective_replan": "x"}))

    def test_wrong_version_or_mode_returns_none(self):
        bad_version = dict(POLICY, version="selective-replan-v0")
        bad_mode = dict(POLICY, mode="guessed_targets")
        self.assertIsNone(
            srp.policy_from_workflow({"selective_replan": bad_version}))
        self.assertIsNone(
            srp.policy_from_workflow({"selective_replan": bad_mode}))

    def test_missing_retry_node_returns_none(self):
        bad = dict(POLICY)
        del bad["retry_node"]
        self.assertIsNone(srp.policy_from_workflow({"selective_replan": bad}))

    def test_valid_policy(self):
        policy = srp.policy_from_workflow({"selective_replan": POLICY})
        self.assertEqual(policy["version"], srp.POLICY_VERSION)
        self.assertEqual(policy["mode"], srp.MODE_EXPLICIT_TASK_TARGETS)
        self.assertEqual(policy["retry_node"], "implementation")

    def test_policy_identity_deterministic_and_sensitive(self):
        first = srp.policy_identity(POLICY)
        self.assertEqual(first, srp.policy_identity(dict(POLICY)))
        other = srp.policy_identity(dict(POLICY, retry_node="plan"))
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith("srp-"))


class ParseAffectedTaskIdsTest(unittest.TestCase):
    def test_missing_is_none(self):
        self.assertIsNone(srp.parse_affected_task_ids(None))

    def test_empty_list_is_explicit_unattributable(self):
        self.assertEqual(srp.parse_affected_task_ids([]), [])

    def test_non_list_is_malformed(self):
        self.assertIsNone(srp.parse_affected_task_ids("wf-impl-B"))
        self.assertIsNone(srp.parse_affected_task_ids({"id": "x"}))

    def test_dedupes_and_strips(self):
        self.assertEqual(
            srp.parse_affected_task_ids([" a ", "b", "a"]), ["a", "b"])

    def test_non_string_member_is_malformed(self):
        self.assertIsNone(srp.parse_affected_task_ids(["a", 1]))
        self.assertIsNone(srp.parse_affected_task_ids(["a", "  "]))


class LineageHeadTest(unittest.TestCase):
    def test_picks_latest_active_member(self):
        tasks = [
            _impl("wf-srp-impl-B", status="superseded",
                  superseded_by="wf-srp-impl-B-r2"),
            _impl("wf-srp-impl-B-r2", status="working"),
        ]
        head = srp.current_lineage_head(tasks, WF, "implementation",
                                        "wf-srp-impl-B")
        self.assertEqual(head["task_id"], "wf-srp-impl-B-r2")

    def test_none_when_entire_lineage_superseded(self):
        tasks = [
            _impl("wf-srp-impl-B", status="superseded"),
            _impl("wf-srp-impl-B-r2", status="superseded"),
        ]
        self.assertIsNone(
            srp.current_lineage_head(tasks, WF, "implementation",
                                     "wf-srp-impl-B"))


class ValidateTargetsTest(unittest.TestCase):
    def test_happy_path_targets_and_preserved(self):
        tasks = [_impl("A"), _impl("B"), _impl("C")]
        targets, preserved, reason = srp.validate_replan_targets(
            tasks, workflow_id=WF, retry_node="implementation",
            requested_ids=["B"], supersedeable_statuses=SUPERSEDEABLE)
        self.assertEqual(targets, ["B"])
        self.assertEqual(preserved, ["A", "C"])
        self.assertEqual(reason, srp.REASON_OK)

    def test_one_invalid_id_rejects_whole_plan(self):
        """Case 8:两个合法 + 一个不存在 → 整体拒绝,绝不部分接受。"""
        tasks = [_impl("A"), _impl("B"), _impl("C")]
        targets, preserved, reason = srp.validate_replan_targets(
            tasks, workflow_id=WF, retry_node="implementation",
            requested_ids=["A", "B", "ghost"],
            supersedeable_statuses=SUPERSEDEABLE)
        self.assertIsNone(targets)
        self.assertIsNone(preserved)
        self.assertTrue(reason.startswith(srp.REASON_TARGET_UNKNOWN))

    def test_foreign_workflow_rejected(self):
        tasks = [_impl("B", workflow_id="wf-other")]
        targets, _, reason = srp.validate_replan_targets(
            tasks, workflow_id=WF, retry_node="implementation",
            requested_ids=["B"], supersedeable_statuses=SUPERSEDEABLE)
        self.assertIsNone(targets)
        self.assertTrue(reason.startswith(srp.REASON_TARGET_FOREIGN_WORKFLOW))

    def test_outside_retry_node_rejected(self):
        gate_task = _gate()
        targets, _, reason = srp.validate_replan_targets(
            [gate_task], workflow_id=WF, retry_node="implementation",
            requested_ids=[gate_task["task_id"]],
            supersedeable_statuses=SUPERSEDEABLE)
        self.assertIsNone(targets)
        self.assertTrue(reason.startswith(srp.REASON_TARGET_WRONG_NODE))

    def test_stale_lineage_id_rejected(self):
        """点名已被替代的 B(现役为 B-r2)→ 拒绝,不自动映射到最新谱系。"""
        tasks = [
            _impl("B", status="superseded", superseded_by="B-r2"),
            _impl("B-r2", status="working"),
        ]
        targets, _, reason = srp.validate_replan_targets(
            tasks, workflow_id=WF, retry_node="implementation",
            requested_ids=["B"], supersedeable_statuses=SUPERSEDEABLE)
        self.assertIsNone(targets)
        self.assertTrue(
            reason.startswith(srp.REASON_TARGET_NOT_CURRENT_LINEAGE))

    def test_pending_status_rejected(self):
        tasks = [_impl("B", status="pending")]
        targets, _, reason = srp.validate_replan_targets(
            tasks, workflow_id=WF, retry_node="implementation",
            requested_ids=["B"], supersedeable_statuses=SUPERSEDEABLE)
        self.assertIsNone(targets)
        self.assertTrue(
            reason.startswith(srp.REASON_TARGET_STATUS_NOT_REPLACEABLE))

    def test_completed_target_accepted_via_finalizable(self):
        tasks = [_impl("B", status="completed")]
        targets, _, reason = srp.validate_replan_targets(
            tasks, workflow_id=WF, retry_node="implementation",
            requested_ids=["B"], supersedeable_statuses=SUPERSEDEABLE)
        self.assertEqual(targets, ["B"])
        self.assertEqual(reason, srp.REASON_OK)


class IdentityTest(unittest.TestCase):
    def test_deterministic(self):
        first = srp.replan_identity(WF, "gate-1", 7, SHA_A,
                                    "implementation", "srp-x")
        second = srp.replan_identity(WF, "gate-1", 7, SHA_A,
                                     "implementation", "srp-x")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("srd-"))

    def test_identity_binds_episode_not_targets(self):
        """同一 episode 给出不同 targets:身份相同,内容由 compare_fields 兜住。"""
        base = _plan()
        other = _plan(requested_raw=["wf-srp-impl-C"])
        self.assertEqual(base["mode"], srp.MODE_SELECTIVE)
        self.assertEqual(other["mode"], srp.MODE_SELECTIVE)
        self.assertEqual(base["replan_id"], other["replan_id"])
        self.assertNotEqual(base["target_task_ids"], other["target_task_ids"])

    def test_identity_changes_with_gate_version(self):
        one = _plan()
        two = _plan(gate_tasks=[_gate(version=8)])
        self.assertNotEqual(one["replan_id"], two["replan_id"])


class BuildPlanTest(unittest.TestCase):
    def test_policy_disabled(self):
        plan = _plan(policy=None)
        self.assertEqual(plan["mode"], srp.MODE_LEGACY_FALLBACK)
        self.assertEqual(plan["reason"], srp.REASON_POLICY_DISABLED)

    def test_no_blocked_gate_task(self):
        plan = _plan(gate_tasks=[_gate(stage_verdict="pass")])
        self.assertEqual(plan["reason"], srp.REASON_NO_GATE_TASK)

    def test_multiple_blocked_gate_tasks(self):
        plan = _plan(gate_tasks=[_gate(), _gate(task_id="wf-srp-review-2")])
        self.assertEqual(plan["reason"], srp.REASON_MULTIPLE_GATE_TASKS)

    def test_gate_version_unproven(self):
        plan = _plan(gate_tasks=[_gate(version=None)])
        self.assertEqual(plan["reason"], srp.REASON_GATE_VERSION_UNPROVEN)
        self.assertEqual(plan["replan_id"], "")

    def test_gate_identity_unproven_when_verified_missing(self):
        plan = _plan(gate_tasks=[_gate(verified_candidate_sha="")])
        self.assertEqual(plan["reason"], srp.REASON_GATE_IDENTITY_UNPROVEN)

    def test_gate_identity_unproven_when_frozen_mismatch(self):
        plan = _plan(frozen_candidate_sha=SHA_B)
        self.assertEqual(plan["reason"], srp.REASON_GATE_IDENTITY_UNPROVEN)

    def test_targets_missing_is_fallback_but_persistable(self):
        plan = _plan(requested_raw=None)
        self.assertEqual(plan["mode"], srp.MODE_LEGACY_FALLBACK)
        self.assertEqual(plan["reason"], srp.REASON_TARGETS_MISSING)
        # 门禁身份已证明:fallback 事实仍可持久化(审计轨迹)。
        self.assertTrue(plan["replan_id"])

    def test_targets_empty_is_explicit_unattributable(self):
        plan = _plan(requested_raw=[])
        self.assertEqual(plan["reason"], srp.REASON_TARGETS_EMPTY)
        self.assertEqual(plan["mode"], srp.MODE_LEGACY_FALLBACK)

    def test_targets_malformed(self):
        plan = _plan(requested_raw="wf-srp-impl-B")
        self.assertEqual(plan["reason"], srp.REASON_TARGETS_MALFORMED)

    def test_selective_happy_path(self):
        plan = _plan()
        self.assertEqual(plan["mode"], srp.MODE_SELECTIVE)
        self.assertEqual(plan["reason"], srp.REASON_OK)
        self.assertEqual(plan["target_task_ids"], ["wf-srp-impl-B"])
        self.assertEqual(plan["target_lineage_roots"], ["wf-srp-impl-B"])
        self.assertEqual(plan["preserved_task_ids"],
                         ["wf-srp-impl-A", "wf-srp-impl-C"])
        self.assertTrue(plan["replan_id"].startswith("srd-"))
        self.assertEqual(plan["gate_task_id"], "wf-srp-review-1")
        self.assertEqual(plan["gate_task_version"], 7)
        self.assertEqual(plan["gate_candidate_sha"], SHA_A)
        self.assertEqual(plan["policy_identity"], srp.policy_identity(POLICY))

    def test_fallback_skeleton_is_complete(self):
        plan = _plan(requested_raw=None)
        for key in ("replan_id", "policy_identity", "gate_node",
                    "gate_task_id", "gate_task_version", "gate_candidate_sha",
                    "retry_node", "mode", "requested_task_ids",
                    "target_task_ids", "target_lineage_roots",
                    "preserved_task_ids", "reason", "created_at"):
            self.assertIn(key, plan)


class InventoryTest(unittest.TestCase):
    def test_only_current_lineage_heads(self):
        tasks = [
            _impl("A"),
            _impl("B", status="superseded", superseded_by="B-r2"),
            _impl("B-r2", status="working", goal="frontend page"),
            _impl("C"),
        ]
        inventory = srp.build_task_inventory(
            tasks, workflow_id=WF, retry_node="implementation")
        ids = [item["task_id"] for item in inventory]
        self.assertEqual(ids, ["A", "B-r2", "C"])

    def test_duplicate_active_members_resolve_to_head(self):
        """同谱系多个存活成员(替代尚未登记 superseded_by)时取序号最大者。"""
        tasks = [
            _impl("B", status="working", goal="old"),
            _impl("B-r2", status="working", goal="new"),
        ]
        inventory = srp.build_task_inventory(
            tasks, workflow_id=WF, retry_node="implementation")
        self.assertEqual([item["task_id"] for item in inventory], ["B-r2"])
        self.assertEqual(inventory[0]["goal"], "new")

    def test_excludes_other_nodes_and_workflows(self):
        tasks = [
            _impl("A"),
            _impl("X", workflow_id="wf-other"),
            _gate(),
        ]
        inventory = srp.build_task_inventory(
            tasks, workflow_id=WF, retry_node="implementation")
        self.assertEqual([item["task_id"] for item in inventory], ["A"])

    def test_render_block_mentions_ids_and_attribution_rules(self):
        block = srp.render_inventory_block(
            [{"task_id": "A", "goal": "backend"}, {"task_id": "B", "goal": ""}])
        self.assertIn("A", block)
        self.assertIn("B", block)
        self.assertIn("affected_task_ids", block)
        self.assertIn("无法准确归因", block)


class ReplacementBlockerNoteTest(unittest.TestCase):
    def test_contents(self):
        plan = _plan()
        note = srp.render_replacement_blocker_note(plan, "wf-srp-impl-B")
        self.assertIn("wf-srp-impl-B", note)
        self.assertIn("前端错误提示缺失", note)
        self.assertIn("wf-srp-impl-A", note)
        self.assertIn("wf-srp-impl-C", note)
        self.assertIn("禁止扩大修改范围", note)


class MetricsTest(unittest.TestCase):
    def test_counts_and_ratio(self):
        metrics = srp.replan_metrics(_plan())
        self.assertEqual(metrics["targeted_task_count"], 1)
        self.assertEqual(metrics["preserved_task_count"], 2)
        self.assertEqual(metrics["implementation_task_count"], 3)
        self.assertAlmostEqual(metrics["replan_ratio"], 1 / 3)
        self.assertEqual(metrics["mode"], srp.MODE_SELECTIVE)


class FixLoopTargetAwareTest(unittest.TestCase):
    def test_fingerprint_legacy_byte_identical_without_ids(self):
        blockers = [{"task_id": "g1", "note": "n"}]
        legacy = fix_loop.verdict_fingerprint("br", blockers)
        self.assertEqual(legacy, fix_loop.verdict_fingerprint(
            "br", blockers, affected_task_ids=None))
        self.assertEqual(legacy, fix_loop.verdict_fingerprint(
            "br", blockers, affected_task_ids=[]))

    def test_fingerprint_changes_with_ids_and_is_order_independent(self):
        blockers = [{"task_id": "g1", "note": "n"}]
        without = fix_loop.verdict_fingerprint("br", blockers)
        with_ids = fix_loop.verdict_fingerprint(
            "br", blockers, affected_task_ids=["A", "B"])
        reordered = fix_loop.verdict_fingerprint(
            "br", blockers, affected_task_ids=["B", "A"])
        self.assertNotEqual(without, with_ids)
        self.assertEqual(with_ids, reordered)

    def _tasks(self):
        return [
            _impl("A", status="completed", updated_at=3000.0),
            _impl("B", status="superseded", updated_at=1000.0),
            _impl("B-r2", status="working", updated_at=2500.0),
            _impl("C", status="completed", updated_at=3000.0),
        ]

    def test_selective_latch_requires_every_target_root(self):
        tasks = self._tasks()
        # B 谱系尚无 latch 之后的完成 → 阻断。
        self.assertTrue(fix_loop.latch_blocks_advance(
            tasks, "implementation", 2000.0, target_lineage_roots=["B"]))
        # B-r2 在 latch 之后完成 → 放行。
        done = [dict(t, status="completed", updated_at=3000.0)
                if t["task_id"] == "B-r2" else t for t in tasks]
        self.assertFalse(fix_loop.latch_blocks_advance(
            done, "implementation", 2000.0, target_lineage_roots=["B"]))

    def test_selective_latch_ignores_preserved_updates(self):
        """被保留任务(A/C)在 latch 之后落定,绝不能解除 selective latch。"""
        tasks = [
            _impl("A", status="completed", updated_at=9999.0),
            _impl("B", status="superseded", updated_at=1000.0),
            _impl("C", status="completed", updated_at=9999.0),
        ]
        self.assertTrue(fix_loop.latch_blocks_advance(
            tasks, "implementation", 2000.0, target_lineage_roots=["B"]))

    def test_selective_latch_multi_target_and_semantics(self):
        tasks = [
            _impl("B", status="superseded"),
            _impl("B-r2", status="completed", updated_at=3000.0),
            _impl("C", status="superseded"),
            _impl("C-r2", status="working", updated_at=3500.0),
        ]
        # C 谱系只有 working(非 completed-like)→ 仍阻断(AND 语义)。
        self.assertTrue(fix_loop.latch_blocks_advance(
            tasks, "implementation", 2000.0,
            target_lineage_roots=["B", "C"]))
        done = [dict(t, status="completed") if t["task_id"] == "C-r2" else t
                for t in tasks]
        self.assertFalse(fix_loop.latch_blocks_advance(
            done, "implementation", 2000.0,
            target_lineage_roots=["B", "C"]))

    def test_latch_legacy_when_roots_empty(self):
        tasks = [_impl("A", status="completed", updated_at=3000.0)]
        self.assertFalse(fix_loop.latch_blocks_advance(
            tasks, "implementation", 2000.0, target_lineage_roots=[]))
        self.assertFalse(
            fix_loop.latch_blocks_advance(tasks, "implementation", 2000.0))

    def test_selective_redelivery_requires_replacement_per_root(self):
        tasks = [
            _impl("B", status="superseded"),
            _impl("B-r2", status="working", created_at=3000.0),
        ]
        self.assertTrue(fix_loop.redelivery_handled(
            tasks, "implementation", 2000.0, target_lineage_roots=["B"]))
        self.assertFalse(fix_loop.redelivery_handled(
            tasks, "implementation", 2000.0,
            target_lineage_roots=["B", "C"]))

    def test_redelivery_legacy_when_roots_empty(self):
        tasks = [_impl("A", status="working", updated_at=3000.0)]
        self.assertTrue(fix_loop.redelivery_handled(
            tasks, "implementation", 2000.0, target_lineage_roots=[]))

    def test_summary_carries_mode_and_roots_only_when_selective(self):
        item = {
            "workflow_id": WF, "gate_stage": "review",
            "retry_node": "implementation", "blockers": [],
            "mode": "selective", "target_lineage_roots": ["B"],
        }
        summary = fix_loop.summarize_fix_loop_item(item)
        self.assertEqual(summary["mode"], "selective")
        self.assertEqual(summary["target_lineage_roots"], ["B"])
        legacy = fix_loop.summarize_fix_loop_item(
            {"workflow_id": WF, "gate_stage": "review",
             "retry_node": "implementation", "blockers": []})
        self.assertNotIn("mode", legacy)
        self.assertNotIn("target_lineage_roots", legacy)


class DirectDispatchContractTest(unittest.TestCase):
    def test_gate_contract_byte_identical_without_inventory(self):
        legacy = direct_dispatch.gate_verdict_contract("g1")
        self.assertEqual(legacy,
                         direct_dispatch.gate_verdict_contract("g1", None))
        self.assertNotIn("affected_task_ids", legacy)

    def test_gate_contract_with_inventory_appends_clause(self):
        block = srp.render_inventory_block([{"task_id": "A", "goal": "g"}])
        text = direct_dispatch.gate_verdict_contract("g1", block)
        self.assertIn("affected_task_ids", text)
        self.assertIn("task_id: A", text)
        self.assertTrue(
            text.startswith(direct_dispatch.gate_verdict_contract("g1")))

    def _node(self):
        return {
            "id": "implementation",
            "label": "实现",
            "purpose": "实现需求",
            "required_outputs": ["代码"],
            "rules": [],
            "default_task_type": "feat",
            "default_integration_mode": "git",
        }

    def test_redispatch_prompt_carries_blocker_note(self):
        tasks = [
            _impl("B", status="superseded", goal="frontend",
                  acceptance_criteria=["AC-1"], integration_mode="git",
                  stage_verdict_note="old note"),
        ]
        plan = direct_dispatch.plan_stage_dispatch(
            WF, self._node(), tasks, "需求",
            redispatch_blocker_notes={"B": "BLOCKER-CONTEXT-X"},
        )
        self.assertEqual(plan["mode"], "dispatch")
        self.assertEqual(plan["reason"], "redispatch superseded subset")
        spec = plan["specs"][0]
        self.assertEqual(spec["task_id"], "B-r2")
        # 继承原任务的 goal/acceptance/integration_mode。
        self.assertEqual(spec["goal"], "frontend")
        self.assertIn("AC-1", spec["acceptance"])
        self.assertEqual(spec["integration_mode"], "git")
        self.assertIn("BLOCKER-CONTEXT-X", spec["prompt"])

    def test_redispatch_prompt_without_notes_unchanged(self):
        tasks = [_impl("B", status="superseded", goal="frontend")]
        with_notes = direct_dispatch.plan_stage_dispatch(
            WF, self._node(), tasks, "需求",
            redispatch_blocker_notes={"B": "X"})["specs"][0]["prompt"]
        without = direct_dispatch.plan_stage_dispatch(
            WF, self._node(), tasks, "需求")["specs"][0]["prompt"]
        self.assertNotEqual(with_notes, without)
        self.assertNotIn("None", without)

    def test_gate_dispatch_prompt_contains_inventory(self):
        gate_node = {
            "id": "review", "label": "评审", "purpose": "评审实现",
            "required_outputs": ["结论"], "rules": [],
            "default_task_type": "review", "default_integration_mode": "none",
        }
        block = srp.render_inventory_block([{"task_id": "A", "goal": "g"}])
        plan = direct_dispatch.plan_stage_dispatch(
            WF, gate_node, [], "需求",
            gate_contract=True, gate_inventory_block=block)
        self.assertEqual(plan["mode"], "dispatch")
        self.assertIn("task_id: A", plan["specs"][0]["prompt"])
        legacy = direct_dispatch.plan_stage_dispatch(
            WF, gate_node, [], "需求", gate_contract=True)
        self.assertNotIn("task_id: A", legacy["specs"][0]["prompt"])


class SchedulerFactsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-srp-facts-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"

    def test_record_find_roundtrip_and_idempotency(self):
        plan = _plan()
        first = scheduler_facts.record_selective_replan_decision(
            WF, plan, db_path=self.db)
        self.assertEqual(first["status"], "created")
        second = scheduler_facts.record_selective_replan_decision(
            WF, plan, db_path=self.db)
        self.assertEqual(second["status"], "exists")
        found = scheduler_facts.find_selective_replan_decision(
            WF, plan["replan_id"], db_path=self.db)
        self.assertIsNotNone(found)
        self.assertEqual(found["target_task_ids"], ["wf-srp-impl-B"])
        self.assertEqual(found["preserved_task_ids"],
                         ["wf-srp-impl-A", "wf-srp-impl-C"])

    def test_same_episode_changed_targets_is_mismatch(self):
        """同一 episode 改 targets:不是第二条事实,而是 identity_content_mismatch。"""
        plan = _plan()
        scheduler_facts.record_selective_replan_decision(
            WF, plan, db_path=self.db)
        edited = _plan(requested_raw=["wf-srp-impl-C"])
        self.assertEqual(edited["replan_id"], plan["replan_id"])
        result = scheduler_facts.record_selective_replan_decision(
            WF, edited, db_path=self.db)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "identity_content_mismatch")
        # 原事实保持不变(immutable)。
        found = scheduler_facts.find_selective_replan_decision(
            WF, plan["replan_id"], db_path=self.db)
        self.assertEqual(found["target_task_ids"], ["wf-srp-impl-B"])

    def test_rejects_unproven_gate_candidate(self):
        plan = _plan()
        plan["gate_candidate_sha"] = ""
        result = scheduler_facts.record_selective_replan_decision(
            WF, plan, db_path=self.db)
        self.assertEqual(result["status"], "rejected")

    def test_rejects_claimed_identity_mismatch(self):
        plan = _plan()
        plan["replan_id"] = "srd-" + "0" * 32
        result = scheduler_facts.record_selective_replan_decision(
            WF, plan, db_path=self.db)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "replan_identity_mismatch")

    def test_latest_for_node_skips_fallback_facts(self):
        """同一 episode 只允许一条事实:fallback 与 selective 用不同 episode。

        fallback 更**新**(version 8)时仍不得掩盖更早的 selective 目标——
        legacy fallback 不作废 retry_node,被点名的谱系依然待补派。
        """
        selective = _plan(gate_tasks=[_gate(version=7)])
        scheduler_facts.record_selective_replan_decision(
            WF, selective, db_path=self.db)
        fallback = _plan(gate_tasks=[_gate(version=8)], requested_raw=None)
        self.assertEqual(fallback["mode"], srp.MODE_LEGACY_FALLBACK)
        self.assertEqual(
            scheduler_facts.record_selective_replan_decision(
                WF, fallback, db_path=self.db)["status"], "created")

        latest = scheduler_facts.latest_selective_replan_for_node(
            WF, "implementation", db_path=self.db)
        self.assertEqual(latest["replan_id"], selective["replan_id"])
        facts = scheduler_facts.list_selective_replan_decisions(
            WF, db_path=self.db)
        self.assertEqual(len(facts), 2)

    def test_find_with_empty_identity_returns_none(self):
        self.assertIsNone(
            scheduler_facts.find_selective_replan_decision(WF, "",
                                                           db_path=self.db))


class CliAffectedTaskIdTest(unittest.TestCase):
    """CLI --affected-task-id 的接纳与拒绝(真实子进程,临时 TASKS_FILE)。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-srp-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tasks_file = self.root / "tasks.json"
        self.tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
        (self.root / "workflows.json").write_text(
            json.dumps({"workflows": []}), encoding="utf-8")
        (self.root / "stage-state.json").write_text("{}", encoding="utf-8")

    def _env(self):
        env = os.environ.copy()
        env["TASKS_FILE"] = str(self.tasks_file)
        env["WORKFLOWS_FILE"] = str(self.root / "workflows.json")
        env["STAGE_STATE_FILE"] = str(self.root / "stage-state.json")
        env["HERDR_STATE_DB"] = str(self.root / "state.db")
        return env

    def _run(self, args):
        return subprocess.run(
            ["python3", str(HERDR_ROOT / "bin" / "herdr-task")] + args,
            env=self._env(), text=True, capture_output=True)

    def _seed(self, task_id="g1", status="agent_done"):
        data = json.loads(self.tasks_file.read_text(encoding="utf-8"))
        data["tasks"].append({
            "task_id": task_id, "workflow_id": WF, "node": "review",
            "stage": "review", "status": status,
            "status_history": [],
        })
        self.tasks_file.write_text(json.dumps(data), encoding="utf-8")

    def _task(self, task_id="g1"):
        data = json.loads(self.tasks_file.read_text(encoding="utf-8"))
        return next(t for t in data["tasks"] if t["task_id"] == task_id)

    def test_blocked_verdict_persists_affected_task_ids(self):
        self._seed()
        result = self._run([
            "set", "g1", "completed", "--verdict", "blocked",
            "--note", "前端错误提示缺失",
            "--affected-task-id", "wf-srp-impl-B",
            "--affected-task-id", "wf-srp-impl-B",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        task = self._task()
        self.assertEqual(task["stage_verdict"], "blocked")
        self.assertEqual(
            task["stage_verdict_affected_task_ids"], ["wf-srp-impl-B"])

    def test_pass_verdict_with_ids_rejected(self):
        self._seed()
        result = self._run([
            "set", "g1", "completed", "--verdict", "pass",
            "--affected-task-id", "wf-srp-impl-B",
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("blocked", result.stderr)

    def test_ids_require_completed_status(self):
        self._seed()
        result = self._run([
            "set", "g1", "working",
            "--affected-task-id", "wf-srp-impl-B",
        ])
        self.assertEqual(result.returncode, 2)

    def test_blocked_without_ids_writes_explicit_empty_list(self):
        """无法归因是结论属性,必须显式覆盖写,不能靠字段缺失去表达。"""
        self._seed()
        result = self._run([
            "set", "g1", "completed", "--verdict", "blocked",
            "--note", "整体返工",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._task()["stage_verdict_affected_task_ids"], [])

    def test_reverdict_without_ids_clears_previous_attribution(self):
        """同一门禁任务二次结论不写 id 时,上一轮归因不得残留。

        残留会让系统拿着旧归因做选择性返工:本轮 Verifier 明确表示
        「无法归因」,系统却仍去 supersede 上一轮点名的任务。
        """
        self._seed()
        first = self._run([
            "set", "g1", "completed", "--verdict", "blocked",
            "--note", "前端错误提示缺失",
            "--affected-task-id", "wf-srp-impl-B",
        ])
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            self._task()["stage_verdict_affected_task_ids"], ["wf-srp-impl-B"])

        second = self._run([
            "set", "g1", "completed", "--verdict", "blocked",
            "--note", "无法归因到具体实现任务",
        ])
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self._task()["stage_verdict_affected_task_ids"], [])

    def test_pass_verdict_clears_previous_attribution(self):
        """结论翻成 pass 后,旧归因同样必须消失。"""
        self._seed()
        self._run([
            "set", "g1", "completed", "--verdict", "blocked",
            "--note", "前端错误提示缺失",
            "--affected-task-id", "wf-srp-impl-B",
        ])
        result = self._run([
            "set", "g1", "completed", "--verdict", "pass", "--note", "已修复",
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._task()["stage_verdict_affected_task_ids"], [])


class MergeAffectedIdsTest(unittest.TestCase):
    def test_sorted_deterministic_union(self):
        self.assertEqual(
            srp.merge_affected_task_ids([["B", "A"], ["C", "B", " "]]),
            ["A", "B", "C"])
        self.assertEqual(srp.merge_affected_task_ids([]), [])
        self.assertEqual(srp.merge_affected_task_ids([None, ["B"]]), ["B"])


class InvalidationOutcomeTest(unittest.TestCase):
    def test_all_applied(self):
        out = srp.selective_invalidation_outcome(["B", "C"], ["B", "C"], {})
        self.assertTrue(out["all_targets_applied"])
        self.assertEqual(out["pending"], [])

    def test_partial_reports_pending_and_failed(self):
        out = srp.selective_invalidation_outcome(
            ["B", "C"], ["B"], {"C": "supersede:boom"})
        self.assertFalse(out["all_targets_applied"])
        self.assertEqual(out["pending"], ["C"])
        self.assertIn("C", out["failed"])

    def test_already_superseded_counts_as_applied(self):
        # 调用方把崩溃前已 superseded 的目标计入 applied。
        out = srp.selective_invalidation_outcome(["B", "C"], ["B"], {})
        self.assertEqual(out["pending"], ["C"])


class ReusableFactTest(unittest.TestCase):
    def _fact(self, **over):
        base = {
            "mode": srp.MODE_SELECTIVE, "retry_node": "implementation",
            "gate_task_id": "g1", "gate_task_version": 7,
            "gate_candidate_sha": SHA_A,
            "policy_identity": "srp-x",
            "target_task_ids": ["B"],
        }
        base.update(over)
        return base

    def test_happy_path_reuse(self):
        fact = self._fact()
        found = srp.find_reusable_selective_fact(
            [fact], retry_node="implementation", gate_task_id="g1",
            gate_task_version=7, frozen_candidate_sha=SHA_A,
            policy_identity="srp-x")
        self.assertEqual(found, fact)

    def test_version_mismatch_no_reuse(self):
        self.assertIsNone(srp.find_reusable_selective_fact(
            [self._fact()], retry_node="implementation", gate_task_id="g1",
            gate_task_version=8, frozen_candidate_sha=SHA_A,
            policy_identity="srp-x"))

    def test_candidate_rotation_no_reuse(self):
        self.assertIsNone(srp.find_reusable_selective_fact(
            [self._fact()], retry_node="implementation", gate_task_id="g1",
            gate_task_version=7, frozen_candidate_sha=SHA_B,
            policy_identity="srp-x"))

    def test_fallback_mode_never_reused(self):
        self.assertIsNone(srp.find_reusable_selective_fact(
            [self._fact(mode=srp.MODE_LEGACY_FALLBACK)],
            retry_node="implementation", gate_task_id="g1",
            gate_task_version=7, frozen_candidate_sha=SHA_A,
            policy_identity="srp-x"))


class ReplacementBaselineTest(unittest.TestCase):
    def _fact(self, **over):
        base = {
            "mode": srp.MODE_SELECTIVE,
            "target_task_ids": ["B"],
            "gate_candidate_sha": SHA_A,
        }
        base.update(over)
        return base

    def test_happy_path(self):
        base = srp.selective_replacement_baseline(
            self._fact(), SHA_A, "candidate-x")
        self.assertEqual(
            base, {"onto_branch": "candidate-x", "candidate_sha": SHA_A})

    def test_rotation_refused(self):
        self.assertIsNone(srp.selective_replacement_baseline(
            self._fact(), SHA_B, "candidate-x"))

    def test_missing_branch_refused(self):
        self.assertIsNone(srp.selective_replacement_baseline(
            self._fact(), SHA_A, ""))

    def test_legacy_fact_refused(self):
        self.assertIsNone(srp.selective_replacement_baseline(
            self._fact(mode=srp.MODE_LEGACY_FALLBACK), SHA_A, "candidate-x"))


if __name__ == "__main__":
    unittest.main()
