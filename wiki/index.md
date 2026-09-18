# HAFlow LLM Wiki: 核心知识索引 (index.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> **AI Coding Agent 与工程师的主入口 (Master Entry Point)**  
> 欢迎来到 HAFlow 代码库知识层。本 Wiki 是一套完全基于仓库真实源码建立的、高保真、可追溯知识图谱。  
> **核心原则**：Wiki 是对代码已被理解过的知识沉淀，帮助您免于重新扫描全量代码；但**代码永远是唯一事实来源**。

---

## 1. 快速通读路径 (Start Here)

如果您是第一次进入本代码库，推荐按照以下顺序建立完整的心理模型：

1. **[[system-overview]] (系统全景)**: 明确系统定位（HAFlow Multi-Agent 编排与控制平台）、解决的核心业务痛点与技术边界。
2. **[[architecture]] (运行架构与守护进程)**: 掌握常驻 LaunchAgent 守护进程、Unix Domain Socket、调度轮询机制与物理存储路径。
3. **[[domain-model]] (领域实体与关系模型)**: 掌握 Project、Workflow、Node、Task、Slot、Anchor、Reservation 等核心概念。
4. **[[tab-node-model]] (Tab=Node 空间现场与自愈模型)**: 理解为什么 Tab 是工作流节点、Anchor Pane 为什么是只读母体，以及系统如何在任务派发前自动检测并自愈现场。
5. **[[task-lifecycle]] (任务生命周期与基线验收)**: 掌握 11 状态机流转、CoW (Copy-on-Write) Git 克隆隔离及防止误判的基线指纹快照机制。
6. **[[dag-workflow-engine]] (DAG 工作流引擎与 Kahn 算法)**: 理解声明式 YAML 模板解析、拓扑死锁检测与节点就绪（Ready Nodes）动态推进。
7. **[[agent-routing-and-pools]] (异构 Agent 路由与并发锁)**: 掌握 Node 级策略解析、优先级评分、300s TTL 预占锁与负载均衡算法。
8. **[[preflight-and-health]] (体检与沙盒深层探针)**: 掌握轻量与深度沙盒探针如何保证无副作用检测 Agent 额度与凭证。
9. **[[common-change-paths]] (高频开发与代码修改指南)**: 针对常见业务与工程需求（如添加新 Agent、修改状态机、重载服务），指导您需要同时关注哪些文件与测试。
10. **[[ops-center]] (Agent 运维驾驶舱)**: 了解 Dashboard V2 的老板视角、Workflow 卡片、Agent Fleet、异常中心与下钻数据契约。

---

## 2. 意图驱动寻路导航 (Task-to-Knowledge Router)

根据您当前的任务目标，直接跳转至对应领域：

| 您的工作目标 | 必须优先阅读的页面 | 核心关联源码 |
| :--- | :--- | :--- |
| **修改任务状态流转或添加新状态** | [[task-lifecycle]] | `herdr/transitions.py`, `herdr/kernel.py:transition_task/workflow` |
| **修改任务/工作流收尾、清理 pane 或 clone** | [[task-lifecycle]] §5 | `bin/herdr-task:finalize_task`, `close_workflow` |
| **新增/调整工作流模板或 DAG 调度算法** | [[dag-workflow-engine]] | `herdr/workflow.py` |
| **修改 Agent 分配算法、优先级或并发锁** | [[agent-routing-and-pools]] | `herdr/agent_router.py` |
| **排查 Tab/Pane 窗格误关、丢失或重建失败** | [[tab-node-model]] | `herdr/projects.py:ensure_node_runtime` |
| **新增支持的 AI Agent CLI** | [[common-change-paths]] | `herdr/agent_binary.py`, `herdr/preflight.py`, `herdr/deep_preflight.py` |
| **排查后台服务不推进、状态不同步** | [[architecture]] | `services/herdr-controller.py` |
| **编写或修改自动化测试** | [[dag-workflow-engine]] | `tests/test_workflow_engine.py` |
| **工作流外部受控元语与快照回溯** | [[dag-workflow-engine]], [[task-lifecycle]] | `herdr/kernel.py`, `console/herdr_factory_console.py` |
| **工位实时打断与插话纠偏 (Steering Mesh & AgentAdapter)** | [[task-lifecycle]] | `herdr/steering.py`, `herdr/agent_adapter.py`, `bin/herdr-task:steer/adapters` |
| **白盒遥测、语义提炼与产物投影 (Projection Engine)** | [[task-lifecycle]] | `herdr/projection.py`, `console/herdr_factory_console.py` |
| **通用配置驱动与受控 MCP 生态容器 (Dynamic Config & MCP Mesh)** | [[dag-workflow-engine]] | `herdr/mcp.py`, `herdr/workflow.py` |
| **状态流转网关、嵌入式状态引擎、统一事件流、检查点快照 (State Transition Gateway, StateStore, WorkflowEvent & Checkpoint Store)** | [[task-lifecycle]], [[domain-model]] | `herdr/transitions.py`, `herdr/kernel.py`, `herdr/state_store.py`, `herdr/state_db.py`, `bin/herdr-task` |
| **跨节点文档/证据共享与 stale 治理 (Workflow Shared Docs)** | [[task-lifecycle]] §5 | `herdr/workflow_docs.py`, `bin/herdr-task:note-add/note-list` |
| **更新或扩展 Wiki 本身** | [[WIKI]] / [[log]] | `wiki/WIKI.md` |

---

## 3. 知识图谱与分层索引 (Knowledge Map)

### 3.1 业务架构与模型层
- **[[system-overview]]**: 定位、目标、边界与系统红线
- **[[architecture]]**: 进程拓扑、LaunchAgent、Unix Socket 与持久化架构
- **[[domain-model]]**: 核心实体关系、持久化 JSON Schema、状态交接规则
- **[[tab-node-model]]**: 空间模型（Workspace/Tab/Pane）、Anchor 母体与 `ensure_node_runtime` 自愈

### 3.2 流程推进与运行时层
- **[[task-lifecycle]]**: 任务 11 状态机、CoW 克隆隔离沙盒、Git 分支规则与基线指纹比对
- **[[dag-workflow-engine]]**: 模板语法、Kahn 拓扑环路检测、Stage/Node 双向归一化与就绪推进
- **[[agent-routing-and-pools]]**: 候选人评分排序、Node Policy 覆盖、健康准入门禁与并发锁
- **[[preflight-and-health]]**: 静默版本检查、无副作用 Token/Auth 沙盒深探机制

### 3.3 开发者与 Agent 实操层
- **[[common-change-paths]]**: 常见业务修改的完整关联文件、注意陷阱与验证命令
- **[[ops-center]]**: 分层 Agent 运维视图、运行时/任务状态对照、时长与异常聚合
- **[[WIKI]]**: Wiki 维护规范、证据契约与更新触发条件
- **[[log]]**: 结构化演进记录与知识修订历史
