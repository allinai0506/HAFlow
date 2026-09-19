# 工作流模板规范定义 (Workflow Template Schema Spec)

> 本文档规范定义 Herdr Workflow Template (YAML / JSON) 的数据模型、字段契约、数据类型与校验边界。

---

## 1. 顶层根对象 Schema

| 字段 | 类型 | 是否必填 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- | :--- |
| `name` | `string` | **是** | - | 模板全局唯一标识符，建议只包含小写字母、数字与连字符（如 `software-development-v1`）。 |
| `label` | `string` | 否 | 与 `name` 相同 | 人类可读名称，展示在 UI、通知与命令行界面中。 |
| `version` | `string` | 否 | `"1.0"` | 模板版本，用于后续模式升级与迁移。 |
| `description` | `string` | 否 | `""` | 模板的业务场景与流程概述。 |
| `execution` | `ExecutionSpec` | 否 | `{"mode": "git"}` | 运行环境契约（见 1.1）。缺省即 legacy Git 执行路径，行为与旧模板完全一致。 |
| `context` | `ContextContract` | 否 | 无 | 业务上下文契约（见 1.2），声明任务需要的业务输入（只读引用）。 |
| `nodes` | `List[Node]` | **是** | - | 工作流包含的节点数组（定义 DAG 的拓扑图）。 |

### 1.1 ExecutionSpec (执行模式)

| 字段 | 类型 | 是否必填 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- | :--- |
| `mode` | `string` | 否 | `"git"` | 仅两个取值：`git`（Git Repo → Project → Workspace → CoW Clone → Branch → 验证/提交/集成）与 `context`（任意真实目录注册 Context 项目，Task 使用独立 Task Workspace，**不创建** CoW Clone/Branch，无 git commit/integrate 路径）。未知取值在模板归一化时直接报错。 |

`execution.mode` 与 `integration_mode` 正交：context 模式任务强制 `integration_mode=none`（`herdr-task` 对 context+git 组合 fail-fast）。context 模式的验收以 Task Workspace 产物与 verify-baseline（文件指纹）为准。

### 1.2 ContextContract (业务上下文契约)

| 字段 | 类型 | 是否必填 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- | :--- |
| `required` | `List[ContextEntry]` | 否 | `[]` | 启动 Workflow 前必须绑定的上下文条目，缺失时拒绝启动。 |
| `optional` | `List[ContextEntry]` | 否 | `[]` | 允许缺失的上下文条目。 |

`ContextEntry` 支持简写字符串（`- company`）或对象 `{id, label}`；`id` 须匹配 `^[a-z][a-z0-9_-]{0,63}$`，required/optional 间不允许重复 id。绑定值绝不写入模板——绑定属于运行期，经 CLI `herdr-factory run --context id=/绝对路径`（可重复）传入，路径校验存在性后解析为绝对路径，随 Workflow 实例持久化（StateStore 元数据，无新增表）。派发 Task 时 Agent 仅获得短小的 "Workflow Context" 引用块（id + 绝对路径），不展开文件内容、不做 RAG。

---

## 2. Node (工作流节点) Schema

