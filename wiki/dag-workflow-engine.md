# DAG 工作流引擎与调度算法 (dag-workflow-engine.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> **模板规范、Kahn 拓扑校验算法、双向归一化与就绪推进**  
> 关联索引: [[index]] | [[domain-model]] | [[task-lifecycle]] | [[architecture]]

---

## 1. 模板体系与加载发现

HAFlow 支持声明式 YAML 与 JSON 工作流模板。

### 1.1 模板扫描顺序
`FACT` 引擎通过 `herdr/workflow.py#list_templates` 自动发现模版：
1. **用户自定义目录**: `~/.herdr-controller/templates/`（具有最高优先级，覆盖同名内置模板）。
2. **仓库内置目录**: `workflow_templates/`（随代码仓库分发）。

已内置的标准模板包括：
- `software-development-v1.yaml`: 标准 6 阶段研发闭环（需求分析 ➔ 计划 ➔ 实现 ➔ 测试 ➔ 评审 ➔ 收尾）。
- `bidding.yaml`: 7 阶段标书制作与审批流程。
- `customer-service.yaml`: 客诉分流与处置流程。

Evidence:
- `herdr/workflow.py:USER_TEMPLATES_DIR`
- `herdr/workflow.py:BUNDLED_TEMPLATES_DIR`
- `tests/test_workflow_engine.py#test_list_templates_bundled`

---

## 2. DAG 拓扑校验与 Kahn 算法

`FACT` 在模板加载与运行时初始化阶段，系统调用 `validate_workflow_dag(nodes)` 强制执行拓扑约束校验：

```mermaid
flowchart TD
    Start([输入 Node 列表]) --> DupCheck{检测是否存在重复 Node ID?}
    DupCheck -- 是 --> Err1[抛出 ValueError: Duplicate node IDs]
    DupCheck -- 否 --> DepCheck{前置依赖 depends_on 是否全部存在?}
    DepCheck -- 否 --> Err2[抛出 ValueError: unknown node]
    DepCheck -- 是 --> CalcDegree[计算各节点入度 In-Degree 及邻接表]
    CalcDegree --> InitQueue[将所有入度为 0 的节点放入队列 zero_in]
    InitQueue --> Loop{队列是否为空?}
    Loop -- 否 --> Pop[出队节点并累加 visited_count]
    Pop --> Reduce[将其所有下游邻接节点的入度减 1]
    Reduce --> CheckZero{下游节点入度是否变为 0?}
    CheckZero -- 是 --> Enqueue[将该节点压入队列]
    CheckZero -- 否 --> Loop
    Enqueue --> Loop
    Loop -- 是 --> FinalCheck{visited_count == 节点总数?}
    FinalCheck -- 是 --> Pass([DAG 校验通过])
    FinalCheck -- 否 --> CycleErr[抛出 ValueError: circular dependency / cycle]
```

Evidence:
- `herdr/workflow.py#validate_workflow_dag`
- `tests/test_workflow_engine.py#test_cycle_detection`
- `tests/test_workflow_engine.py#test_unknown_dependency`

---

## 3. 双向归一化适配器 (`normalize_workflow`)

### 3.1 历史兼容背景
在早期版本中，工作流以线性的 `stages` 列表表达（`[requirements, plan, ...]`），仅支持单一链式流转。  
新架构重构为通用的有向无环图 `nodes`，支持并行分支与多重聚合。

### 3.2 归一化逻辑
`FACT` `normalize_workflow(workflow)` 提供了透明的双向兼容：
- **若输入包含 `nodes`**:
  - 保留并校验 `nodes`。
  - 根据节点顺序自动生成对齐的向后兼容 `stages` 列表，为每个 stage 填充 `next` 指针。
- **若输入仅包含 legacy `stages`**:
  - 将每个 stage 自动转化为 `node_type = "agent"` 的现代 Node。
  - 自动将前一个 stage 设为后一个 stage 的 `depends_on` 前置依赖。
  - 提取 `stage_policies` 中的属性并注入各节点的 `agent_policy`。

Evidence:
- `herdr/workflow.py#normalize_workflow`
- `tests/test_workflow_engine.py#test_normalize_legacy_stages_to_nodes`
- `tests/test_workflow_engine.py#test_normalize_nodes_to_legacy_stages`

