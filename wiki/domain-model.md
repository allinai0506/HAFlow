# 领域模型与核心实体 (domain-model.md)

> **公司：上海共事智能科技有限公司**  
> **品牌：共事**  
> **产品：HAFlow**  
> **一句话：让人和多个 AI Agent 一起把事情做完**  
> *Human + Agent, in Flow*  
> **实体定义、对象关系与持久化契约**  
> 关联索引: [[index]] | [[system-overview]] | [[tab-node-model]] | [[task-lifecycle]]

---

## 1. 领域对象全景模型

HAFlow 的核心业务概念由**项目绑定**、**工作流定义**、**空间现场**与**任务工单**四大聚合构成。

```mermaid
classDiagram
    class Project {
        +string project_id
        +string project_name
        +string project_root
        +string base_branch
        +string workspace_id
        +string coordinator_pane_id
        +string workflow_file
    }

    class WorkflowDefinition {
        +string name
        +string label
        +string version
        +List~WorkflowNode~ nodes
        +List~LegacyStage~ stages
    }

    class WorkflowNode {
        +string id
        +string label
        +string node_type
        +List~string~ depends_on
        +bool parallel
        +string purpose
        +string default_task_type
        +string default_integration_mode
        +Dict agent_policy
        +List~string~ required_outputs
        +string tab_id (runtime)
        +string anchor_pane_id (runtime)
    }

    class Task {
        +string task_id
        +string workflow_id
        +string project_id
        +string node
        +string agent
        +string task_type
        +string status
        +string pane_id
        +string clone_path
        +string branch
        +Dict baseline_fingerprint
    }

    class AgentPool {
        +List~string~ allowed_agents
        +List~string~ disabled_agents
        +Dict stage_preferences
        +Dict task_type_preferences
    }

    class AgentReservation {
        +string task_id
        +string agent
        +float created_at (TTL 300s)
    }

    Project "1" *-- "1" WorkflowDefinition : 绑定运行
    Project "1" *-- "1" AgentPool : 配置准入池
    WorkflowDefinition "1" *-- "n" WorkflowNode : 拓扑编排
    WorkflowNode "1" ..> "n" Task : 产生执行工单
    AgentPool "1" ..> "n" AgentReservation : 预占并发锁
```

Evidence:
- `herdr/projects.py:provision_project`
- `herdr/workflow.py:normalize_workflow`
- `bin/herdr-task:TRANSITIONS`
- `herdr/agent_router.py:ensure_pool_for_project`

---

## 2. 核心实体详细说明

### 2.1 Project (项目空间)
- `FACT` **定义**: 对应本地一个真实的 Git 仓库根目录。
- `FACT` **生成规则**:
  - `project_id`: 由目录名 Slug 加上仓库绝对路径的 SHA1 前 8 位组合生成，确保单机唯一（如 `nexusarchive-a1b2c3d4`）。
  - `workspace_id`: 在 Herdr 终端中专属创建的 Workspace ID（如 `w9`）。
  - `coordinator_pane_id`: 专属于该项目的总指挥交互窗格（如 `w9:p1`）。
  - `base_branch`: 自动嗅探或指定的基线开发分支（`main` / `dev` / `master`）。

Evidence:
- `herdr/projects.py#project_id_for`
- `herdr/projects.py#detect_base_branch`

### 2.2 Workflow Definition & Node (工作流定义与拓扑节点)
- `FACT` **定义**: 描述项目研发或业务生产流程的声明式 DAG。
- `FACT` **关键属性**:
  - `nodes`: 拓扑节点列表。每个节点具有全局唯一的 `id`、展示名 `label` 及前置依赖列表 `depends_on`。
  - `agent_policy`: 节点级 Agent 派发策略，可定义 `fixed`（固定指定）、`preferred`（优先列表）、`exclude`（排除列表）、`parallel`（最大并发数）。
  - `runtime mappings`: 包含 `tab_id` 与 `anchor_pane_id`。这两者**不是**节点的静态属性，而是动态写入的易失运行时映射。
- `FACT` **双向兼容机制 (`normalize_workflow`)**:
  - 系统历史上曾使用线性 `stages`。现在的引擎在加载任何模板或配置时，透明在 `nodes` 与 `stages` 之间执行双向同步补齐。

Evidence:
- `herdr/workflow.py#normalize_workflow`
- `docs/product-specs/agent-policy-spec.md`

### 2.2.1 Workflow Instance (工作流运行实例)
- `FACT` **定义**: 实际启动并执行某次需求变更的运行时记录（存储于 `workflows.json`）。
- `FACT` **命名与标识契约 (方案 A)**:
  - `workflow_id`: 格式为 `wf-{project_slug}-{MMDD}-{seq:02d}`（如 `wf-nexusarchive-0913-01`），去除非人类可读的路径哈希，以项目 slug + 4 位月日 + 两位当天自增序号组合，全局唯一且具备清晰时间线感知。
  - `title`: 用户输入的具体任务名称（如“适配深色模式切换”），为系统一等公民。
  - `requirement_subject`: 保持下游展示与旧接口兼容的业务主题，优先读取 `title`，回退自需求正文提取。
