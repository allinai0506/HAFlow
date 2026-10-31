"""PR #356 plan/dispatch 评审遗留回归（批次 5）。

- [5b] 显式 dispatch_role 元数据优先于 task_id 关键字推断。
- [5c] 围栏代码块以验证命令关键字开头时必须可解析。
- [5d] 多任务计划的共享 preamble 必须随每个任务进入实现提示词。
- [5e] staged 工作流的失效任务事件不得写成假干预 attention。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import yaml

from herdr.workflow import find_node

TEMPLATE = "workflow_templates/software-development-v1.yaml"


def test_explicit_role_metadata_wins_over_task_id_inference():
    """[5b] workflow ID 含 review 时显式 executor 角色不得被误判为 challenger。"""
    from herdr.plan_parser import find_plan_deliverables

    wf_id = "wf-code-review-1027-01"
    tasks = [
        {"task_id": f"{wf_id}-plan-a", "workflow_id": wf_id, "node": "plan",
         "dispatch_role": "executor", "status": "completed"},
        {"task_id": f"{wf_id}-plan-b", "workflow_id": wf_id, "node": "plan",
         "dispatch_role": "challenger", "status": "completed"},
    ]
    with patch("herdr.workflow_docs.load_notes", return_value=[]):
        _plan_text, _chal_text, executor_task, challenger_task = find_plan_deliverables(
            wf_id, tasks=tasks)
    assert executor_task is not None, "显式 executor 角色不得因 task_id 含 review 被误判"
    assert executor_task["dispatch_role"] == "executor"
    assert challenger_task is not None and challenger_task["dispatch_role"] == "challenger"


def test_verify_command_at_fence_start_is_recognized():
    """[5c] 围栏代码块以 pytest 开头时验证命令必须可解析。"""
    from herdr.plan_parser import extract_verify_command

    chunk = "```bash\npytest -q tests/test_alpha.py\n```"
    assert extract_verify_command(chunk) == "pytest -q tests/test_alpha.py"
    # 既有形态回归：关键字前有其他文字仍可解析
    chunk2 = "```bash\nRun: pytest -q tests/test_beta.py\n```"
    assert "pytest -q tests/test_beta.py" in extract_verify_command(chunk2)


def test_multi_task_plan_attaches_shared_preamble():
    """[5d] 首个任务头之前的共享约束必须随每个任务携带。"""
    from herdr.plan_parser import parse_plan_tasks

    plan_text = """# Implementation Plan
## 全局约束（所有任务必须遵守）
- 严禁修改 herdr/kernel.py
- 统一使用仓库现有状态存储，不引入平行事实源

## 任务拆分
### Task 1: 核心模块
- **Files:**
  - Create: pref_core.py
  - Test: test_pref_core.py
- **Verify:**
  pytest -q test_pref_core.py

### Task 2: CLI 模块
- **Files:**
  - Create: pref_cli.py
  - Test: test_pref_cli.py
- **Verify:**
  pytest -q test_pref_cli.py
"""
    tasks = parse_plan_tasks(plan_text)
    assert len(tasks) == 2
    for task in tasks:
        preamble = task.get("plan_preamble") or ""
        assert "严禁修改 herdr/kernel.py" in preamble
        assert "### Task" not in preamble


def test_plan_dispatch_prompt_carries_shared_preamble():
    """[5d] 端到端：实现 spec 的 plan_chunk 携带计划级共享约束。"""
    from herdr.direct_dispatch import plan_stage_dispatch

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    impl_node = find_node(cfg, "implementation")

    wf_id = "wf-plan-preamble-01"
    preamble = "## 全局约束\n- 严禁修改 herdr/kernel.py\n- 复用既有状态存储\n"
    plan_body = f"""# Implementation Plan
{preamble}
### Task 1: 核心模块
- **Goal:** 实现偏好存储核心接口
- **Files:**
  - Create: pref_core.py
  - Test: test_pref_core.py
- **Verify:**
  pytest -q test_pref_core.py
- **Dependencies:** None

### Task 2: CLI 模块
- **Goal:** 实现偏好设置命令行导出
- **Files:**
  - Create: pref_cli.py
  - Test: test_pref_cli.py
- **Verify:**
  pytest -q test_pref_cli.py
- **Dependencies:** None
"""
    challenger_body = """
    ## 方案对抗性审查与可行性风险评估
    质询审查：两个任务文件范围完全解耦互不重叠，pref_core 与 pref_cli 无共享写文件，无死锁和循环依赖风险，
    验证命令独立且完备，各自可独立自测。计划级全局约束（严禁修改 herdr/kernel.py、复用既有状态存储）
    已在实现任务提示词中逐任务传达。经详细审查，未发现阻塞性缺陷，结论：建议通过。
    """
    tasks = [
        {"task_id": f"{wf_id}-plan-executor", "workflow_id": wf_id, "node": "plan",
         "dispatch_role": "executor", "status": "completed"},
        {"task_id": f"{wf_id}-plan-challenger", "workflow_id": wf_id, "node": "plan",
         "dispatch_role": "challenger", "status": "completed"},
    ]
    notes = [
        {"node": "plan", "kind": "plan", "title": "解耦实现计划", "body": plan_body,
         "task_id": f"{wf_id}-plan-executor"},
        {"node": "plan", "kind": "review", "title": "对抗质询", "body": challenger_body,
         "task_id": f"{wf_id}-plan-challenger"},
    ]
    with patch("herdr.workflow_docs.load_notes", return_value=notes):
        res = plan_stage_dispatch(wf_id, impl_node, tasks, "实现偏好设置", context_branch="main")
    assert res.get("mode") == "dispatch", res
    specs = res.get("specs", [])
    assert len(specs) == 2
    for spec in specs:
        chunk = str(spec.get("plan_chunk") or "")
        assert "严禁修改 herdr/kernel.py" in chunk, "共享 preamble 必须进入每个实现任务的 plan_chunk"


def _staged_ports(status):
    ports = MagicMock()
    ports.get_task.return_value = {
        "task_id": "task-x",
        "workflow_id": "wf-dev-01",
        "status": status,
    }
    ports.workflow_config_for.return_value = {"workflow_template": "software-development-v1"}
    ports.try_auto_accept.return_value = False
    ports.try_auto_verdict.return_value = False
    ports.verdict_defer_reason.return_value = "accept_race_or_unknown"
    return ports


def test_stale_done_event_not_recorded_as_attention_in_staged_flow():
    """[5e] 已推进任务的排队 done 事件不得写成 auto_acceptance_rejected 假干预。"""
    from services.controller import task_events

    ports = _staged_ports("completed")
    item = {"key": "task-x:event", "task_id": "task-x", "event_type": "done",
            "workflow_id": "wf-dev-01"}
    task_events._handle_task_event(ports, item)
    ports.attention_note.assert_not_called()


def test_stale_attention_event_dropped_in_staged_flow():
    """[5e] 已回到活跃态任务的排队 attention 事件必须按失效丢弃。"""
    from services.controller import task_events

    ports = _staged_ports("working")
    item = {"key": "task-x:event", "task_id": "task-x", "event_type": "attention",
            "workflow_id": "wf-dev-01"}
    task_events._handle_task_event(ports, item)
    ports.attention_note.assert_not_called()
    ports.attention_clear.assert_called_once_with("task-x:event")
