"""Role identity consistency regression tests.

A workflow id may contain a role word (wf-challenger-fix). Role evidence must
come from explicit dispatch metadata first, and only then from the exact
``{workflow}-{node}-{role}`` task-id convention.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from herdr import node_dispatch, node_dispatch_store, recovery_store, scheduler
from herdr.direct_dispatch import plan_stage_dispatch
from herdr.state_store import get_state_store, reset_state_store
from services.controller.stage_state import is_node_complete as stage_is_node_complete

WF = "wf-challenger-fix"

PLAN_NODE = {
    "id": "plan",
    "label": "计划",
    "purpose": "拆解任务",
    "required_outputs": ["技术架构与解耦任务拆解", "方案对抗审查与可行性风险评估"],
    "agent_policy": {
        "max_concurrency": 2,
        "roles": [
            {"name": "executor", "label": "架构"},
            {"name": "challenger", "label": "对抗", "depends_on_role": "executor"},
        ],
    },
}

IMPLEMENTATION_NODE = {
    "id": "implementation",
    "label": "实现",
    "purpose": "实现方案",
    "depends_on": ["plan"],
    "default_task_type": "feat",
    "default_integration_mode": "git",
    "agent_policy": {"max_concurrency": 3, "parallel": True},
    "max_tasks_per_node": 12,
}

PLAN_BODY = """## 技术方案与架构设计
核心模块解耦设计说明。
## 任务拆分与依赖关系
Task 1: 核心接口实现
## 修改文件清单
文件条目: core.py
## 每个 Task 目标与验收标准
验证命令: pytest -q test_core.py
## 风险与应对策略
无不可控风险。"""

REVIEW_BODY = """## 方案对抗性审查与可行性评估
Plan-Ref: n-plan-1
[PASS] 审查通过。
HERDR_GATE_VERDICT: pass
未发现阻塞性缺陷。本方案文件条目单一、验证命令可执行，
未发现循环依赖、过度设计或死锁风险。风险与验证方案明确，
验收命令可在本地复现，同意实施。"""


def make_task(task_id, role=None, status="completed", node="plan", **extra):
    task = {
        "task_id": task_id,
        "workflow_id": WF,
        "node": node,
        "stage": node,
        "execution_id": "exec-1",
        "run_id": f"run-{task_id}",
        "status": status,
    }
    if role is not None:
        task["dispatch_role"] = role
    task.update(extra)
    return task


def plan_note(task_id=f"{WF}-plan-executor", note_id="n-plan-1", ts=100.0):
    return {
        "note_id": note_id,
        "node": "plan",
        "kind": "plan",
        "title": "技术方案",
        "body": PLAN_BODY,
        "task_id": task_id,
        "ts": ts,
    }


def review_note(task_id=f"{WF}-plan-challenger", note_id="n-chal-1", ts=200.0, ref="n-plan-1"):
    body = REVIEW_BODY.replace("Plan-Ref: n-plan-1", f"Plan-Ref: {ref}")
    return {
        "note_id": note_id,
        "node": "plan",
        "kind": "review",
        "title": "对抗审查",
        "body": body,
        "task_id": task_id,
        "ts": ts,
    }


class SchedulerRoleIdentityTest(unittest.TestCase):
    def test_executor_only_does_not_complete_two_role_node(self):  # AC1
        self.assertFalse(scheduler.node_is_complete(
            [make_task(f"{WF}-plan-executor", "executor")], node=PLAN_NODE))

    def test_both_roles_complete_including_replacement_suffix(self):  # AC2
        tasks = [
            make_task(f"{WF}-plan-executor", "executor"),
            make_task(f"{WF}-plan-challenger-r2", "challenger",
                      supersedes=f"{WF}-plan-challenger"),
        ]
        self.assertTrue(scheduler.node_is_complete(tasks, node=PLAN_NODE))

    def test_challenger_only_does_not_complete(self):  # AC2
        self.assertFalse(scheduler.node_is_complete(
            [make_task(f"{WF}-plan-challenger", "challenger")], node=PLAN_NODE))

    def test_explicit_role_wins_over_contradictory_task_id(self):  # AC8
        from herdr.scheduler import resolve_task_role

        task = make_task(f"{WF}-plan-executor-challenger", "executor")
        self.assertEqual(
            resolve_task_role(task, role_names=("executor", "challenger"),
                              workflow_id=WF, node_id="plan"),
            "executor",
        )
        self.assertFalse(scheduler.node_is_complete([task], node=PLAN_NODE))

    def test_explicit_worker_placeholder_does_not_fall_back(self):  # R1 / R6
        from herdr.scheduler import resolve_task_role

        task = make_task(f"{WF}-plan-executor", "worker")
        self.assertEqual(
            resolve_task_role(task, role_names=("executor", "challenger"),
                              workflow_id=WF, node_id="plan"),
            "worker",
        )
        self.assertFalse(scheduler.node_is_complete([task], node=PLAN_NODE))

    def test_restricted_inference_strips_replacement_and_conflict_suffixes(self):  # R2 / R4
        from herdr.scheduler import resolve_task_role

        cases = (
            (f"{WF}-plan-executor", "executor"),
            (f"{WF}-plan-executor-auto", "executor"),
            (f"{WF}-plan-executor-auto-2", "executor"),
            (f"{WF}-plan-challenger-r2", "challenger"),
            (f"{WF}-plan-challenger-auto-r3", "challenger"),
        )
        for task_id, expected in cases:
            with self.subTest(task_id=task_id):
                self.assertEqual(
                    resolve_task_role(make_task(task_id),
                                      role_names=("executor", "challenger"),
                                      workflow_id=WF, node_id="plan"),
                    expected,
                )
        self.assertEqual(
            resolve_task_role(make_task(f"{WF}-plan-executor"),
                              role_names=("executor", "challenger")),
            "",
            "workflow_id/node_id missing must disable inference",
        )
        self.assertEqual(
            resolve_task_role(make_task(f"{WF}-plan-executor-r2-auto"),
                              role_names=("executor", "challenger"),
                              workflow_id=WF, node_id="plan"),
            "",
            "only the generated suffix order (replacements end with -rN) is accepted",
        )

    def test_workflow_prefix_is_not_role_evidence_and_legacy_semantics_hold(self):  # R2 / R3
        from herdr.scheduler import resolve_task_role

        task = make_task(f"{WF}-plan-executor")
        self.assertEqual(
            resolve_task_role(task, role_names=("challenger",),
                              workflow_id=WF, node_id="plan"),
            "",
            "the wf-challenger-fix prefix must not match the challenger role",
        )
        self.assertTrue(
            scheduler.node_is_complete([make_task("legacy-task")], node=PLAN_NODE),
            "tasks with no recognizable role keep the legacy completion semantics",
        )

    def test_metadata_precedence_normalization_and_scoped_inference(self):
        cases = [
            ({"dispatch_role": " Executor ", "role": "challenger"}, "executor"),
            ({"dispatch_role": " ", "role": " Challenger ", "agent_role": "executor"}, "challenger"),
            ({"agent_role": " Executor "}, "executor"),
            ({"role": "worker"}, "worker"),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(scheduler.resolve_task_role(
                    {**make_task(f"{WF}-plan-challenger"), **fields},
                    role_names=("executor", "challenger"), workflow_id=WF, node_id="plan"),
                    expected)
        for task_id in (f"{WF}-other-executor", f"{WF}-plan-executor-extra",
                        f"other-{WF}-plan-executor", f"{WF}-plan-executor-r2-r3",
                        f"{WF}-plan-executor-auto-auto"):
            with self.subTest(task_id=task_id):
                self.assertEqual(scheduler.resolve_task_role(
                    make_task(task_id), role_names=("executor", "challenger"),
                    workflow_id=WF, node_id="plan"), "")


class NodeDispatchRoleIdentityTest(unittest.TestCase):
    def setUp(self):
        self.workflow = {
            "workflow_id": WF,
            "status": "running",
            "execution_id": "exec-1",
            "created_at": 1,
        }
        self.config = {"nodes": [PLAN_NODE, IMPLEMENTATION_NODE]}
        payload = node_dispatch.payload(self.workflow, self.config, PLAN_NODE)
        self.operation = {
            "id": "op-1",
            "workflow_id": WF,
            "payload": payload,
            "identity_key": payload["identity_key"],
            "status": "pending",
            "started": False,
            "detail": {},
            "attempts": 0,
        }

    def test_executor_only_does_not_supersede_existing_dispatch(self):  # AC3
        tasks = [make_task(f"{WF}-plan-executor", "executor")]
        with mock.patch("herdr.plan_parser.verify_plan_and_challenger",
                        return_value=(True, "", {})):
            result = node_dispatch.result(
                self.operation, self.workflow, self.config, tasks, 100)
        self.assertIsNone(result)

    def test_both_roles_still_supersede_existing_dispatch(self):  # AC3
        tasks = [
            make_task(f"{WF}-plan-executor", "executor"),
            make_task(f"{WF}-plan-challenger", "challenger"),
        ]
        with mock.patch("herdr.plan_parser.verify_plan_and_challenger",
                        return_value=(True, "", {})):
            result = node_dispatch.result(
                self.operation, self.workflow, self.config, tasks, 100)
        self.assertEqual(result, (
            "superseded",
            {
                "reason": "dispatch_existing_tasks",
                "responsibility_transferred_to": "registered_tasks",
                "task_runs": {
                    f"{WF}-plan-executor": f"run-{WF}-plan-executor",
                    f"{WF}-plan-challenger": f"run-{WF}-plan-challenger",
                },
            },
        ))

    def test_downstream_readiness_requires_all_declared_roles(self):  # AC4
        workflow = {"workflow_id": WF, "status": "running", "execution_id": "exec-1"}
        only_executor = [make_task(f"{WF}-plan-executor", "executor")]
        self.assertFalse(node_dispatch.dependencies_ready(
            workflow, self.config, IMPLEMENTATION_NODE, only_executor))
        both = only_executor + [make_task(f"{WF}-plan-challenger", "challenger")]
        self.assertTrue(node_dispatch.dependencies_ready(
            workflow, self.config, IMPLEMENTATION_NODE, both))

    def test_prior_unknown_delivery_accepts_inferred_and_agent_role_metadata(self):
        self.operation.update(status="waiting_human", detail={
            "reason": "dispatch_prior_delivery_unknown"})
        tasks = [make_task(f"{WF}-plan-executor-auto-r3", stage_verdict="pass"),
                 make_task("legacy-review", agent_role="challenger", stage_verdict="pass")]
        with mock.patch("herdr.plan_parser.verify_plan_and_challenger",
                        return_value=(True, "", {})):
            result = node_dispatch.result(
                self.operation, self.workflow, self.config, tasks, 100)
        self.assertEqual(result[0], "superseded")
        self.assertEqual(result[1]["task_runs"], {
            f"{WF}-plan-executor-auto-r3": f"run-{WF}-plan-executor-auto-r3",
            "legacy-review": "run-legacy-review"})


class PlanReviewGateTest(unittest.TestCase):
    def ports(self, tasks):
        return SimpleNamespace(
            load_tasks=lambda: tasks,
            workflow_config_for=lambda workflow_id: {"nodes": [PLAN_NODE]},
            scheduler_core=scheduler,
            _reverification_satisfies_node=lambda workflow_id, node_id: False,
        )

    def tasks(self):
        return [
            make_task(f"{WF}-plan-executor", "executor"),
            make_task(f"{WF}-plan-challenger", "challenger"),
        ]

    def test_plan_review_must_exist_match_and_be_fresh(self):  # AC5
        """Only the notes data source is mocked; verify_plan_and_challenger itself
        runs its real Plan-Ref, freshness and verdict checks."""
        ports = self.ports(self.tasks())
        with mock.patch("herdr.workflow_docs.load_notes",
                        return_value=[plan_note()]):
            self.assertFalse(stage_is_node_complete(ports, WF, "plan"),
                             "challenger completed without review must not advance")

        with mock.patch("herdr.workflow_docs.load_notes",
                        return_value=[plan_note(), review_note(ref="n-other-plan")]):
            self.assertFalse(stage_is_node_complete(ports, WF, "plan"),
                             "review targeting another plan must not advance")

        with mock.patch("herdr.workflow_docs.load_notes",
                        return_value=[plan_note(ts=300.0), review_note(ts=200.0)]):
            self.assertFalse(stage_is_node_complete(ports, WF, "plan"),
                             "review older than the plan must not advance")

        with mock.patch("herdr.workflow_docs.load_notes",
                        return_value=[plan_note(), review_note()]):
            self.assertTrue(stage_is_node_complete(ports, WF, "plan"),
                            "matching fresh reviewer pass must advance")


class PlanStageDispatchRoleTest(unittest.TestCase):
    def test_completed_executor_without_metadata_is_not_redispatched(self):  # AC6
        node = {
            "id": "plan",
            "label": "计划",
            "purpose": "拆解任务",
            "required_outputs": ["方案"],
            "agent_policy": {
                "max_concurrency": 2,
                "roles": [
                    {"name": "challenger", "label": "对抗",
                     "depends_on_role": "executor"},
                    {"name": "executor", "label": "架构"},
                ],
            },
        }
        executor = make_task(f"{WF}-plan-executor")  # no explicit role metadata
        with mock.patch("herdr.workflow_docs.load_notes", return_value=[]):
            result = plan_stage_dispatch(
                WF, node, [executor], "需求", project_root="/tmp")
        self.assertNotEqual(result.get("mode"), "dispatch")
        self.assertEqual(
            [spec for spec in result.get("specs", [])
             if spec.get("dispatch_role") == "executor"],
            [],
            "completed executor must not be dispatched again",
        )

        with mock.patch("herdr.workflow_docs.load_notes",
                        return_value=[plan_note()]):
            result = plan_stage_dispatch(
                WF, node, [executor], "需求", project_root="/tmp")
        self.assertEqual(result.get("mode"), "dispatch")
        self.assertEqual(
            [spec.get("dispatch_role") for spec in result["specs"]],
            ["challenger"],
        )
        self.assertEqual(result["specs"][0]["task_id"],
                         f"{WF}-plan-challenger")

    def test_workflow_id_does_not_force_challenger_for_implementation(self):  # AC7
        task = make_task(f"{WF}-plan-executor", "executor")
        with mock.patch("herdr.workflow_docs.load_notes", return_value=[plan_note()]):
            result = plan_stage_dispatch(
                WF, IMPLEMENTATION_NODE, [task], "需求", project_root="/tmp")
        self.assertEqual(result.get("mode"), "dispatch", result)
        self.assertEqual(len(result["specs"]), 1)

    def test_explicit_executor_with_challenger_id_does_not_force_review(self):
        task = make_task(f"{WF}-plan-challenger", "executor")
        with mock.patch("herdr.workflow_docs.load_notes",
                        return_value=[plan_note(task_id=task["task_id"])]):
            result = plan_stage_dispatch(
                WF, IMPLEMENTATION_NODE, [task], "需求", project_root="/tmp")
        self.assertEqual(result.get("mode"), "dispatch", result)

    def test_review_aliases_require_review_but_word_fragments_do_not(self):
        for role, requires_review in (("reviewer", True), ("adversarial", True),
                                      ("preview", False), ("executor", False)):
            with self.subTest(role=role):
                task = make_task(f"{WF}-plan-{role}", role)
                with mock.patch("herdr.workflow_docs.load_notes",
                                return_value=[plan_note(task_id=task["task_id"])]):
                    result = plan_stage_dispatch(
                        WF, IMPLEMENTATION_NODE, [task], "需求", project_root="/tmp")
                self.assertEqual(result.get("mode") == "dispatch", not requires_review, result)


class WorkflowDispatchDiscoveryTest(unittest.TestCase):
    def test_sqlite_discovery_waits_for_both_roles_and_persists_one_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {
                "HOME": tmp, "HERDR_STATE_DB": str(Path(tmp) / "state.db"),
                "TASKS_FILE": str(Path(tmp) / "tasks.json"),
                "WORKFLOWS_FILE": str(Path(tmp) / "workflows.json"),
            }):
            reset_state_store()
            try:
                store = get_state_store(Path(tmp) / "state.db")
                store.save_workflow({
                    "workflow_id": WF, "status": "running", "execution_id": "exec-1",
                    "created_at": 1, "config": {"nodes": [PLAN_NODE, IMPLEMENTATION_NODE]},
                })
                store.save_task(make_task(f"{WF}-plan-executor", "executor"))
                with mock.patch("herdr.workflow_docs.load_notes", return_value=[plan_note()]):
                    node_dispatch_store.reconcile_workflow(store.db_path, WF, now=1000)
                self.assertIsNone(node_dispatch_store.operation_for_node(
                    store.db_path, WF, "implementation"))
                store.save_task(make_task(f"{WF}-plan-challenger", "challenger"))
                with mock.patch("herdr.workflow_docs.load_notes",
                                return_value=[plan_note(), review_note()]):
                    node_dispatch_store.reconcile_workflow(store.db_path, WF, now=1001)
                    node_dispatch_store.reconcile_workflow(store.db_path, WF, now=1002)
                operations = [op for op in recovery_store.list_operations(store.db_path, WF)
                              if op["payload"].get("node_id") == "implementation"]
                self.assertEqual(len(operations), 1)
                self.assertEqual(operations[0]["status"], "pending")
            finally:
                reset_state_store()