---

## 4. 就绪节点计算与推进 (`get_ready_nodes`)

### 4.1 推进规则
`FACT` 控制器在周期调度时，通过 `get_ready_nodes(workflow, completed_node_ids)` 计算当前时刻可以立刻并发派发的就绪节点：
一个 Node 满足就绪必须**同时**满足：
1. **未完成**: `node["id"] not in completed_node_ids`。
2. **全前置满足**: 节点的所有依赖项集合必须是已完成集合的子集：  
   `set(node.depends_on).issubset(completed_node_ids)`。

### 4.2 菱形与并行依赖支持
引擎天然支持菱形分支合并（Diamond Graph）模型：
- `Start` ➔ `Branch_A` & `Branch_B` ➔ `Merge`
- 当且仅当 `Branch_A` 和 `Branch_B` 均进入 `completed_node_ids` 时，`Merge` 节点才会返回在 `ready` 列表中。

Evidence:
- `herdr/workflow.py#get_ready_nodes`
- `herdr/workflow.py#is_workflow_completed`
- `tests/test_workflow_engine.py#test_join_waits_for_all_dependencies`

### 4.3 Direct Dispatch 边界

`FACT` 首次派发只接受 Agent 静态节点：有效角色列表生成固定角色 Task；无角色、`max_agents=1` 且不允许并行时生成单 Task。无固定角色但允许并行或上限不为 1 的节点回退总指挥规划，不生成通用 `-auto` Task。已有任务等待与既有被作废子集补派保持原行为。

`FACT` Controller 的 `stage_advance` 入口对非 Agent 节点输出 `STAGE ADVANCE BLOCKED`，要求人工处理，不调用 Direct Dispatch 或总指挥。该阻断不受 `HERDR_DIRECT_STAGE_DISPATCH` 开关影响；事件未携带 Node 时从工作流配置解析。它不实现 human/tool/gate 原生执行器，也不限制用户手动调用 CLI。

Evidence:
- `herdr/direct_dispatch.py#classify_dispatch`
- `herdr/direct_dispatch.py#plan_stage_dispatch`
- `services/herdr-controller.py#_handle_coordinator_item`
- `tests/test_direct_stage_dispatch.py#PlanStageDispatchTest`
- `tests/test_direct_stage_dispatch.py#TryDirectStageAdvanceTest`

### 4.4 任务级门禁结论对称性

`FACT` 任务级 `blocked` 验收结论对自动推进一律有效，不问节点有无 gate 配置：
sweep 发现就绪节点的任一依赖存在未作废 blocked 结论时，不 queue、不作废下游，
只记 attention（`upstream_blocked`，可退避）并通知总指挥裁决，作废过期结论后自动恢复；
`try_direct_stage_advance` 在规划前复查同一条件，命中则 `DIRECT DISPATCH BLOCKED`
回退总指挥。未知依赖时保持原行为（fail-open）。fix-loop 的销毁式回流仍仅对有
gate 配置的节点（test/review/wrapup）触发。

Evidence:
- `services/herdr-controller.py#blocked_verdict_dep`
- `services/herdr-controller.py#check_workflow_stage_advance`
- `services/herdr-controller.py#try_direct_stage_advance`
- `tests/test_gate_verdict_symmetry.py`

## 10. 门禁 verdict 与 fix-loop 回路

`FACT` 阶段结论（pass/blocked）是 DAG 推进的一等输入，与任务完成态正交：
`is_node_complete` 只看任务状态，门禁判定在 `check_workflow_stage_advance`
的 ready-node 循环与 workflow 完成分支两处读取 verdict（详见
[[task-lifecycle]] §1.1）。blocked 的处理不是报错而是**结构回流**：原子作废
gate+下游任务 → 节点回归未完成 → 既有 `reconcile_stage_advance_states` +
`get_ready_nodes` 机制驱动 DAG 在 fix 完成后自动重流，无需新状态机状态。

Evidence: `services/herdr-controller.py` #check_workflow_stage_advance /
#blocked_gate_dependency；`docs/walkthroughs/20260913-fix-loop-design.md`