| 字段 | 类型 | 是否必填 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `string` | **是** | - | 节点在当前 Workflow 中的唯一 Key（如 `requirements`）。 |
| `label` | `string` | 否 | 与 `id` 相同 | 节点的中文/展示标签。 |
| `node_type` | `string` | 否 | `"agent"` | 节点执行类型。可选：`agent` (AI 智能体), `human` (人工节点), `tool` (脚本/程序), `gate` (条件网关)。 |
| `depends_on` | `List[string]`| 否 | `[]` | 当前节点依赖的前置节点 `id` 列表。为空表示起始根节点。 |
| `parallel` | `boolean` | 否 | `false` | 是否允许该节点内部同时并发派发并运行多个 Task。 |
| `purpose` | `string` | 否 | `""` | 节点的执行目的与职责简述，用于初始化 Coordinator 提示词。 |
| `default_integration_mode` | `string` | 否 | `"none"` | 产出物的合并模式：`none` (无需合并), `branch` (合并到分支), `commit` (提交)。 |
| `default_task_type` | `string` | 否 | `"docs"` | 默认任务类型，影响路由倾向：`explore`, `plan`, `code`, `test`, `review`, `docs`。 |
| `agent_policy` | `AgentPolicy` | 否 | `{}` | 该节点专有的 Agent 调度与路由约束配置（同 `worker_policy`）。 |
| `worker_policy`| `WorkerPolicy` | 否 | 与 `agent_policy` 相同 | 执行者能力与沙盒权限约束配置（支持 `capabilities`, `permissions`）。 |
| `inputs` | `List[Dict]` | 否 | `[]` | 显式声明的上游输入依赖引用（如 `{"ref": "context.user_goal"}` 或 `{"ref": "nodes.analysis.outputs"}`）。 |
| `gate` | `GateSpec` | 否 | `{}` | 节点质量与合规门禁契约配置（支持自动规则、人工审批与打回重试目标）。 |
| `required_outputs` | `List[string]`| 否 | `[]` | 该节点验收时必须存在的产出文件路径清单。 |
| `rules` | `List[string]`| 否 | `[]` | 该节点执行过程必须强制遵循的业务/工程规则。 |

### rules 字段中的技能引用

当 `rules` 中包含对特定技能的引用时（如 `必须使用 six-step-finish 技能`），
该技能必须在仓库内 `.agents/skills/<skill-name>/` 目录下存在（vendored），
或提供绝对路径作为兜底。规则文本中引用的 base_branch 由 Agent 从
stage advance 事件头部的 `base_branch` 字段获取，controller 对 rules
做逐字 join，不做占位符渲染。

---

## 3. WorkerPolicy 与 AgentPolicy Schema

| 字段 | 类型 | 是否必填 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- | :--- |
| `fixed` | `string` | 否 | `null` | 强制指定特定的 Agent 模型（如 `claude`），此时忽略自动路由。 |
| `preferred` | `List[string]`| 否 | `[]` | 偏好 Agent 候选列表，排在越前优先级越高。 |
| `exclude` | `List[string]`| 否 | `[]` | 坚决排除的 Agent 列表（例如排斥某类轻量 Agent）。 |
| `capabilities` | `List[string]`| 否 | `[]` | 该节点所需的工具能力清单（如 `web_search`、`data_extraction`、`analysis`）。 |
| `permissions` | `List[string]`| 否 | `["read_write"]` | 该节点的权限安全边界，可选：`read_only`, `read_write`, `require_approval`, `admin`。 |
| `min_agents` | `integer` | 否 | `1` | 允许派发的最小 Agent 数量。 |
| `max_agents` | `integer` | 否 | `1` | 允许派发的最大并发 Agent 数量。 |

---

## 4. GateSpec (门禁契约) Schema

| 字段 | 类型 | 是否必填 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- | :--- |
| `type` | `string` | 否 | `"auto"` | 门禁类型：`auto` (自动判定), `human` (人工核验), `hybrid` (自动+人工会签)。 |
| `auto_criteria` | `string` | 否 | `""` | 自动化评估准则表达式（如 `len(artifacts) >= 3`）。 |
| `requires_human_approval` | `boolean` | 否 | `false` | 是否强制需要人类总指挥在控制台签署通过。 |
| `retry_target` | `string` | 否 | `null` | 门禁验收失败或打回时，指定重置回退的目标节点 ID。 |

---

## 5. DAG 校验规则

所有模板在加载时必须通过 `validate_workflow_dag` 检查：
1. **唯一性校验**：所有节点的 `id` 必须全图唯一，不允许重名。
2. **合法依赖引用校验**：所有 `depends_on` 中的节点 ID 必须在当前模板的 `nodes` 中存在。
3. **输入引用合法性校验**：若 `inputs` 声明了以 `nodes.<node_id>` 开头的引用，引用的 `<node_id>` 必须存在于图中。
4. **门禁回退目标校验**：若 `gate.retry_target` 非空，指定的重试节点必须在 `nodes` 中存在。
5. **无环图拓扑校验 (Acyclic)**：基于 Kahn 算法进行拓扑检测，不能存在循环依赖（如 A ➔ B ➔ C ➔ A）。
