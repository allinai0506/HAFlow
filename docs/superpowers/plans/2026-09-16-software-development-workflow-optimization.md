# 软件开发标准流程优化实施计划
(Software Development Workflow Optimization Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化内置软件开发标准工作流模板 `software-development-v1` 及底层调度与派发引擎，实现 Pane 数量严格收敛、需求/计划阶段双工位对抗审查、实现阶段自适应解耦并发、测试/评审/收尾阶段单工位跨阶段 Agent 隔离。

**Architecture:** 
1. 在 `herdr/agent_router.py` 中增强 `choose_agent` 解析 `exclude_stage_agents` 策略，动态查询 Workflow 既往阶段执行者并从候选池过滤排除，带单 Agent 环境优雅降级兜底；
2. 在 `herdr/direct_dispatch.py` 中增强 `plan_stage_dispatch` 支持角色化（roles）多规格派发，使需求与计划阶段规则化生成 `executor` 与 `challenger` 双工位；
3. 更新 `workflow_templates/software-development-v1.yaml`，全面配置 6 节点工位上限、角色契约、交付物及隔离红线；
4. 编写全量单元与集成测试套件，执行全仓 500+ 测试回归。

**Tech Stack:** Python 3.13, PyYAML, SQLite (StateStore), pytest.

## Global Constraints
- 业务纯逻辑收敛在 `herdr/`，严禁在 CLI 或服务外壳揉捏复杂逻辑。
- 严禁引入任何未在项目中使用的外部第三方包（Ponytail Principle）。
- 保持向后兼容：未配置 `roles` 或 `exclude_stage_agents` 的旧模板（`bidding`, `customer-service`）行为保持 100% 原样。
- 全仓既有 500 个自动化测试必须 100% 保持绿色通过。

---

### Task 1: 增强 Agent 路由器支持跨阶段执行者隔离 (`exclude_stage_agents`)

**Files:**
- Modify: `herdr/agent_router.py:215-340`
- Test: `tests/test_agent_router_stage_exclusion.py`

**Interfaces:**
- Consumes:
  - `store.list_tasks() -> List[Dict[str, Any]]`
  - `node_policy.get("exclude_stage_agents") -> List[str]`
- Produces:
  - `choose_agent(workflow_id, stage, task_type, requested="auto", reservation_key=None) -> str` (剔除排除阶段已用 Agent 后的最佳 Agent)

- [ ] **Step 1: 编写跨阶段 Agent 隔离测试用例**

创建 `tests/test_agent_router_stage_exclusion.py`，测试覆盖：
1. 当 `node_policy` 声明 `exclude_stage_agents: ["implementation"]` 且实现阶段已使用 `codex` 时，`choose_agent` 自动选择 `claude` 或其他健康 Agent，绝不返回 `codex`；
2. 当调用方显式指定 `--agent codex`（且与排除阶段冲突）且存在其他可用 Agent 时，抛出 `RuntimeError` 拒绝违规指定；
3. 单 Agent 极端环境下的优雅降级（不报错阻断，返回可用 Agent）。

```python
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr import agent_router
from herdr.state_store import get_state_store


class TestAgentRouterStageExclusion(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "state.db"
        self.store = get_state_store(self.db_path)
        self.wf_id = "wf-test-exclusion"
        self.store.save_workflow({
            "workflow_id": self.wf_id,
            "project_id": "test-proj",
            "status": "running",
            "healthy_agents": ["codex", "claude", "opencode"],
            "unhealthy_agents": {},
        })
        self.store.save_task({
            "task_id": "task-impl-1",
            "workflow_id": self.wf_id,
            "node": "implementation",
            "stage": "implementation",
            "agent": "codex",
            "status": "completed",
        })

    def tearDown(self):
        self.tmp_dir.cleanup()

    @patch("herdr.agent_router._get_store")
    @patch("herdr.agent_router.workflow_config_for")
    def test_exclude_stage_agents_auto_selection(self, mock_wf_cfg, mock_get_store):
        mock_get_store.return_value = self.store
        mock_wf_cfg.return_value = {
            "nodes": [
                {
                    "id": "test",
                    "agent_policy": {
                        "preferred": ["codex", "claude", "opencode"],
                        "exclude_stage_agents": ["implementation"],
                    }
                }
            ]
        }
        chosen = agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertNotEqual(chosen, "codex")
        self.assertEqual(chosen, "claude")

    @patch("herdr.agent_router._get_store")
    @patch("herdr.agent_router.workflow_config_for")
    def test_explicit_requested_conflict_raises(self, mock_wf_cfg, mock_get_store):
        mock_get_store.return_value = self.store
        mock_wf_cfg.return_value = {
            "nodes": [
                {
                    "id": "review",
                    "agent_policy": {
                        "preferred": ["claude", "opencode"],
                        "exclude_stage_agents": ["implementation"],
                    }
                }
            ]
        }
        with self.assertRaises(RuntimeError) as ctx:
            agent_router.choose_agent(self.wf_id, "review", "test", requested="codex")
        self.assertIn("prohibited", str(ctx.exception).lower())

    @patch("herdr.agent_router._get_store")
    @patch("herdr.agent_router.workflow_config_for")
    def test_single_agent_environment_graceful_fallback(self, mock_wf_cfg, mock_get_store):
        self.store.save_workflow({
            "workflow_id": self.wf_id,
            "project_id": "test-proj",
            "status": "running",
            "healthy_agents": ["codex"],
            "unhealthy_agents": {},
        })
        mock_get_store.return_value = self.store
        mock_wf_cfg.return_value = {
            "nodes": [
                {
                    "id": "test",
                    "agent_policy": {
                        "preferred": ["codex"],
                        "exclude_stage_agents": ["implementation"],
                    }
                }
            ]
        }
        chosen = agent_router.choose_agent(self.wf_id, "test", "test", requested="auto")
        self.assertEqual(chosen, "codex")
```

- [ ] **Step 2: 运行测试验证失败**

运行：`pytest tests/test_agent_router_stage_exclusion.py -v`
预期：FAIL（`choose_agent` 尚未实现 `exclude_stage_agents` 逻辑）。

- [ ] **Step 3: 在 `herdr/agent_router.py` 中实现 `exclude_stage_agents`**

在 `herdr/agent_router.py` 的 `choose_agent` 中加入排除逻辑：
1. 获取 `exclude_stages = node_policy.get("exclude_stage_agents") or node_policy.get("disallow_from_stages") or []`；
2. 若 `exclude_stages` 存在，遍历 `store.list_tasks()` 获取该 `workflow_id` 下对应阶段已分配的 agent 集合 `stage_used_agents`；
3. 若 `requested != "auto"` 且 `requested in stage_used_agents`：检查系统是否存在其他可用候选；若有，抛出 `RuntimeError` 拒绝违规指定；
4. 在计算 candidates 候选时，若存在非被排除的可用健康 Agent，过滤剔除 `stage_used_agents`；若剔除后无剩余可用 Agent，保留原 candidates 并记录降级日志。

- [ ] **Step 4: 重新运行测试验证通过**

运行：`pytest tests/test_agent_router_stage_exclusion.py -v`
预期：PASS。

- [ ] **Step 5: 提交任务代码**

```bash
git add herdr/agent_router.py tests/test_agent_router_stage_exclusion.py
git commit -m "feat(router): support exclude_stage_agents for cross-stage agent isolation"
```

---

### Task 2: 增强直接派发引擎支持角色化双工位派发 (`roles`)

**Files:**
- Modify: `herdr/direct_dispatch.py:80-105,234-339`
- Test: `tests/test_direct_stage_dispatch.py`

**Interfaces:**
- Consumes:
  - `node.get("agent_policy", {}).get("roles") -> List[Dict[str, Any]]`
- Produces:
  - `plan_stage_dispatch(workflow_id, node, tasks, requirement, context_branch=None) -> Dict[str, Any]`
  - 返回带有多个角色规范的 `specs: [executor_spec, challenger_spec]`。

- [ ] **Step 1: 在 `tests/test_direct_stage_dispatch.py` 中补充角色化派发测试**

添加测试：
1. 当 node 配置了 `agent_policy.roles`（如 `executor` 与 `challenger`）时，`plan_stage_dispatch` 返回 `specs` 长度为 2；
2. `specs[0]` 的 `task_id` 后缀为 `-executor`，包含执行者目标与交付物；
3. `specs[1]` 的 `task_id` 后缀为 `-challenger`，包含对抗性质询者目标与边界清单交付物；
4. 当节点已有部分或全部活跃任务时，`mode` 返回 `wait`。

- [ ] **Step 2: 运行测试验证失败**

运行：`pytest tests/test_direct_stage_dispatch.py -k test_roles -v`
预期：FAIL。

- [ ] **Step 3: 在 `herdr/direct_dispatch.py` 中实现角色化派发**

1. 在 `normalize_node` 中提取 `roles = (node.get("agent_policy") or node.get("worker_policy") or {}).get("roles") or []`；
2. 在 `plan_stage_dispatch` 的初始派发分支中：
   - 若 `roles` 非空：
     遍历 `roles` 列表，为每个 role 构造独立的 `task_id = f"{workflow_id}-{node_id}-{role['name']}"`、`goal` 和 `acceptance`；
     返回 `{"mode": "dispatch", "reason": "initial node dispatch with roles", "specs": specs}`；
   - 若 `roles` 为空：
     保持既有单任务 spec 逻辑。

- [ ] **Step 4: 重新运行测试验证通过**

运行：`pytest tests/test_direct_stage_dispatch.py -v`
预期：全量测试 100% PASS。

- [ ] **Step 5: 提交任务代码**

```bash
git add herdr/direct_dispatch.py tests/test_direct_stage_dispatch.py
git commit -m "feat(direct-dispatch): support multi-spec dispatch based on node agent_policy roles"
```

---

### Task 3: 优化工作流模板 `software-development-v1.yaml`

**Files:**
- Modify: `workflow_templates/software-development-v1.yaml`
- Test: `tests/test_software_development_v1_template.py`

**Interfaces:**
- Consumes:
  - `workflow.load_template("software-development-v1")`
  - `workflow.validate_workflow_dag(nodes)`
- Produces:
  - 生产级 `software-development-v1.yaml` 模板定义文件。

- [ ] **Step 1: 编写模板规范校验测试**

创建 `tests/test_software_development_v1_template.py`，验证：
1. 模板正确加载且版本规范正确；
2. `requirements` 节点包含 `max_agents: 2` 与 `executor` / `challenger` 两个角色；
3. `plan` 节点包含 `max_agents: 2` 与 `executor` / `challenger` 两个角色；
4. `implementation` 节点包含 `max_agents: 3` 与 `parallel: true`；
5. `test`、`review`、`wrapup` 节点包含 `max_agents: 1`、`parallel: false` 与 `exclude_stage_agents: ["implementation"]`；
6. 所有阶段的 rules 均不含导致无序膨胀的模糊描述，并严格包含工位上限约束。

- [ ] **Step 2: 运行测试验证失败**

运行：`pytest tests/test_software_development_v1_template.py -v`
预期：FAIL。

- [ ] **Step 3: 更新 `workflow_templates/software-development-v1.yaml`**

按照规范完整重构 6 个节点的配置、agent_policy 与 rules。

- [ ] **Step 4: 运行测试验证通过**

运行：`pytest tests/test_software_development_v1_template.py -v`
预期：PASS。

- [ ] **Step 5: 提交模板更新**

```bash
git add workflow_templates/software-development-v1.yaml tests/test_software_development_v1_template.py
git commit -m "feat(workflow-template): optimize software-development-v1 with dual-adversarial and stage-exclusion rules"
```

---

### Task 4: 全量回归与集成验收

**Files:**
- Docs: `wiki/log.md`, `docs/lessons/lessons-learned.md`
- Verification: 全仓 pytest 套件

- [ ] **Step 1: 运行全量测试套件**

运行：`pytest`
预期：500+ 个测试 100% 全部通过，零 warning，零 failure。

- [ ] **Step 2: 沉淀 Wiki 变更与技术教训**

- 在 `wiki/log.md` 记录本次优化；
- 更新 `docs/lessons/lessons-learned.md` 沉淀多 Agent 角色分工与跨阶段隔离教训；
- 运行 `./bin/herdr-factory doctor` 验证环境与模板整体状态。

- [ ] **Step 3: 最终提交**

```bash
git add wiki/ docs/
git commit -m "docs: document workflow optimization and codify multi-agent coordination lessons"
```