- `FACT` **CLI 免手敲推断**: `herdr-task close-workflow` 在项目目录下省略参数时自动推断该项目唯一活跃工作流，或支持短后缀匹配（如 `01`、`0913-01`）。

Evidence:
- `herdr/projects.py#generate_workflow_id`
- `herdr/projects.py#register_workflow`
- `bin/herdr-factory#start_workflow`
- `bin/herdr-task#resolve_workflow_id_for_close`

### 2.3 Task (任务工单)
- `FACT` **定义**: 针对具体某个 Node 派发的一次独立 Agent 执行单元。
- `FACT` **核心字段**:
  - `task_id`: 工单唯一标识（如 `TASK-001`）。
  - `status`: 任务当前在 11 状态机中所处的状态（[[task-lifecycle]]）。
  - `pane_id`: 运行该 Task 的 Herdr 终端窗格。
  - `clone_path`: 该 Task 独占的 CoW Git 克隆物理目录（`~/.herdr-controller/clones/<task_id>`）。
  - `branch`: 独占 Git 分支名（格式为 `agent/{agent}/{task_type}-{task_id}`）。
  - `baseline_fingerprint`: 派发瞬间针对工作区未提交脏文件和未跟踪文件采样的 SHA1 树快照。

Evidence:
- `bin/herdr-task:TRANSITIONS`
- `services/herdr-worker.py#create_task_branch`
- `services/herdr-worker.py#build_baseline_fingerprint`

### 2.4 Agent Pool & Reservation (Agent 准入池与预占锁)
- `FACT` **AgentPool**: 存储在 `agent-pools.json` 中，按 `project_id` 隔离。决定某个项目允许使用哪些 Agent、禁用哪些 Agent，以及不同节点与任务类型的选人偏好。
- `FACT` **AgentReservation**: 存储在 `agent-reservations.json` 中。
  - 当通过 `herdr-task launch` 或 `choose_agent` 决定分配 Agent 时，会在锁文件中预占一个 Reservation 记录。
  - `TTL`: 预占锁具备 300 秒强制超时回收机制。
  - `交接规则`: 一旦 Task 被正规注册写入 `tasks.json`，该 Task 将从 reservations 中清理，转由 `tasks.json` 中的实际 `status` 接管全局负载计数。

Evidence:
- `herdr/agent_router.py#_clean_reservations`
- `herdr/agent_router.py#choose_agent`

### 2.5 Domain Adapters SPI (微内核与领域适配器解耦)
- `FACT` **中立微内核**: 核心调度引擎（Controller, Sentinel, Worker, DAG Engine）对任何业务领域（编程、标书、客服、法务、调研）保持 100% 业务中立。
- `FACT` **BaseDomainAdapter**: 领域 SPI 接口（`herdr/domain/`），提供 `on_task_init`、`on_task_finalize`、`prepare_delivery_contract`、`evaluate_differential_tests`、`should_allow_rebase` 生命周期钩子。
- `FACT` **SoftwareDomainAdapter**: 收敛代码特定逻辑（Husky 复盘文档预埋、Git 变基采纳策略、差量测试）。
- `FACT` **GenericDomainAdapter**: 通用任务透传适配器，零文件修改与零额外副作用。

Evidence:
- `herdr/domain/base.py`
- `herdr/domain/software.py`
- `herdr/domain/generic.py`
- `herdr/domain/registry.py#get_domain_adapter`

---

## 3. 核心领域不变量 (Invariants)

1. `FACT` **Anchor Pane 永不执行业务**: 每个 Node Tab 内命名为 `"Anchor"` 的窗格专供 `herdr pane split` 分裂新工位使用，绝对不能被分配给 Task 作为工作窗格。
2. `FACT` **一个 Task 一个隔离克隆**: 一个 Task 永远对应一个独立的 CoW 克隆目录，绝不允许两个并发 Task 共享同一个克隆目录。
3. `FACT` **DAG 拓扑无环**: 工作流节点的 `depends_on` 依赖图谱必须严格为有向无环图（DAG），在模板加载与运行时校验阶段由 Kahn 算法强制保障。
4. `FACT` **微内核业务中立性**: 通用调度与 Worker 进程管理器严禁直接硬编码特定代码工程或特定业务逻辑，必须通过 Domain Adapter SPI 解耦。
5. `FACT` **门禁豁免可审计性**: 人工强制放行门禁必须签发全局唯一 bypass_id 凭证并写入轨迹账本与结论文件。

Evidence:
- `herdr/pane_pool.py#list_slots_for_project` (过滤 anchors)
- `herdr/workflow.py#validate_workflow_dag`
- `herdr/domain/registry.py`
- `herdr/kernel.py#bypass_task_gate`
