from herdr.coordinator_messages import (
    build_coordinator_message,
    build_stage_advance_context,
    build_stage_advance_message,
)


DISCIPLINE = "执行纪律内容"


def test_inner_loop_message_uses_injected_blocker_document_reader():
    task = {
        "task_id": "task-1",
        "workflow_id": "workflow-1",
        "stage": "implementation",
        "pane_id": "pane-1",
        "agent": "opencode",
        "goal": "修复任务",
        "acceptance_criteria": ["通过回归"],
    }
    reads = []

    message = build_coordinator_message(
        task,
        "inner_loop_exhausted",
        discipline=DISCIPLINE,
        read_loop_doc=lambda received_task, filename: reads.append(
            (received_task, filename)
        ) or "阻塞证据",
    )

    assert reads == [(task, "BLOCKER.md")]
    assert "阻塞证据" in message
    assert DISCIPLINE in message
    assert "修复任务" in message


def test_done_message_keeps_acceptance_criteria():
    message = build_coordinator_message(
        {
            "task_id": "task-1",
            "workflow_id": "workflow-1",
            "stage": "implementation",
            "pane_id": "pane-1",
            "agent": "opencode",
            "goal": "修复任务",
            "acceptance_criteria": ["通过回归"],
        },
        "done",
        discipline=DISCIPLINE,
        read_loop_doc=lambda *_: "unused",
    )
    assert "通过回归" in message
    assert "HERDR_CONTROLLER_DONE_EVENT" in message
    assert DISCIPLINE in message


def test_unknown_event_keeps_none_result_without_reading_loop_document():
    message = build_coordinator_message(
        {"task_id": "task-1"},
        "unknown",
        discipline=DISCIPLINE,
        read_loop_doc=lambda *_: (_ for _ in ()).throw(AssertionError("unexpected read")),
    )

    assert message is None


def test_stage_advance_message_preserves_intake_and_dispatch_contract():
    context = {
        "event_header": "HERDR_WORKFLOW_INTAKE_EVENT",
        "intake_note": "总指挥接单机制",
        "workflow_id": "workflow-1",
        "project_name": "demo",
        "project_root": "/tmp/demo",
        "base_branch": "main",
        "stage": "start",
        "next_stage": "implementation",
        "node_label": "实现",
        "node_type": "agent",
        "requirement": "实现需求",
        "preserved_text": "保留任务",
        "purpose": "实现功能",
        "integration_mode": "branch",
        "task_type": "feat",
        "required_outputs": "- 代码",
        "rules": "- 先验证",
        "docs_block": "共享文档",
        "agent_policy_text": "Agent 策略",
        "task_manager": "/tmp/herdr-task",
        "candidate_flags": "--candidate-sha abc123",
        "candidate_block": "冻结候选说明",
        "dispatch_op": {
            "id": "op-1",
            "attempts": 2,
            "payload": {"predecessors": [{"task_id": "task-prev"}]},
        },
    }

    message = build_stage_advance_message(context, discipline=DISCIPLINE)

    for expected in (
        "HERDR_WORKFLOW_INTAKE_EVENT",
        "总指挥接单机制",
        "实现需求",
        "保留任务",
        '"/tmp/herdr-task" list --workflow-id workflow-1',
        "--dispatch-operation-id op-1 --dispatch-attempt 2",
        "--supersedes task-prev",
        "冻结候选说明",
        DISCIPLINE,
    ):
        assert expected in message


def test_stage_advance_context_preserves_policy_and_verified_task_selection():
    context = build_stage_advance_context(
        item={"workflow_id": "workflow-1", "stage": "implementation", "stage_label": "测试"},
        stage="implementation",
        project_ctx={"project_name": "demo"},
        project_name="demo",
        project_root="",
        base_branch="",
        node={
            "id": "test",
            "label": "测试节点",
            "purpose": "验证冻结版本",
            "default_integration_mode": "none",
            "default_task_type": "test",
            "node_type": "agent",
            "agent_policy": {"preferred": ["claude"], "exclude": ["auto"]},
            "required_outputs": ["测试报告"],
            "rules": ["只测当前候选"],
            "depends_on": ["implementation"],
        },
        next_stage="test",
        policy={"purpose": "fallback purpose"},
        tasks=[
            {"task_id": "kept", "workflow_id": "workflow-1", "node": "test",
             "status": "completed", "stage_verdict": "pass"},
            {"task_id": "failed", "workflow_id": "workflow-1", "node": "test",
             "status": "superseded", "stage_verdict": "pass"},
            {"task_id": "other-workflow", "workflow_id": "workflow-2", "node": "test",
             "status": "completed", "stage_verdict": "pass"},
            {"task_id": "other-node", "workflow_id": "workflow-1", "node": "implementation",
             "status": "completed", "stage_verdict": "pass"},
        ],
        frozen_candidate=("abc123", "candidate-branch"),
    )

    assert context["purpose"] == "验证冻结版本"
    assert context["task_type"] == "test"
    assert context["preserved_text"] == (
        "\n已保留的有效任务（verdict=pass，禁止重复创建）：\n"
        "- kept\n只需补派缺失/被作废的 Task。\n"
    )
    assert context["candidate_flags"] == "   --candidate-sha abc123"
    assert "candidate_sha: abc123" in context["candidate_block"]
    assert "--onto" not in context["candidate_block"]


def test_stage_advance_context_keeps_legacy_defaults_without_node():
    context = build_stage_advance_context(
        item={"workflow_id": "workflow-1", "stage": "start", "stage_label": "旧节点"},
        stage="start",
        project_ctx={},
        project_name="legacy/unknown",
        project_root="",
        base_branch="",
        node=None,
        next_stage="legacy",
        policy={},
        tasks=[],
        frozen_candidate=(None, None),
    )

    assert context["purpose"] == "未定义"
    assert context["integration_mode"] == "none"
    assert context["task_type"] == "test"
    assert context["node_label"] == "旧节点"
    assert context["candidate_flags"] == ""
