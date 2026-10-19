# ADR-0007：工作流初始状态语义收敛与注册生命周期

## 背景

在 HAFlow 状态流转矩阵中，`herdr/transitions.py` L89-97 明确将 `pending` 定义为工作流生命周期的初始合法状态（`pending -> {"running", "in_progress", "paused", "failed"}`）。

但在工程实际中，存在明显的设计与实现割裂（语义冲突 SC-011，详见 `decisions/0003-semantic-conflicts.md`）：
1. 正常项目注册路径（`herdr/projects.py:593` 的 `register_workflow`）在装配工作区、工位与配置后，直接向存储层写入 `status: "running"`，从未经过 `pending` 状态，也未生成 `pending -> running` 迁移记录；
2. 占位工作流路径（`herdr/state_db.py:1617`）在任务先于工作流定义到达时，向存储层写入 `status: "pending"` 的占位记录；
3. 调度器 `services/herdr-controller.py` 对处于 `running` / `in_progress` 的工作流进行常规节点轮询，而目前系统中并无独立的后台守护协程去主动扫描并将 `pending` 工作流调度为 `running`。

这导致了本体模型与生产运行在“工作流起点究竟是 pending 还是 running”上存在持续歧义。

## Evidence

- `herdr/transitions.py` L89-97：`WORKFLOW_TRANSITIONS` 状态矩阵定义。
- `herdr/projects.py` L578-603 (`register_workflow`)：`wf_entry = {"status": "running", ...}` 并直接持久化。
- `herdr/state_db.py` L1617：占位工作流写入 `status: "pending"`。
- `herdr/state_db.py` L1080 & L7929：快照缺失时默认回退 `status: "pending"`。
- `services/herdr-controller.py` L10669-10725：工作流调度与关闭逻辑。

## 决策

1. **精准划分两个初始状态的业务语义（Dual-Initial State Semantics）**：
   - **`running` 为标准装配初始态（Active Initial State）**：通过 `herdr-factory` 或 `projects.ensure_project` 正式初始化的工作流，因项目环境、工位分配与模板装配已全部就绪，其生命周期起点确立为 `running`。调度器直接对其进行 DAG 阶段推进。
   - **`pending` 为系统未装配/占位态（Unprovisioned Placeholder / Draft State）**：仅用于先有任务外部引用、后补全工作流定义，或离线草稿导入场景。占位工作流不参与常规节点推进；一旦后续被正式装配，必须经由原子网关执行显式迁移 `transition_workflow(pending -> running)` 激活。

2. **工作流创建升格为一等动作（First-class Creation Action）**：
   - 将 `register_workflow` 在本体中建模为一等创建动作，其执行后作用（Effects）明确声明为 `creates workflow with status=running`。
   - 在代码注册路径中，补齐同事务的 `workflow_created` 权威生命周期审计事件，彻底终结“裸写数据库无事件”的历史债务。

3. **状态机模型对齐**：
   - `ontology/states/workflow_lifecycle.yaml` 的 `initial` 属性正式定案为 `running`。
   - 保留状态集合中的 `pending` 状态及 `pending -> running`、`pending -> failed` 转移边，作为占位工作流及延迟装配工作流的合法受控通道。

## 被拒绝方案

- **方案 1：强制所有工作流注册必须先写 `pending`，再两阶段推进为 `running`**。
  - 被拒绝理由：两阶段写入徒增存储往返延迟，并且如果两阶段之间进程崩溃，缺少拉起机制会导致工作流永久卡死在 `pending`；同时会导致大量已有自动化测试与 CLI 工具失效。
- **方案 2：直接从状态矩阵中删除 `pending` 状态**。
  - 被拒绝理由：`state_db.py:1617` 的占位工作流机制是保障任务引用完整性的关键基础设施，且快照回退需要 `pending` 表达未决状态。

## 影响

- 彻底消除 SC-011 冲突，模型声明与生产真实行为严密吻合。
- 候选本体 `states/workflow_lifecycle.yaml` 与相关文档解除 experimental 决策阻塞。
- 未来在 `register_workflow` 中追加 `workflow_created` 规范事件。

## 兼容性

- 对现有运行代码零破坏，完全兼容当前所有控制器与 CLI 运行逻辑。
